"""API-level tests: the real FastAPI app on a mocked Ollama (no Ollama/GPU/internet)."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest

from app.config import ApiSettings, ConfigError, OllamaSettings
from app.main import create_app

pytestmark = pytest.mark.anyio

MODEL = "qwen2.5-coder:3b"
PROMPT = "Explain what a Python for loop does."


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ollama_handler(
    *,
    models: tuple[str, ...] = (MODEL,),
    version: str = "0.40.2",
    text: str = "A for loop iterates over an iterable.",
    **generate_extra,
):
    """A well-behaved fake Ollama server."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": version})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m, "model": m} for m in models]})
        if path == "/api/generate":
            return httpx.Response(
                200, json={"model": MODEL, "response": text, "done": True, **generate_extra}
            )
        return httpx.Response(404, text="404 page not found")

    return handler


def only_generate(inner):
    """Wrap ``inner`` so it runs for /api/generate; other paths behave normally."""
    ok = ollama_handler()

    def handler(request: httpx.Request):
        return inner(request) if request.url.path == "/api/generate" else ok(request)

    return handler


def raising(exc: BaseException):
    def inner(request: httpx.Request):
        raise exc

    return only_generate(inner)


def replying(status: int, body=None, *, content: bytes | None = None):
    def inner(request: httpx.Request):
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, json=body)

    return only_generate(inner)


