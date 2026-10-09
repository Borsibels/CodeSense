"""Tests for OllamaService using mocked HTTP responses (Ollama need not run)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.config import OllamaSettings
from app.services import (
    OllamaConnectTimeoutError,
    OllamaError,
    OllamaHTTPError,
    OllamaInferenceError,
    OllamaInvalidPromptError,
    OllamaModelNotFoundError,
    OllamaResponseError,
    OllamaService,
    OllamaTimeoutError,
    OllamaUnavailableError,
)

pytestmark = pytest.mark.anyio


def tags(*names: str) -> dict:
    return {"models": [{"name": n, "model": n} for n in names]}


def generated(text: str = "Line 1 creates a list.", **extra) -> dict:
    return {"model": "qwen2.5-coder:3b", "response": text, "done": True, **extra}


def respond(body=None, status: int = 200):
    """Handler that always answers with a JSON body."""
    return lambda request: httpx.Response(status, json=body)


def raises(exc: BaseException):
    def handler(request: httpx.Request):
        raise exc

    return handler


# --------------------------------------------------------------------------- #
# health_check
# --------------------------------------------------------------------------- #
async def test_health_check_healthy_uses_version_endpoint_without_inference(service_factory):
    service, transport = service_factory(respond({"version": "0.12.3"}))

    health = await service.health_check()

    assert health.healthy is True
    assert health.version == "0.12.3"
    assert [(r.method, r.url.path) for r in transport.requests] == [("GET", "/api/version")]


@pytest.mark.parametrize(
    "handler",
    [
        raises(httpx.ConnectError("connection refused")),
        raises(httpx.ConnectTimeout("timed out")),
        raises(httpx.ReadTimeout("timed out")),
        respond({"error": "boom"}, status=500),
        lambda request: httpx.Response(200, content=b"<html>not ollama</html>"),
        respond(["not", "an", "object"]),
    ],
    ids=["refused", "connect-timeout", "read-timeout", "http-500", "not-json", "json-not-object"],
)
async def test_health_check_reports_unhealthy_instead_of_raising(service_factory, handler):
    service, _ = service_factory(handler)

    health = await service.health_check()

    assert health.healthy is False
    assert health.version is None
    assert health.detail  # non-empty, user-safe explanation


async def test_health_check_detail_does_not_leak_raw_exception_text(service_factory):
    service, _ = service_factory(raises(httpx.ConnectError("[WinError 10061] secret-host-detail")))

    health = await service.health_check()

    assert "secret-host-detail" not in health.detail
    assert health.detail == OllamaUnavailableError.default_user_message


# --------------------------------------------------------------------------- #
# model_available
# --------------------------------------------------------------------------- #
async def test_model_available_true_when_installed(service_factory):
    service, transport = service_factory(respond(tags("llama3:8b", "qwen2.5-coder:3b")))

    assert await service.model_available() is True
    assert [(r.method, r.url.path) for r in transport.requests] == [("GET", "/api/tags")]


async def test_model_available_false_when_missing(service_factory):
    service, _ = service_factory(respond(tags("llama3:8b", "qwen2.5-coder:7b")))

    assert await service.model_available() is False


async def test_model_available_false_for_empty_model_list(service_factory):
    service, _ = service_factory(respond({"models": []}))

    assert await service.model_available() is False


async def test_model_available_treats_untagged_name_as_latest(service_factory):
    service, _ = service_factory(respond(tags("qwen2.5-coder:latest")), model="qwen2.5-coder")

    assert await service.model_available() is True


async def test_model_available_matches_on_model_field_too(service_factory):
    body = {"models": [{"model": "qwen2.5-coder:3b"}]}
    service, _ = service_factory(respond(body))

    assert await service.model_available() is True


async def test_model_available_connection_failure_raises_not_false(service_factory):
    service, _ = service_factory(raises(httpx.ConnectError("refused")))

    with pytest.raises(OllamaUnavailableError):
        await service.model_available()


async def test_model_available_http_error(service_factory):
    service, _ = service_factory(respond({"error": "internal"}, status=500))

    with pytest.raises(OllamaHTTPError) as excinfo:
        await service.model_available()
    assert excinfo.value.status_code == 500


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(200, content=b"not json"),
        respond([]),
        respond({}),
        respond({"models": "qwen2.5-coder:3b"}),
        respond({"models": ["qwen2.5-coder:3b"]}),
        respond({"models": [{"size": 1}]}),
    ],
    ids=["not-json", "list-body", "no-models-key", "models-not-list", "entry-not-dict", "no-name"],
)
async def test_model_available_malformed_response(service_factory, handler):
    service, _ = service_factory(handler)

    with pytest.raises(OllamaResponseError):
        await service.model_available()


# --------------------------------------------------------------------------- #
# generate: success + request shape
# --------------------------------------------------------------------------- #
async def test_generate_returns_text(service_factory):
    service, _ = service_factory(respond(generated("It prints 1..5.")))

    assert await service.generate("Explain this code") == "It prints 1..5."


async def test_generate_sends_explicit_model_and_generation_settings(service_factory):
    service, transport = service_factory(respond(generated()))

    await service.generate("Explain this code")

    (request,) = transport.requests
    assert (request.method, request.url.path) == ("POST", "/api/generate")
    assert json.loads(request.content) == {
        "model": "qwen2.5-coder:3b",
        "prompt": "Explain this code",
        "stream": False,
        "options": {"num_ctx": 4096, "num_predict": 1024, "temperature": 0.2},
    }


async def test_generate_uses_overridden_settings(service_factory):
    service, transport = service_factory(
        respond(generated()), model="other:1b", num_ctx=2048, num_predict=64, temperature=0.0
    )

    await service.generate("hi")

    body = json.loads(transport.requests[0].content)
    assert body["model"] == "other:1b"
    assert body["stream"] is False
    assert body["options"] == {"num_ctx": 2048, "num_predict": 64, "temperature": 0.0}


async def test_configured_timeouts_are_applied_per_request(service_factory):
    service, transport = service_factory(
        lambda request: httpx.Response(
            200, json=generated() if request.url.path == "/api/generate" else {"version": "1"}
        ),
        connect_timeout=1.0,
        read_timeout=77.0,
        write_timeout=2.0,
        pool_timeout=3.0,
        health_read_timeout=4.0,
    )

    await service.health_check()
    await service.generate("hi")

    health_req, generate_req = transport.requests
    assert health_req.extensions["timeout"] == {
        "connect": 1.0,
        "read": 4.0,
        "write": 2.0,
        "pool": 3.0,
    }
    assert generate_req.extensions["timeout"] == {
        "connect": 1.0,
        "read": 77.0,
        "write": 2.0,
        "pool": 3.0,
    }


async def test_generate_warns_when_output_hits_token_limit(service_factory, caplog):
    service, _ = service_factory(respond(generated(done_reason="length")))

    with caplog.at_level(logging.WARNING, logger="app.services.ollama_service"):
        assert await service.generate("hi") == "Line 1 creates a list."

    assert any("num_predict" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- #
# generate: validation and error mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prompt", ["", "   ", "\n\t", None, 123, b"bytes"])
async def test_generate_rejects_invalid_prompt_without_sending_request(service_factory, prompt):
    service, transport = service_factory(respond(generated()))

    with pytest.raises(OllamaInvalidPromptError) as excinfo:
        await service.generate(prompt)

    assert isinstance(excinfo.value, ValueError)
    assert transport.requests == []


async def test_generate_model_not_installed(service_factory):
    body = {"error": "model 'qwen2.5-coder:3b' not found"}
    service, _ = service_factory(respond(body, status=404))

    with pytest.raises(OllamaModelNotFoundError) as excinfo:
        await service.generate("hi")

    assert "ollama pull qwen2.5-coder:3b" in excinfo.value.user_message


async def test_generate_404_unrelated_to_model_is_a_plain_http_error(service_factory):
    service, _ = service_factory(lambda request: httpx.Response(404, text="404 page not found"))

    with pytest.raises(OllamaHTTPError) as excinfo:
        await service.generate("hi")

    assert not isinstance(excinfo.value, OllamaModelNotFoundError)
    assert excinfo.value.status_code == 404


async def test_generate_http_error_keeps_status_and_detail_for_logs(service_factory):
    service, _ = service_factory(respond({"error": "llama runner process has terminated"}, 500))

    with pytest.raises(OllamaHTTPError) as excinfo:
        await service.generate("hi")

    assert excinfo.value.status_code == 500
    assert "llama runner process has terminated" in str(excinfo.value)
    # ... but the frontend-safe message does not echo the server's internals.
    assert "llama runner" not in excinfo.value.user_message


async def test_generate_server_unavailable(service_factory):
    service, _ = service_factory(raises(httpx.ConnectError("refused")))

    with pytest.raises(OllamaUnavailableError) as excinfo:
        await service.generate("hi")

    assert not isinstance(excinfo.value, OllamaConnectTimeoutError)
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


async def test_generate_connect_timeout(service_factory):
    service, _ = service_factory(raises(httpx.ConnectTimeout("timed out")))

    with pytest.raises(OllamaConnectTimeoutError):
        await service.generate("hi")


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout("t"), httpx.WriteTimeout("t"), httpx.PoolTimeout("t")],
    ids=["read", "write", "pool"],
)
async def test_generate_timeout_after_connecting(service_factory, exc):
    service, _ = service_factory(raises(exc))

    with pytest.raises(OllamaTimeoutError) as excinfo:
        await service.generate("hi")

    # A generation timeout must not be reported as "server unavailable".
    assert not isinstance(excinfo.value, OllamaUnavailableError)


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(200, content=b"{not valid json"),
        lambda request: httpx.Response(200, content=b""),
        lambda request: httpx.Response(200, content=b"\xff\xfe"),
        respond(["response"]),
        respond("just a string"),
        respond({"done": True}),
        respond({"response": None, "done": True}),
        respond({"response": 42, "done": True}),
        respond({"response": "", "done": True}),
        respond({"response": "  \n", "done": True}),
        respond({"response": "partial", "done": False}),
    ],
    ids=[
        "invalid-json",
        "empty-body",
        "undecodable-bytes",
        "list-body",
        "string-body",
        "missing-response",
        "null-response",
        "non-string-response",
        "empty-response",
        "blank-response",
        "not-done",
    ],
)
async def test_generate_malformed_or_missing_response(service_factory, handler):
    service, _ = service_factory(handler)

    with pytest.raises(OllamaResponseError):
        await service.generate("hi")


async def test_generate_error_field_in_200_response(service_factory):
    service, _ = service_factory(respond({"error": "out of memory"}))

    with pytest.raises(OllamaInferenceError):
        await service.generate("hi")


async def test_generate_unexpected_failure_is_wrapped_and_not_leaked(service_factory):
    service, _ = service_factory(raises(RuntimeError(r"secret path C:\internal\thing")))

    with pytest.raises(OllamaInferenceError) as excinfo:
        await service.generate("hi")

    assert isinstance(excinfo.value.__cause__, RuntimeError)  # kept for debugging
    assert "secret" not in excinfo.value.user_message
    assert not isinstance(excinfo.value, (OllamaUnavailableError, OllamaTimeoutError))


async def test_every_failure_is_an_ollama_error(service_factory):
    handlers = [
        raises(httpx.ConnectError("x")),
        raises(httpx.ReadTimeout("x")),
        raises(httpx.RemoteProtocolError("x")),
        raises(ValueError("x")),
        respond({"error": "x"}, 500),
        respond({}),
    ]
    for handler in handlers:
        service, _ = service_factory(handler)
        with pytest.raises(OllamaError):
            await service.generate("hi")


# --------------------------------------------------------------------------- #
# generate_detailed (completion metadata)
# --------------------------------------------------------------------------- #
async def test_generate_detailed_reports_normal_completion(service_factory):
    service, _ = service_factory(respond(generated("done", done_reason="stop")))

    result = await service.generate_detailed("hi")

    assert (result.text, result.done_reason, result.truncated) == ("done", "stop", False)


async def test_generate_detailed_flags_truncation(service_factory):
    service, _ = service_factory(respond(generated("part", done_reason="length")))

    result = await service.generate_detailed("hi")

    assert (result.text, result.done_reason, result.truncated) == ("part", "length", True)


async def test_generate_detailed_tolerates_missing_or_odd_done_reason(service_factory):
    for body in (generated("a"), generated("a", done_reason=5)):
        service, _ = service_factory(respond(body))
        result = await service.generate_detailed("hi")
        assert (result.done_reason, result.truncated) == (None, False)


async def test_generate_still_returns_plain_text_even_when_truncated(service_factory):
    service, _ = service_factory(respond(generated("part", done_reason="length")))

    assert await service.generate("hi") == "part"


# --------------------------------------------------------------------------- #
# Resource lifecycle
# --------------------------------------------------------------------------- #
async def test_client_is_created_lazily_and_reused_across_calls(service_factory):
    service, transport = service_factory(
        lambda request: httpx.Response(
            200,
            json=generated()
            if request.url.path == "/api/generate"
            else tags("qwen2.5-coder:3b")
            if request.url.path == "/api/tags"
            else {"version": "1"},
        )
    )
    assert service._client is None

    await service.health_check()
    first_client = service._client
    await service.model_available()
    await service.generate("one")
    await service.generate("two")

    assert service._client is first_client
    assert len(transport.requests) == 4
    assert transport.closed is False


async def test_aclose_closes_client_and_transport_and_is_idempotent(service_factory):
    service, transport = service_factory(respond(generated()))
    await service.generate("hi")
    client = service._client
    assert client is not None and not client.is_closed

    await service.aclose()
    await service.aclose()  # second call must be a harmless no-op

    assert client.is_closed
    assert transport.closed is True
    assert service._client is None


async def test_async_context_manager_closes_resources_on_exit(service_factory):
    service, transport = service_factory(respond(generated()))

    async with service as entered:
        assert entered is service
        await service.generate("hi")
        assert transport.closed is False

    assert transport.closed is True


async def test_async_context_manager_closes_resources_when_body_raises(service_factory):
    service, transport = service_factory(respond(generated()))

    with pytest.raises(RuntimeError, match="boom"):
        async with service:
            await service.generate("hi")
            raise RuntimeError("boom")

    assert transport.closed is True


async def test_service_is_usable_again_after_aclose(service_factory):
    service, _ = service_factory(respond(generated("again")))
    await service.generate("hi")
    await service.aclose()

    assert await service.generate("hi") == "again"


def test_service_reads_settings_from_environment_when_not_given(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "from-env:1b")

    assert OllamaService().settings.model == "from-env:1b"


def test_service_base_url_comes_from_settings():
    service = OllamaService(OllamaSettings(base_url="http://localhost:9/"))

    assert service.settings.base_url == "http://localhost:9"
