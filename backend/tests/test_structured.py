"""Structured generation: schema validation, bounded deterministic retries, limiter governance."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from pydantic import BaseModel, Field

from app.api.inference import run_structured
from app.config import ApiSettings, ConfigError
from app.services import (
    OllamaHTTPError,
    OllamaResponseError,
    OllamaUnavailableError,
    PromptTooLargeError,
    StructuredGenerator,
    StructuredOutputInvalidError,
    StructuredOutputTruncatedError,
    TokenBudget,
)

pytestmark = pytest.mark.anyio


class Mini(BaseModel):
    """Minimal test schema (NOT a Sift schema)."""

    summary: str = Field(min_length=1)
    line_count: int = Field(ge=0)


GOOD = {"summary": "Prints numbers.", "line_count": 3}


def reply(text, **extra) -> httpx.Response:
    return httpx.Response(200, json={"response": text, "done": True, **extra})


def scripted(*replies):
    """Handler that answers /api/generate with the given replies, in order."""
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return handler


def sent_prompts(transport) -> list[str]:
    return [json.loads(r.content)["prompt"] for r in transport.requests]


# --------------------------------------------------------------------------- #
# Request shape and the success path
# --------------------------------------------------------------------------- #
async def test_success_sends_the_schema_as_ollama_format_and_returns_a_model(service_factory):
    service, transport = service_factory(scripted(reply(json.dumps(GOOD))))

    result = await StructuredGenerator(service).generate("Describe the code", Mini)

    assert result.value == Mini(**GOOD)
    assert isinstance(result.value, Mini)
    assert result.attempts == 1
    payload = json.loads(transport.requests[0].content)
    assert payload["format"] == Mini.model_json_schema()
    assert payload["stream"] is False
    assert payload["model"] == service.settings.model  # model/options stay server-controlled
    assert payload["options"]["num_predict"] == service.settings.num_predict


async def test_plain_generation_does_not_send_a_format_field(service_factory):
    service, transport = service_factory(scripted(reply("plain text")))

    assert await service.generate("hi") == "plain text"
    assert "format" not in json.loads(transport.requests[0].content)


async def test_ollama_token_counts_are_exposed_on_the_generation(service_factory):
    service, _ = service_factory(
        scripted(reply("x", prompt_eval_count=77, eval_count=5), reply("y", prompt_eval_count="bad"))
    )

    first = await service.generate_detailed("hi")
    second = await service.generate_detailed("hi")

    assert (first.prompt_eval_count, first.eval_count) == (77, 5)
    assert (second.prompt_eval_count, second.eval_count) == (None, None)


# --------------------------------------------------------------------------- #
# Validation is real: valid JSON syntax is not enough
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_reply",
    [
        "not json at all",
        "",
        '{"summary": "x"',  # cut off mid-object
        "{}",  # valid JSON, required fields missing
        '{"summary": "x", "line_count": "three"}',  # wrong type
        '{"summary": "", "line_count": 1}',  # violates min_length
        '{"summary": "x", "line_count": -1}',  # violates ge=0
        "[1, 2, 3]",  # valid JSON, wrong shape
        "null",
    ],
)
async def test_invalid_output_is_never_accepted_even_when_it_is_valid_json(service_factory, bad_reply):
    service, _ = service_factory(scripted(reply(bad_reply or " ")))

    with pytest.raises(OllamaResponseError if not bad_reply else StructuredOutputInvalidError):
        await StructuredGenerator(service, max_retries=0).generate("p", Mini)


# --------------------------------------------------------------------------- #
# Retry policy
# --------------------------------------------------------------------------- #
async def test_a_rejected_attempt_is_retried_once_by_default_with_a_reason(service_factory):
    service, transport = service_factory(scripted(reply("oops {"), reply(json.dumps(GOOD))))

    result = await StructuredGenerator(service).generate("Describe the code", Mini)

    assert result.attempts == 2 and result.value == Mini(**GOOD)
    first, second = sent_prompts(transport)
    assert first == "Describe the code"
    assert second.startswith("Describe the code")
    assert "previous reply was rejected" in second and "not valid JSON" in second


async def test_schema_failure_retry_names_the_field_but_never_echoes_the_bad_value(service_factory):
    bad = json.dumps({"summary": "ok", "line_count": "SECRET-VALUE-12345"})
    service, transport = service_factory(scripted(reply(bad), reply(json.dumps(GOOD))))

    await StructuredGenerator(service).generate("p", Mini)

    retry_prompt = sent_prompts(transport)[1]
    assert "line_count" in retry_prompt
    assert "SECRET-VALUE-12345" not in retry_prompt


@pytest.mark.parametrize("max_retries, expected_requests", [(0, 1), (1, 2), (2, 3), (5, 6)])
async def test_attempts_are_bounded_by_the_configured_retry_limit(
    service_factory, max_retries, expected_requests
):
    service, transport = service_factory(scripted(*[reply("garbage")] * 10))

    with pytest.raises(StructuredOutputInvalidError) as excinfo:
        await StructuredGenerator(service, max_retries=max_retries).generate("p", Mini)

    assert len(transport.requests) == expected_requests  # exactly bounded, never unlimited
    assert excinfo.value.attempts == expected_requests
    assert "not valid JSON" in excinfo.value.reason


async def test_retry_limit_is_validated():
    for bad in (-1, 6, True, 1.5):
        with pytest.raises(ConfigError):
            StructuredGenerator(None, max_retries=bad)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        ApiSettings(structured_max_retries=6)
    assert ApiSettings.from_env({"AI_STRUCTURED_MAX_RETRIES": "3"}).structured_max_retries == 3


async def test_retry_behaviour_is_deterministic(service_factory):
    runs = []
    for _ in range(2):
        service, transport = service_factory(scripted(reply("bad"), reply("worse"), reply(json.dumps(GOOD))))
        await StructuredGenerator(service, max_retries=2).generate("same prompt", Mini)
        runs.append(sent_prompts(transport))

    assert runs[0] == runs[1]


# --------------------------------------------------------------------------- #
# What must NOT be retried
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "failure, error",
    [
        (httpx.ConnectError("refused"), OllamaUnavailableError),
        (httpx.ReadTimeout("slow"), Exception),
        (httpx.Response(500, json={"error": "runner died"}), OllamaHTTPError),
        (reply(""), OllamaResponseError),  # Ollama returned an empty response
    ],
    ids=["connect-error", "read-timeout", "http-500", "empty-response"],
)
async def test_transport_and_ollama_failures_are_not_retried(service_factory, failure, error):
    service, transport = service_factory(scripted(failure, reply(json.dumps(GOOD))))

    with pytest.raises(error):
        await StructuredGenerator(service, max_retries=3).generate("p", Mini)

    assert len(transport.requests) == 1


async def test_truncated_invalid_output_is_not_retried(service_factory):
    cut_off = '{"summary": "A very long explanation that never ends'
    service, transport = service_factory(scripted(reply(cut_off, done_reason="length"), reply(json.dumps(GOOD))))

    with pytest.raises(StructuredOutputTruncatedError) as excinfo:
        await StructuredGenerator(service, max_retries=3).generate("p", Mini)

    assert len(transport.requests) == 1  # a retry would hit the same token limit again
    assert excinfo.value.attempts == 1


async def test_complete_valid_output_is_accepted_even_if_the_limit_was_reached(service_factory, caplog):
    service, _ = service_factory(scripted(reply(json.dumps(GOOD) + "\n\n\n", done_reason="length")))

    with caplog.at_level(logging.WARNING):
        result = await StructuredGenerator(service).generate("p", Mini)

    assert result.value == Mini(**GOOD)
    assert result.generation.truncated is True
    assert any("done_reason=length" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Token budget integration
# --------------------------------------------------------------------------- #
async def test_oversized_prompt_is_rejected_before_anything_is_sent(service_factory):
    service, transport = service_factory(scripted(reply(json.dumps(GOOD))))
    generator = StructuredGenerator(service, budget=TokenBudget(4096, 1024, 256, max_input_tokens=10))

    with pytest.raises(PromptTooLargeError):
        await generator.generate("word " * 200, Mini)

    assert transport.requests == []


async def test_a_retry_that_would_not_fit_the_budget_is_skipped(service_factory):
    original = "Describe the code in detail please"
    budget = TokenBudget(4096, 1024, 256)
    needed = budget.count(original).tokens
    tight = TokenBudget(4096, 1024, 256, max_input_tokens=needed + 2)  # fits original, not original+suffix
    service, transport = service_factory(scripted(reply("garbage"), reply(json.dumps(GOOD))))

    with pytest.raises(StructuredOutputInvalidError) as excinfo:
        await StructuredGenerator(service, max_retries=3, budget=tight).generate(original, Mini)

    assert len(transport.requests) == 1
    assert "exceed the input budget" in excinfo.value.reason


# --------------------------------------------------------------------------- #
# Through the API layer: every attempt is governed by the limiter
# --------------------------------------------------------------------------- #
def add_structured_route(app):
    """A test-only route; the real API deliberately has no public structured endpoint yet."""

    @app.post("/test-only/structured")
    async def structured_route():
        result = await run_structured(app, "Describe the code", Mini)
        return {"value": result.value.model_dump(), "attempts": result.attempts}


class SlowSecondAttempt:
    """Fake Ollama: attempt 1 is garbage (instant); attempt 2 blocks until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "x"})
        self.calls += 1
        if self.calls == 1:
            return reply("garbage")
        await self.release.wait()
        return reply(json.dumps(GOOD))