class Gate:
    """Fake Ollama whose /api/generate blocks until ``release`` is set."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.peak = 0
        self._ok = ollama_handler()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/generate":
            return self._ok(request)
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return self._ok(request)

    async def wait_for_calls(self, n: int) -> None:
        await wait_until(lambda: self.calls >= n)


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


def post_generate(client: httpx.AsyncClient, prompt: str = PROMPT):
    return client.post("/api/ai/generate", json={"prompt": prompt})


def assert_error(response: httpx.Response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    return body["error"]


# --------------------------------------------------------------------------- #
# Application lifecycle
# --------------------------------------------------------------------------- #
async def test_startup_needs_no_ollama_and_shutdown_closes_the_shared_client():
    from conftest import TrackingTransport  # noqa: PLC0415

    transport = TrackingTransport(ollama_handler())
    app = create_app(OllamaSettings(), ApiSettings(), ollama_transport=transport)

    async with app.router.lifespan_context(app):
        assert app.state.ollama.settings.model == MODEL
        assert app.state.limiter.max_concurrent == 1
        assert transport.requests == []  # startup made no network call
        # The Ollama client is opened lazily; use it so there is something to close.
        assert (await app.state.ollama.health_check()).healthy
        assert transport.closed is False

    assert transport.closed is True  # shutdown closed the shared HTTP client


async def test_one_ollama_service_and_client_is_shared_across_requests(api_factory):
    api = await api_factory(ollama_handler())
    service = api.app.state.ollama

    await api.client.get("/api/ai/health")
    client_after_health = service._client
    await post_generate(api.client)
    await post_generate(api.client)

    assert api.app.state.ollama is service
    assert service._client is client_after_health  # pooled client reused, not recreated
    assert api.transport.closed is False


def test_invalid_environment_fails_fast_at_app_creation(monkeypatch):
    monkeypatch.setenv("AI_MAX_CONCURRENT_REQUESTS", "zero")

    with pytest.raises(ConfigError, match="AI_MAX_CONCURRENT_REQUESTS"):
        create_app()


def test_module_level_app_is_exposed_for_uvicorn():
    from app.main import app  # noqa: PLC0415

    paths = app.openapi()["paths"]
    assert set(paths["/api/ai/health"]) == {"get"}
    assert set(paths["/api/ai/generate"]) == {"post"}


# --------------------------------------------------------------------------- #
# GET /api/ai/health
# --------------------------------------------------------------------------- #
async def test_health_ready(api_factory):
    api = await api_factory(ollama_handler(version="0.40.2"))

    response = await api.client.get("/api/ai/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "ollama": {"healthy": True, "version": "0.40.2", "detail": None},
        "model": {"name": MODEL, "available": True, "detail": None},
    }


async def test_health_never_runs_inference(api_factory):
    api = await api_factory(ollama_handler())

    await api.client.get("/api/ai/health")

    assert [(r.method, r.url.path) for r in api.transport.requests] == [
        ("GET", "/api/version"),
        ("GET", "/api/tags"),
    ]


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("t"), httpx.ReadTimeout("t")]
)
async def test_health_ollama_unavailable_is_unavailable_with_http_200(api_factory, exc):
    def handler(request):
        raise exc

    api = await api_factory(handler)

    response = await api.client.get("/api/ai/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["ollama"]["healthy"] is False
    assert body["ollama"]["version"] is None
    assert body["ollama"]["detail"]
    assert body["model"] == {
        "name": MODEL,
        "available": None,
        "detail": "Not checked because Ollama is unreachable.",
    }
    # Don't pile a second slow connection attempt on top of an unreachable server.
    assert [r.url.path for r in api.transport.requests] == ["/api/version"]


async def test_health_model_missing_is_degraded_and_available_false(api_factory):
    api = await api_factory(ollama_handler(models=("llama3:8b",)))

    response = await api.client.get("/api/ai/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["ollama"]["healthy"] is True
    assert body["model"]["available"] is False
    assert f"ollama pull {MODEL}" in body["model"]["detail"]


@pytest.mark.parametrize(
    "tags_response",
    [
        lambda: httpx.Response(500, json={"error": "secret internal failure"}),
        lambda: httpx.Response(200, content=b"not json"),
        lambda: httpx.Response(200, json={"models": "nope"}),
    ],
    ids=["http-500", "invalid-json", "malformed-body"],
)
async def test_health_model_check_failure_is_degraded_and_available_null(
    api_factory, tags_response
):
    ok = ollama_handler()
    api = await api_factory(
        lambda request: tags_response() if request.url.path == "/api/tags" else ok(request)
    )

    response = await api.client.get("/api/ai/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["ollama"]["healthy"] is True
    # "could not check" (null) must stay distinguishable from "not installed" (false)
    assert body["model"]["available"] is None
    assert body["model"]["detail"]
    assert "secret" not in response.text


async def test_health_survives_model_availability_raising_any_exception(api_factory, caplog):
    api = await api_factory(ollama_handler())

    async def explode() -> bool:
        raise RuntimeError(r"secret C:\internal\path")

    api.app.state.ollama.model_available = explode

    with caplog.at_level(logging.WARNING):
        response = await api.client.get("/api/ai/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["model"]["available"] is None
    assert "secret" not in response.text and "Traceback" not in response.text
    assert any("Model availability check failed" in r.getMessage() for r in caplog.records)


async def test_health_is_not_blocked_by_a_running_generation(api_factory):
    gate = Gate()
    api = await api_factory(gate)

    generation = asyncio.create_task(post_generate(api.client))
    try:
        await gate.wait_for_calls(1)
        response = await asyncio.wait_for(api.client.get("/api/ai/health"), timeout=2)
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
    finally:
        gate.release.set()
        await generation


# --------------------------------------------------------------------------- #
# POST /api/ai/generate: success and request validation
# --------------------------------------------------------------------------- #
async def test_generate_success_schema(api_factory):
    api = await api_factory(ollama_handler(text="A for loop iterates over an iterable."))

    response = await post_generate(api.client)

    assert response.status_code == 200
    assert response.json() == {
        "model": MODEL,
        "response": "A for loop iterates over an iterable.",
        "status": "completed",
    }


async def test_generate_uses_server_side_model_and_limits_and_hides_raw_payload(api_factory):
    api = await api_factory(
        ollama_handler(total_duration=123, context=[1, 2, 3]),
        model="other:1b",
        num_ctx=2048,
        num_predict=64,
    )

    response = await post_generate(api.client)

    (sent,) = [r for r in api.transport.requests if r.url.path == "/api/generate"]
    payload = json.loads(sent.content)
    assert payload["model"] == "other:1b"
    assert payload["stream"] is False
    assert payload["options"]["num_ctx"] == 2048 and payload["options"]["num_predict"] == 64
    assert response.json()["model"] == "other:1b"
    assert set(response.json()) == {"model", "response", "status"}  # no raw Ollama fields


async def test_generate_reports_truncated_output_honestly(api_factory):
    api = await api_factory(ollama_handler(text="partial ans", done_reason="length"))

    response = await post_generate(api.client)

    assert response.status_code == 200
    assert response.json()["status"] == "truncated"
    assert response.json()["response"] == "partial ans"


async def test_generate_normal_stop_is_completed(api_factory):
    api = await api_factory(ollama_handler(done_reason="stop"))

    assert (await post_generate(api.client)).json()["status"] == "completed"


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t  \n"])
async def test_generate_rejects_empty_and_whitespace_prompts(api_factory, prompt):
    api = await api_factory(ollama_handler())

    response = await post_generate(api.client, prompt)

    error = assert_error(response, 422, "INVALID_REQUEST")
    assert error["details"] == [
        {"field": "prompt", "message": "prompt must not be empty or whitespace-only"}
    ]
    assert api.transport.requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": {}},
        {"json": {"prompt": None}},
        {"json": {"prompt": 123}},
        {"json": ["prompt"]},
        {"content": b"{not json", "headers": {"content-type": "application/json"}},
        {"content": b"", "headers": {"content-type": "application/json"}},
    ],
    ids=["missing", "null", "number", "list-body", "bad-json", "empty-body"],
)
async def test_generate_rejects_malformed_requests(api_factory, kwargs):
    api = await api_factory(ollama_handler())

    response = await api.client.post("/api/ai/generate", **kwargs)

    assert_error(response, 422, "INVALID_REQUEST")
    assert api.transport.requests == []


@pytest.mark.parametrize("extra", [{"model": "llama3:70b"}, {"options": {"num_ctx": 99999}}])
async def test_generate_does_not_accept_model_or_ollama_overrides(api_factory, extra):
    api = await api_factory(ollama_handler())

    response = await api.client.post("/api/ai/generate", json={"prompt": PROMPT, **extra})

    error = assert_error(response, 422, "INVALID_REQUEST")
    assert list(extra)[0] in error["details"][0]["field"]
    assert api.transport.requests == []


async def test_validation_errors_do_not_echo_the_submitted_input(api_factory):
    api = await api_factory(ollama_handler())

    response = await api.client.post("/api/ai/generate", json={"prompt": 12345678901})

    assert "12345678901" not in response.text


async def test_generate_rejects_oversized_prompt_but_accepts_the_boundary(api_factory):
    api = await api_factory(ollama_handler(), api=ApiSettings(max_prompt_chars=10))

    too_long = await post_generate(api.client, "x" * 11)
    at_limit = await post_generate(api.client, "x" * 10)

    error = assert_error(too_long, 422, "PROMPT_TOO_LONG")
    assert "10" in error["message"]
    assert at_limit.status_code == 200
    assert len([r for r in api.transport.requests if r.url.path == "/api/generate"]) == 1


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "handler, status, code",
    [
        (raising(httpx.ConnectError("refused")), 503, "OLLAMA_UNAVAILABLE"),
        (raising(httpx.ConnectTimeout("t")), 503, "OLLAMA_CONNECT_TIMEOUT"),
        (raising(httpx.ReadTimeout("t")), 504, "GENERATION_TIMEOUT"),
        (raising(httpx.WriteTimeout("t")), 504, "GENERATION_TIMEOUT"),
        (
            replying(404, {"error": f"model '{MODEL}' not found"}),
            503,
            "MODEL_NOT_INSTALLED",
        ),
        (replying(500, {"error": "secret runner crash"}), 502, "OLLAMA_UPSTREAM_ERROR"),
        (replying(503, {"error": "server busy"}), 503, "OLLAMA_UPSTREAM_ERROR"),
        (replying(404, content=b"404 page not found"), 502, "OLLAMA_UPSTREAM_ERROR"),
        (replying(200, content=b"{broken"), 502, "INVALID_OLLAMA_RESPONSE"),
        (replying(200, {"done": True}), 502, "INVALID_OLLAMA_RESPONSE"),
        (replying(200, {"response": "  ", "done": True}), 502, "INVALID_OLLAMA_RESPONSE"),
        (replying(200, {"error": "secret out of memory"}), 500, "INFERENCE_FAILED"),
        (raising(RuntimeError(r"secret C:\internal\path")), 500, "INFERENCE_FAILED"),
    ],
    ids=[
        "unavailable",
        "connect-timeout",
        "read-timeout",
        "write-timeout",
        "model-missing",
        "upstream-500",
        "upstream-503",
        "upstream-unrelated-404",
        "invalid-json",
        "missing-response",
        "blank-response",
        "error-field",
        "unexpected-exception",
    ],
)
async def test_ollama_errors_map_to_consistent_http_errors(api_factory, handler, status, code):
    api = await api_factory(handler)

    response = await post_generate(api.client)

    error = assert_error(response, status, code)
    assert set(error) == {"code", "message"}
    # Raw Ollama bodies, exception text, paths and stack traces never reach the client.
    assert "secret" not in response.text
    assert "Traceback" not in response.text
    assert "Error(" not in response.text


async def test_model_missing_message_is_actionable(api_factory):
    api = await api_factory(replying(404, {"error": f"model '{MODEL}' not found"}))

    error = assert_error(await post_generate(api.client), 503, "MODEL_NOT_INSTALLED")

    assert f"ollama pull {MODEL}" in error["message"]


async def test_error_details_are_logged_server_side(api_factory, caplog):
    api = await api_factory(replying(500, {"error": "llama runner process has terminated"}))

    with caplog.at_level(logging.WARNING):
        response = await post_generate(api.client)

    assert response.status_code == 502
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "OLLAMA_UPSTREAM_ERROR" in logged and "llama runner process has terminated" in logged


async def test_unexpected_non_ollama_exception_becomes_a_clean_500(api_factory, caplog):
    api = await api_factory(ollama_handler())

    async def boom(prompt: str):
        raise ValueError(r"secret C:\internal\path")

    api.app.state.ollama.generate_detailed = boom

    with caplog.at_level(logging.ERROR):
        response = await post_generate(api.client)

    assert_error(response, 500, "INTERNAL_ERROR")
    assert "secret" not in response.text
    assert any(r.exc_info for r in caplog.records)  # traceback is in the log


async def test_unknown_route_and_wrong_method_use_the_same_error_shape(api_factory):
    api = await api_factory(ollama_handler())

    assert_error(await api.client.get("/api/nope"), 404, "NOT_FOUND")
    assert_error(await api.client.get("/api/ai/generate"), 405, "METHOD_NOT_ALLOWED")


# --------------------------------------------------------------------------- #
# Bounded inference concurrency
# --------------------------------------------------------------------------- #
async def test_only_one_inference_runs_at_a_time_by_default(api_factory):
    gate = Gate()
    api = await api_factory(gate, api=ApiSettings(queue_wait_timeout=5))

    first = asyncio.create_task(post_generate(api.client, "one"))
    await gate.wait_for_calls(1)
    second = asyncio.create_task(post_generate(api.client, "two"))
    await asyncio.sleep(0.1)  # give the second request every chance to start

    assert gate.calls == 1 and api.app.state.limiter.active == 1

    gate.release.set()
    r1, r2 = await asyncio.gather(first, second)

    assert (r1.status_code, r2.status_code) == (200, 200)
    assert gate.calls == 2
    assert gate.peak == 1
    assert api.app.state.limiter.active == 0


async def test_concurrency_limit_is_configurable(api_factory):
    gate = Gate()
    api = await api_factory(gate, api=ApiSettings(max_concurrent_requests=2, queue_wait_timeout=5))

    tasks = [asyncio.create_task(post_generate(api.client, str(i))) for i in range(3)]
    await gate.wait_for_calls(2)
    await asyncio.sleep(0.1)

    assert gate.calls == 2  # the third is queued behind the two slots

    gate.release.set()
    responses = await asyncio.gather(*tasks)

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert gate.peak == 2


async def test_queue_wait_timeout_returns_ai_busy_then_recovers(api_factory):
    gate = Gate()
    api = await api_factory(gate, api=ApiSettings(queue_wait_timeout=0.1))

    first = asyncio.create_task(post_generate(api.client, "one"))
    await gate.wait_for_calls(1)

    started = time.perf_counter()
    busy = await post_generate(api.client, "two")
    waited = time.perf_counter() - started

    error = assert_error(busy, 503, "AI_BUSY")
    assert busy.headers["retry-after"]
    assert "try again" in error["message"].lower()
    assert 0.08 <= waited < 2  # waited for the queue timeout, no longer
    assert gate.calls == 1  # the busy request never reached Ollama

    gate.release.set()
    assert (await first).status_code == 200
    assert (await post_generate(api.client, "three")).status_code == 200


async def test_slot_is_released_after_ollama_errors(api_factory):
    outcomes = iter(
        [httpx.ReadTimeout("t"), httpx.ConnectError("x"), RuntimeError("y"), None, None]
    )
    ok = ollama_handler()

    def handler(request: httpx.Request):
        if request.url.path != "/api/generate":
            return ok(request)
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome
        return ok(request)

    # queue_wait_timeout=0: a leaked slot would turn every later call into AI_BUSY.
    api = await api_factory(handler, api=ApiSettings(queue_wait_timeout=0))

    statuses = [(await post_generate(api.client)).status_code for _ in range(5)]

    assert statuses == [504, 503, 500, 200, 200]
    assert api.app.state.limiter.active == 0


async def test_slot_is_released_when_the_request_is_cancelled(api_factory):
    gate = Gate()
    api = await api_factory(gate, api=ApiSettings(queue_wait_timeout=0))

    task = asyncio.create_task(post_generate(api.client))
    await gate.wait_for_calls(1)
    assert api.app.state.limiter.active == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_until(lambda: api.app.state.limiter.active == 0)

    gate.release.set()  # later calls go straight through
    assert (await post_generate(api.client)).status_code == 200


async def test_invalid_requests_never_consume_an_inference_slot(api_factory):
    gate = Gate()
    api = await api_factory(gate, api=ApiSettings(queue_wait_timeout=0, max_prompt_chars=5))

    holder = asyncio.create_task(post_generate(api.client, "ok"))
    await gate.wait_for_calls(1)

    # While the only slot is taken, bad input still gets its precise 422 (not AI_BUSY).
    assert_error(await post_generate(api.client, "   "), 422, "INVALID_REQUEST")
    assert_error(await post_generate(api.client, "too long!"), 422, "PROMPT_TOO_LONG")

    gate.release.set()
    await holder


# --------------------------------------------------------------------------- #
# CORS (browser on the Vite dev server -> API)
# --------------------------------------------------------------------------- #
async def test_cors_allows_the_vite_dev_origin_only(api_factory):
    api = await api_factory(ollama_handler())
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }

    allowed = await api.client.options(
        "/api/ai/generate", headers={"Origin": "http://127.0.0.1:5173", **preflight}
    )
    denied = await api.client.options(
        "/api/ai/generate", headers={"Origin": "http://evil.example", **preflight}
    )

    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "access-control-allow-origin" not in denied.headers


# --------------------------------------------------------------------------- #
# Token-aware budgeting through the API
# --------------------------------------------------------------------------- #
async def test_prompt_under_the_char_limit_but_over_the_token_budget_is_rejected(api_factory):
    # 300 digit characters: far below the 6000-char limit, but ~300 estimated tokens.
    api = await api_factory(ollama_handler(), api=ApiSettings(max_input_tokens=100))

    response = await post_generate(api.client, "7" * 300)

    error = assert_error(response, 422, "PROMPT_TOO_MANY_TOKENS")
    assert "estimate" in error["message"] and "100" in error["message"]
    assert api.transport.requests == []  # rejected, never truncated or sent
    assert api.app.state.limiter.active == 0


async def test_token_budget_boundary_through_the_api(api_factory):
    from app.services import ConservativeTokenEstimator  # noqa: PLC0415

    prompt = "Explain this function please: " + "word " * 40
    tokens = ConservativeTokenEstimator().count(prompt).tokens

    at_limit = await api_factory(ollama_handler(), api=ApiSettings(max_input_tokens=tokens))
    below = await api_factory(ollama_handler(), api=ApiSettings(max_input_tokens=tokens - 1))

    assert (await post_generate(at_limit.client, prompt)).status_code == 200
    assert_error(await post_generate(below.client, prompt), 422, "PROMPT_TOO_MANY_TOKENS")


async def test_character_limit_is_still_enforced_in_addition_to_the_token_budget(api_factory):
    api = await api_factory(ollama_handler(), api=ApiSettings(max_prompt_chars=50))

    assert_error(await post_generate(api.client, "a " * 40), 422, "PROMPT_TOO_LONG")


async def test_default_budget_is_context_minus_output_minus_margin(api_factory):
    api = await api_factory(ollama_handler())

    budget = api.app.state.budget
    assert (budget.total_context, budget.reserved_generation, budget.safety_margin) == (4096, 1024, 256)
    assert budget.input_limit == 2816


async def test_budget_follows_the_configured_ollama_limits(api_factory):
    api = await api_factory(ollama_handler(), num_ctx=8192, num_predict=512)

    assert api.app.state.budget.input_limit == 8192 - 512 - 256


def test_impossible_context_arithmetic_fails_fast_at_startup():
    with pytest.raises(ConfigError, match="no room"):
        create_app(OllamaSettings(num_ctx=1000, num_predict=900), ApiSettings())
    with pytest.raises(ConfigError, match="exceeds"):
        create_app(OllamaSettings(), ApiSettings(max_input_tokens=3000))


async def test_underestimated_prompt_is_noticed_from_ollamas_real_counts(api_factory, caplog):
    # Estimate is ~3 tokens, but Ollama reports 900 evaluated: the audit must say so.
    api = await api_factory(ollama_handler(prompt_eval_count=900))

    with caplog.at_level(logging.WARNING):
        response = await post_generate(api.client, "hi there")

    assert response.status_code == 200  # advisory only, the answer is still returned
    assert any("UNDER-estimated" in r.getMessage() for r in caplog.records)


async def test_context_overflow_is_flagged_when_prompt_plus_output_exceed_the_window(api_factory, caplog):
    api = await api_factory(ollama_handler(prompt_eval_count=3500))

    with caplog.at_level(logging.WARNING):
        await post_generate(api.client, "hi there")

    assert any("exceeds the 4096-token context window" in r.getMessage() for r in caplog.records)


async def test_consistent_token_counts_raise_no_audit_warnings(api_factory, caplog):
    api = await api_factory(ollama_handler(prompt_eval_count=31))  # 29 template + 2 real prompt tokens

    with caplog.at_level(logging.WARNING):
        await post_generate(api.client, "hi there")

    assert not [r for r in caplog.records if "estimat" in r.getMessage() or "context window" in r.getMessage()]
