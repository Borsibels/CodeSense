"""CORS (incl. unexpected 500s), Host/body/content-type guards, and local-API security properties."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from app.config import ApiSettings, ConfigError

pytestmark = pytest.mark.anyio

ALLOWED = "http://127.0.0.1:5173"
EVIL = "http://evil.example"
APP_DIR = Path(__file__).resolve().parent.parent / "app"


def ollama_ok(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/version":
        return httpx.Response(200, json={"version": "0.40.2"})
    if path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:3b"}]})
    return httpx.Response(200, json={"response": "ok", "done": True})


def add_boom_route(app):
    @app.get("/test-only/boom")
    async def boom():
        raise RuntimeError(r"secret C:\internal\path and password=hunter2")


# --------------------------------------------------------------------------- #
# CORS: allow-list, never wildcard, and the same headers on unexpected 500s
# --------------------------------------------------------------------------- #
async def test_unexpected_500_carries_cors_headers_for_an_allowed_origin(api_factory):
    api = await api_factory(ollama_ok)
    add_boom_route(api.app)

    response = await api.client.get("/test-only/boom", headers={"Origin": ALLOWED})

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == ALLOWED
    body = response.json()
    assert body == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred in the backend. Check the server log for details.",
        }
    }
    assert "secret" not in response.text and "hunter2" not in response.text


async def test_unexpected_500_gets_no_cors_headers_for_a_disallowed_origin(api_factory):
    api = await api_factory(ollama_ok)
    add_boom_route(api.app)

    response = await api.client.get("/test-only/boom", headers={"Origin": EVIL})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "request_kwargs, status, code",
    [
        ({"method": "POST", "url": "/api/ai/generate", "json": {"prompt": "  "}}, 422, "INVALID_REQUEST"),
        ({"method": "POST", "url": "/api/ai/generate", "json": {"prompt": "x" * 7000}}, 422, "PROMPT_TOO_LONG"),
        ({"method": "GET", "url": "/api/missing"}, 404, "NOT_FOUND"),
        ({"method": "GET", "url": "/api/ai/generate"}, 405, "METHOD_NOT_ALLOWED"),
    ],
    ids=["validation", "too-long", "not-found", "wrong-method"],
)
async def test_expected_errors_follow_the_same_cors_rules(api_factory, request_kwargs, status, code):
    api = await api_factory(ollama_ok)

    allowed = await api.client.request(headers={"Origin": ALLOWED}, **request_kwargs)
    denied = await api.client.request(headers={"Origin": EVIL}, **request_kwargs)

    assert (allowed.status_code, allowed.json()["error"]["code"]) == (status, code)
    assert allowed.headers["access-control-allow-origin"] == ALLOWED
    assert (denied.status_code, denied.json()["error"]["code"]) == (status, code)
    assert "access-control-allow-origin" not in denied.headers


async def test_upstream_errors_through_the_exception_handlers_keep_cors_headers(api_factory):
    def down(request):
        raise httpx.ConnectError("refused")

    api = await api_factory(down)

    response = await api.client.post(
        "/api/ai/generate", json={"prompt": "hello"}, headers={"Origin": ALLOWED}
    )

    assert response.status_code == 503
    assert response.headers["access-control-allow-origin"] == ALLOWED


async def test_cors_is_an_exact_allow_list_without_wildcard_or_credentials(api_factory):
    api = await api_factory(ollama_ok)
    seen = set()

    for origin in (ALLOWED, "http://localhost:5173", EVIL, "http://127.0.0.1:5174", "null"):
        response = await api.client.get("/api/ai/health", headers={"Origin": origin})
        seen.add(response.headers.get("access-control-allow-origin"))
        assert "access-control-allow-credentials" not in response.headers

    assert "*" not in seen
    assert seen == {ALLOWED, "http://localhost:5173", None}


async def test_preflight_allows_only_configured_methods_and_headers(api_factory):
    api = await api_factory(ollama_ok)
    base = {"Origin": ALLOWED, "Access-Control-Request-Method": "POST"}

    ok = await api.client.options("/api/ai/generate", headers={**base, "Access-Control-Request-Headers": "content-type"})
    bad_header = await api.client.options("/api/ai/generate", headers={**base, "Access-Control-Request-Headers": "x-evil"})
    bad_method = await api.client.options(
        "/api/ai/generate", headers={"Origin": ALLOWED, "Access-Control-Request-Method": "DELETE"}
    )
    bad_origin = await api.client.options("/api/ai/generate", headers={**base, "Origin": EVIL})

    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == ALLOWED
    assert set(ok.headers["access-control-allow-methods"].replace(" ", "").split(",")) == {"GET", "POST"}
    assert bad_header.status_code == 400
    assert bad_method.status_code == 400
    assert bad_origin.status_code == 400 and "access-control-allow-origin" not in bad_origin.headers


async def test_cors_origins_are_configurable_and_can_be_emptied(api_factory):
    custom = await api_factory(ollama_ok, api=ApiSettings(cors_origins=("http://localhost:3000",)))
    none = await api_factory(ollama_ok, api=ApiSettings(cors_origins=()))

    a = await custom.client.get("/api/ai/health", headers={"Origin": "http://localhost:3000"})
    b = await custom.client.get("/api/ai/health", headers={"Origin": ALLOWED})
    c = await none.client.get("/api/ai/health", headers={"Origin": ALLOWED})

    assert a.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in b.headers
    assert "access-control-allow-origin" not in c.headers


# --------------------------------------------------------------------------- #
# Host header guard (DNS-rebinding defence)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8000", "localhost", "LOCALHOST:5173", "[::1]:8000"])
async def test_loopback_hosts_are_accepted(api_factory, host):
    api = await api_factory(ollama_ok)

    response = await api.client.get("/api/ai/health", headers={"Host": host})

    assert response.status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "127.0.0.1.evil.example", "192.168.1.20:8000", ""])
async def test_foreign_host_headers_are_refused_with_the_error_envelope(api_factory, host):
    api = await api_factory(ollama_ok)

    response = await api.client.get("/api/ai/health", headers={"Host": host})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_HOST"
    assert api.transport.requests == []  # refused before reaching any handler


async def test_allowed_hosts_are_configurable(api_factory):
    lan = await api_factory(ollama_ok, api=ApiSettings(allowed_hosts=("192.168.1.20",)))
    anyhost = await api_factory(ollama_ok, api=ApiSettings(allowed_hosts=("*",)))

    assert (await lan.client.get("/api/ai/health", headers={"Host": "192.168.1.20:8000"})).status_code == 200
    assert (await lan.client.get("/api/ai/health", headers={"Host": "127.0.0.1"})).status_code == 400
    assert (await anyhost.client.get("/api/ai/health", headers={"Host": "anything.example"})).status_code == 200


def test_allowed_hosts_from_environment_are_normalised():
    settings = ApiSettings.from_env({"API_ALLOWED_HOSTS": " Localhost , 127.0.0.1 "})

    assert settings.allowed_hosts == ("localhost", "127.0.0.1")
    with pytest.raises(ConfigError):
        ApiSettings(allowed_hosts=())


# --------------------------------------------------------------------------- #
# Content type and body size guards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/plain"},  # a CORS "simple request" a hostile page could send
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"Content-Type": "multipart/form-data; boundary=x"},
        {},
    ],
    ids=["text-plain", "form", "multipart", "none"],
)
async def test_non_json_bodies_never_reach_a_handler(api_factory, headers):
    api = await api_factory(ollama_ok)

    response = await api.client.post(
        "/api/ai/generate", content=json.dumps({"prompt": "hello"}).encode(), headers=headers
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert api.transport.requests == []


async def test_json_with_charset_is_accepted_and_empty_post_is_a_validation_error(api_factory):
    api = await api_factory(ollama_ok)

    ok = await api.client.post(
        "/api/ai/generate",
        content=b'{"prompt": "hello"}',
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    empty = await api.client.post("/api/ai/generate")

    assert ok.status_code == 200
    assert empty.status_code == 422  # no body at all is not a media-type problem


async def test_declared_oversized_body_is_refused_without_being_processed(api_factory):
    api = await api_factory(ollama_ok, api=ApiSettings(max_body_bytes=2048))
    body = json.dumps({"prompt": "x" * 5000}).encode()

    response = await api.client.post(
        "/api/ai/generate", content=body, headers={"Content-Type": "application/json", "Origin": ALLOWED}
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    assert response.headers["access-control-allow-origin"] == ALLOWED  # browsers can read the reason
    assert api.transport.requests == []


async def test_streamed_oversized_body_is_cut_off_without_a_content_length(api_factory):
    api = await api_factory(ollama_ok, api=ApiSettings(max_body_bytes=2048))

    async def chunks():
        yield b'{"prompt": "'
        for _ in range(10):
            yield b"x" * 1000  # 10 kB in total, no Content-Length header
        yield b'"}'

    response = await api.client.post(
        "/api/ai/generate", content=chunks(), headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    assert api.transport.requests == []


async def test_body_just_under_the_cap_is_processed(api_factory):
    api = await api_factory(ollama_ok, api=ApiSettings(max_body_bytes=2048, max_prompt_chars=1500))
    body = json.dumps({"prompt": "hello " * 200}).encode()
    assert len(body) < 2048

    response = await api.client.post(
        "/api/ai/generate", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 200


def test_body_cap_has_a_sane_floor():
    with pytest.raises(ConfigError):
        ApiSettings(max_body_bytes=10)


# --------------------------------------------------------------------------- #
# Untrusted callers cannot change the model or inference settings
# --------------------------------------------------------------------------- #
async def test_callers_cannot_override_model_or_options_by_any_channel(api_factory):
    api = await api_factory(ollama_ok)

    by_body = await api.client.post("/api/ai/generate", json={"prompt": "hi", "model": "llama3:70b"})
    by_options = await api.client.post(
        "/api/ai/generate", json={"prompt": "hi", "options": {"num_predict": 99999, "num_ctx": 131072}}
    )
    by_format = await api.client.post("/api/ai/generate", json={"prompt": "hi", "format": "json"})
    by_query = await api.client.post(
        "/api/ai/generate?model=llama3:70b&num_predict=99999&temperature=2", json={"prompt": "hi"}
    )
    by_headers = await api.client.post(
        "/api/ai/generate",
        json={"prompt": "hi"},
        headers={"X-Model": "llama3:70b", "OLLAMA_MODEL": "llama3:70b", "X-Num-Predict": "99999"},
    )

    assert by_body.status_code == by_options.status_code == by_format.status_code == 422
    assert by_query.status_code == by_headers.status_code == 200
    sent = [json.loads(r.content) for r in api.transport.requests if r.url.path == "/api/generate"]
    assert len(sent) == 2  # only the query/header attempts got through, and they changed nothing
    for payload in sent:
        assert payload["model"] == "qwen2.5-coder:3b"
        assert payload["options"] == {"num_ctx": 4096, "num_predict": 1024, "temperature": 0.2}
        assert "format" not in payload


# --------------------------------------------------------------------------- #
# Attack surface: no filesystem, no command execution
# --------------------------------------------------------------------------- #
async def test_the_api_exposes_exactly_the_intended_routes(api_factory):
    api = await api_factory(ollama_ok)

    spec = (await api.client.get("/openapi.json")).json()["paths"]

    assert {path: sorted(methods) for path, methods in spec.items()} == {
        "/api/ai/health": ["get"],
        "/api/ai/generate": ["post"],
        "/api/ai/analyze": ["post"],
        "/api/projects/inspect": ["post"],
        "/api/projects/context": ["post"],
    }


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/files"),
        ("GET", "/api/files/../../config.py"),
        ("POST", "/api/exec"),
        ("POST", "/api/shell"),
        ("GET", "/static/swagger/LICENSE"),
        ("GET", "/app/config.py"),
        ("GET", "/.env"),
        ("PUT", "/api/ai/generate"),
        ("DELETE", "/api/ai/generate"),
        ("GET", "/redoc"),
    ],
)
async def test_nothing_else_is_reachable(api_factory, method, path):
    api = await api_factory(ollama_ok)

    response = await api.client.request(method, path)

    assert response.status_code in (404, 405)
    assert response.json()["error"]["code"] in ("NOT_FOUND", "METHOD_NOT_ALLOWED")


def test_app_source_has_no_command_execution_or_general_filesystem_access():
    forbidden = re.compile(
        r"\b(?:subprocess|os\.system|os\.popen|os\.exec\w*|os\.spawn\w*|shutil|pickle|marshal|ctypes|"
        r"importlib|tempfile|glob|os\.(?:remove|unlink|rmdir|listdir|walk|scandir)|StaticFiles|UploadFile)\b"
        r"|(?<![\w.])(?:eval|exec|compile|__import__)\("  # builtins only: re.compile( is fine
    )
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if forbidden.search(code) and not code.strip().startswith(('"', "'")):
                offenders.append(f"{path.relative_to(APP_DIR)}:{number}: {line.strip()}")
    assert offenders == []


def test_only_the_docs_module_reads_files_from_disk():
    readers = {
        str(p.relative_to(APP_DIR))
        for p in APP_DIR.rglob("*.py")
        if re.search(r"\bFileResponse\b|\bopen\(|\.read_text\(|\.read_bytes\(", p.read_text(encoding="utf-8"))
    }
    # project_ingestion calls ZipFile.open on an in-memory io.BytesIO; it never touches the disk
    # (test_project_security.py asserts that separately).
    assert readers == {str(Path("api") / "docs.py"), str(Path("services") / "project_ingestion.py")}


async def test_declared_oversized_body_is_refused_before_a_single_byte_is_read():
    from app.api.middleware import RequestGuardMiddleware  # noqa: PLC0415

    inner_called = False

    async def inner(scope, receive, send):
        nonlocal inner_called
        inner_called = True

    async def receive():
        raise AssertionError("the body must not be read when Content-Length already exceeds the cap")

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/ai/generate",
        "headers": [(b"content-length", b"999999999"), (b"content-type", b"application/json")],
    }

    await RequestGuardMiddleware(inner, max_body_bytes=2048)(scope, receive, send)

    assert sent[0]["status"] == 413
    assert inner_called is False