async def test_structured_success_and_failure_through_the_api(api_factory):
    api = await api_factory(scripted(reply(json.dumps(GOOD))))
    add_structured_route(api.app)

    response = await api.client.post("/test-only/structured", json={})

    assert response.status_code == 200
    assert response.json() == {"value": GOOD, "attempts": 1}
    assert api.app.state.limiter.active == 0


@pytest.mark.parametrize(
    "replies, status, code",
    [
        ([reply("bad"), reply("bad")], 502, "INVALID_STRUCTURED_OUTPUT"),
        ([reply('{"summary": "cut', done_reason="length")], 502, "OUTPUT_TRUNCATED"),
    ],
)
async def test_structured_failures_map_to_clean_errors_and_release_the_slot(
    api_factory, replies, status, code
):
    api = await api_factory(scripted(*replies, reply(json.dumps(GOOD))), api=ApiSettings(queue_wait_timeout=0))
    add_structured_route(api.app)

    response = await api.client.post("/test-only/structured", json={})

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "summary" not in response.text  # no raw model output in the error
    assert api.app.state.limiter.active == 0


async def test_oversized_structured_prompt_maps_to_422(api_factory):
    api = await api_factory(scripted(), api=ApiSettings(max_input_tokens=3))
    add_structured_route(api.app)

    response = await api.client.post("/test-only/structured", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROMPT_TOO_MANY_TOKENS"


async def test_retries_hold_one_slot_for_all_attempts(api_factory):
    slow = SlowSecondAttempt()
    api = await api_factory(slow, api=ApiSettings(queue_wait_timeout=0.1))
    add_structured_route(api.app)

    structured = asyncio.create_task(api.client.post("/test-only/structured", json={}))
    try:
        # Attempt 1 has been rejected and attempt 2 is running. The slot must still be held,
        # otherwise another request could slip in between attempts.
        while slow.calls < 2:
            await asyncio.sleep(0.01)
        assert api.app.state.limiter.active == 1
        busy = await api.client.post("/api/ai/generate", json={"prompt": "hello"})
        assert busy.status_code == 503 and busy.json()["error"]["code"] == "AI_BUSY"
        assert slow.calls == 2  # the busy request never reached Ollama
    finally:
        slow.release.set()
    response = await structured

    assert response.status_code == 200 and response.json()["attempts"] == 2
    assert api.app.state.limiter.active == 0


class BlockingOllama:
    """Fake Ollama whose /api/generate blocks until released, then returns valid JSON."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "x"})
        self.calls += 1
        await self.release.wait()
        return reply(json.dumps(GOOD))


async def test_a_waiting_structured_request_gets_ai_busy_not_a_silent_queue(api_factory):
    blocking = BlockingOllama()
    api = await api_factory(blocking, api=ApiSettings(queue_wait_timeout=0.1))
    add_structured_route(api.app)
    holder = asyncio.create_task(api.client.post("/api/ai/generate", json={"prompt": "hello"}))
    try:
        while blocking.calls < 1:
            await asyncio.sleep(0.01)
        blocked = await asyncio.wait_for(api.client.post("/test-only/structured", json={}), timeout=5)
        assert blocking.calls == 1  # the structured request never reached Ollama
    finally:
        blocking.release.set()
        await holder

    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "AI_BUSY"


# --------------------------------------------------------------------------- #
# Phase 4: per-call retry override (default behaviour unchanged)
# --------------------------------------------------------------------------- #
async def test_per_call_zero_retries_makes_exactly_one_attempt(service_factory):
    service, transport = service_factory(scripted(reply("nope"), reply(json.dumps(GOOD))))
    with pytest.raises(StructuredOutputInvalidError) as caught:
        await StructuredGenerator(service, max_retries=3).generate("p", Mini, max_retries=0)
    assert caught.value.attempts == 1 and len(transport.requests) == 1


async def test_per_call_override_beats_the_instance_default_in_both_directions(service_factory):
    service, transport = service_factory(scripted(reply("nope"), reply(json.dumps(GOOD))))
    result = await StructuredGenerator(service, max_retries=0).generate("p", Mini, max_retries=1)
    assert result.attempts == 2 and len(transport.requests) == 2


async def test_without_an_override_the_instance_default_still_applies(service_factory):
    service, transport = service_factory(scripted(reply("nope"), reply("nope")))
    with pytest.raises(StructuredOutputInvalidError) as caught:
        await StructuredGenerator(service, max_retries=1).generate("p", Mini)
    assert caught.value.attempts == 2


@pytest.mark.parametrize("bad", [-1, 6, True, 1.5, "1"])
async def test_invalid_per_call_override_is_a_config_error_before_any_request(service_factory, bad):
    service, transport = service_factory(scripted())
    with pytest.raises(ConfigError):
        await StructuredGenerator(service).generate("p", Mini, max_retries=bad)
    assert transport.requests == []


async def test_the_retry_sentence_is_unchanged(service_factory):
    from app.services.structured import retry_prompt

    service, transport = service_factory(scripted(reply("nope"), reply(json.dumps(GOOD))))
    await StructuredGenerator(service).generate("PROMPT", Mini)
    second = sent_prompts(transport)[1]
    assert second.startswith("PROMPT\n\nYour previous reply was rejected: ")
    assert second.endswith(". Reply again with only valid JSON that matches the required schema.")
    assert retry_prompt("PROMPT", "why") == "PROMPT\n\nYour previous reply was rejected: why. Reply again with only valid JSON that matches the required schema."
