"""POST /api/projects/inspect and /api/projects/context through the real FastAPI app."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading

import httpx
import pytest

from app.config import ApiSettings, ProjectSettings
from project_fixtures import (
    IGNORED_PROJECT,
    MIXED_PROJECT,
    PYTHON_PROJECT,
    WEB_PROJECT,
    big_python_project,
    make_encrypted_zip,
    make_symlink_zip,
    make_zip,
    make_zip_bomb,
)

pytestmark = pytest.mark.anyio

ZIP = {"Content-Type": "application/zip"}
INSPECT = "/api/projects/inspect"
CONTEXT = "/api/projects/context"


def no_ollama(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Ollama must not be contacted by the project endpoints: {request.url}")


async def post(api, path, files=None, *, data=None, headers=ZIP, params=None):
    content = data if data is not None else make_zip(files)
    return await api.client.post(path, content=content, headers=headers, params=params)


def error_code(response) -> str:
    body = response.json()
    assert set(body) == {"error"} and {"code", "message"} <= set(body["error"])
    return body["error"]["code"]


# --------------------------------------------------------------------------- #
# /inspect
# --------------------------------------------------------------------------- #
async def test_inspect_returns_files_symbols_and_graph(api_factory):
    api = await api_factory(no_ollama)

    response = await post(api, INSPECT, MIXED_PROJECT)

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["source_files"] == 9
    assert body["summary"]["languages"] == {"css": 2, "html": 1, "javascript": 2, "python": 4}
    assert body["summary"]["excluded_files"] == 2
    files = {f["path"]: f for f in body["files"]}
    assert [f["path"] for f in body["files"]] == sorted(files)
    main = files["app/main.py"]
    assert main["confidence"] == "confirmed" and main["parser"] == "python-ast"
    assert main["has_main_guard"] is True
    assert {s["qualified_name"] for s in main["symbols"]} == {"run"}
    assert files["web/js/app.js"]["confidence"] == "heuristic"
    assert {e["path"]: e["reason"] for e in map(lambda x: x, body["excluded"])} == {
        "README.md": "unsupported_extension",
        "web/img/logo.png": "unsupported_extension",
    }
    graph = body["graph"]
    statuses = {(e["source"], e["specifier"]): e["status"] for e in graph["edges"]}
    assert statuses[("app/main.py", "app.utils")] == "resolved"
    assert statuses[("app/main.py", "requests")] == "external"
    assert statuses[("web/index.html", "js/missing.js")] == "missing"
    assert [e["path"] for e in graph["entry_points"]] == ["app/main.py", "web/index.html"]
    assert "not a" in body["notice"] and "Nothing was executed" in body["notice"]


async def test_inspect_is_deterministic(api_factory):
    api = await api_factory(no_ollama)
    first = (await post(api, INSPECT, WEB_PROJECT)).text
    second = (await post(api, INSPECT, WEB_PROJECT)).text
    assert first == second


async def test_inspect_does_not_send_source_code_back(api_factory):
    api = await api_factory(no_ollama)
    text = (await post(api, INSPECT, PYTHON_PROJECT)).text
    assert 'return text.upper() + "!"' not in text


async def test_inspect_reports_syntax_errors_without_failing(api_factory):
    api = await api_factory(no_ollama)
    body = (await post(api, INSPECT, {"bad.py": "def broken(:\n", "ok.py": "x = 1\n"})).json()
    bad = next(f for f in body["files"] if f["path"] == "bad.py")
    assert bad["has_syntax_error"] is True
    assert bad["diagnostics"][0]["code"] == "syntax_error" and bad["diagnostics"][0]["line"] == 1
    assert body["summary"]["diagnostics"]["error"] == 1


async def test_inspect_lists_ignored_and_binary_files_with_reasons(api_factory):
    api = await api_factory(no_ollama)
    body = (await post(api, INSPECT, IGNORED_PROJECT)).json()
    assert [f["path"] for f in body["files"]] == ["main.py"]
    reasons = {e["path"]: e["reason"] for e in body["excluded"]}
    assert reasons["node_modules/lib/index.js"] == "ignored_directory"
    assert reasons["static/app.min.js"] == "minified"
    assert len(reasons) == len(IGNORED_PROJECT) - 1


async def test_a_project_with_no_supported_files_is_a_valid_empty_result(api_factory):
    api = await api_factory(no_ollama)
    response = await post(api, INSPECT, {"README.md": "# hi\n"})
    assert response.status_code == 200
    assert response.json()["files"] == [] and response.json()["summary"]["source_files"] == 0


# --------------------------------------------------------------------------- #
# /context
# --------------------------------------------------------------------------- #
async def test_context_overview_needs_no_file(api_factory):
    api = await api_factory(no_ollama)

    response = await post(api, CONTEXT, MIXED_PROJECT)

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "overview" and body["target_file"] is None
    assert body["context"].startswith("SIFT PROJECT CONTEXT")
    assert body["budget"] == {
        "input_limit": 2816,
        "instruction_reserve": 700,
        "context_limit": 2116,
        "estimated_tokens": body["budget"]["estimated_tokens"],
        "remaining_tokens": 2116 - body["budget"]["estimated_tokens"],
        "is_estimate": True,
    }
    assert 0 < body["budget"]["estimated_tokens"] <= 2116
    assert body["coverage_summary"]["files_total"] == 9 == len(body["files"])
    assert body["project"]["source_files"] == 9


async def test_context_for_a_file_and_symbol(api_factory):
    api = await api_factory(no_ollama)

    response = await post(api, CONTEXT, PYTHON_PROJECT, params={"file": "app/models.py", "symbol": "User.greet", "intent": "debug"})

    assert response.status_code == 200
    body = response.json()
    assert (body["intent"], body["target_file"], body["target_symbols"]) == ("debug", "app/models.py", ["User.greet"])
    models = next(f for f in body["files"] if f["path"] == "app/models.py")
    assert models["status"] == "partial"
    assert {"start_line": 10, "end_line": 11} in models["included_ranges"]
    assert models["omitted_ranges"]
    assert 'return helper("hello " + self.name)' in body["context"]


async def test_context_coverage_for_an_oversized_project(api_factory):
    api = await api_factory(no_ollama)
    body = (await post(api, CONTEXT, big_python_project(), params={"file": "pipeline.py", "intent": "explain"})).json()
    pipeline = next(f for f in body["files"] if f["path"] == "pipeline.py")
    assert pipeline["status"] == "partial" and pipeline["line_count"] == 758
    assert body["omitted_regions"] and body["omitted_region_total"] >= len(body["omitted_regions"])
    assert body["budget"]["estimated_tokens"] <= body["budget"]["context_limit"]


async def test_the_instruction_reserve_can_be_adjusted_per_request(api_factory):
    api = await api_factory(no_ollama)
    body = (await post(api, CONTEXT, big_python_project(), params={"file": "pipeline.py", "intent": "explain", "instruction_reserve_tokens": 1800})).json()
    assert body["budget"]["instruction_reserve"] == 1800 and body["budget"]["context_limit"] == 1016
    assert body["budget"]["estimated_tokens"] <= 1016


@pytest.mark.parametrize(
    "params, status, code",
    [
        ({"intent": "explain"}, 422, "FILE_REQUIRED"),
        ({"intent": "debug"}, 422, "FILE_REQUIRED"),
        ({"symbol": "run"}, 422, "FILE_REQUIRED"),
        ({"file": "nope.py", "intent": "explain"}, 422, "FILE_NOT_FOUND"),
        ({"file": "README.md", "intent": "explain"}, 422, "FILE_NOT_ANALYZED"),
        ({"file": "app/main.py", "symbol": "ghost", "intent": "explain"}, 422, "SYMBOL_NOT_FOUND"),
        ({"intent": "refactor"}, 422, "INVALID_REQUEST"),
        ({"instruction_reserve_tokens": -1}, 422, "INVALID_REQUEST"),
        ({"instruction_reserve_tokens": 2800}, 422, "INVALID_REQUEST"),
        ({"file": "x" * 400}, 422, "INVALID_REQUEST"),
    ],
)
async def test_context_request_errors_use_the_standard_envelope(api_factory, params, status, code):
    api = await api_factory(no_ollama)
    response = await post(api, CONTEXT, PYTHON_PROJECT, params=params)
    assert response.status_code == status
    assert error_code(response) == code


async def test_query_validation_errors_name_the_field(api_factory):
    api = await api_factory(no_ollama)
    response = await post(api, CONTEXT, PYTHON_PROJECT, params={"intent": "refactor"})
    assert response.json()["error"]["details"][0]["field"] == "intent"


# --------------------------------------------------------------------------- #
# Invalid and hostile uploads
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [INSPECT, CONTEXT])
@pytest.mark.parametrize(
    "data, status, code",
    [
        (b"", 422, "INVALID_ARCHIVE"),
        (b"not a zip", 422, "INVALID_ARCHIVE"),
        (b"PK\x05\x06" + b"\x00" * 18, 422, "EMPTY_ARCHIVE"),
        (make_symlink_zip(), 422, "UNSUPPORTED_ENTRY"),
        (make_encrypted_zip(), 422, "ENCRYPTED_ARCHIVE"),
        (make_zip({"../evil.py": "x"}), 422, "UNSAFE_PATH"),
        (make_zip({"a.py": "x", "A.py": "y"}), 422, "DUPLICATE_PATH"),
        (make_zip_bomb(4), 422, "SUSPICIOUS_COMPRESSION"),
        (make_zip({f"f{i}.py": "" for i in range(501)}), 422, "TOO_MANY_ENTRIES"),
    ],
    ids=["empty", "garbage", "no-entries", "symlink", "encrypted", "traversal", "case-collision", "zip-bomb", "too-many"],
)
async def test_bad_archives_get_clear_consistent_errors(api_factory, path, data, status, code):
    api = await api_factory(no_ollama)
    response = await post(api, path, data=data)
    assert response.status_code == status
    assert error_code(response) == code
    assert response.json()["error"]["message"]


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "multipart/form-data; boundary=x", "application/x-www-form-urlencoded"])
async def test_wrong_content_types_are_refused_before_the_handler(api_factory, content_type):
    api = await api_factory(no_ollama)
    response = await post(api, INSPECT, data=make_zip(PYTHON_PROJECT), headers={"Content-Type": content_type})
    assert response.status_code == 415 and error_code(response) == "UNSUPPORTED_MEDIA_TYPE"
    assert "ZIP" in response.json()["error"]["message"]


@pytest.mark.parametrize("content_type", ["application/zip", "application/x-zip-compressed", "application/octet-stream", "Application/ZIP; charset=binary"])
async def test_accepted_content_types(api_factory, content_type):
    api = await api_factory(no_ollama)
    response = await post(api, INSPECT, data=make_zip(PYTHON_PROJECT), headers={"Content-Type": content_type})
    assert response.status_code == 200


async def test_a_wrong_extension_or_name_does_not_matter_only_the_bytes_do(api_factory):
    api = await api_factory(no_ollama)
    assert (await post(api, INSPECT, data=b"MZ\x90\x00 pretending to be a zip", headers={"Content-Type": "application/zip"})).status_code == 422


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
async def test_only_post_is_allowed(api_factory, method):
    api = await api_factory(no_ollama)
    response = await api.client.request(method, INSPECT)
    assert response.status_code == 405 and error_code(response) == "METHOD_NOT_ALLOWED"


# --------------------------------------------------------------------------- #
# Size limits: route-specific, the global limit is untouched
# --------------------------------------------------------------------------- #
async def test_declared_oversized_uploads_are_refused_with_413(api_factory):
    api = await api_factory(no_ollama, project=ProjectSettings(max_zip_bytes=2000))
    data = make_zip({"a.py": os.urandom(4000)}, compression=0)
    response = await post(api, INSPECT, data=data)
    assert response.status_code == 413 and error_code(response) == "REQUEST_TOO_LARGE"
    assert "2000" in response.json()["error"]["message"]


async def test_streamed_oversized_uploads_are_cut_off_too(api_factory):
    api = await api_factory(no_ollama, project=ProjectSettings(max_zip_bytes=2000))
    data = make_zip({"a.py": os.urandom(4000)}, compression=0)

    async def chunks():
        for i in range(0, len(data), 500):
            yield data[i : i + 500]

    response = await api.client.post(INSPECT, content=chunks(), headers=ZIP)  # chunked: no Content-Length
    assert response.status_code == 413 and error_code(response) == "REQUEST_TOO_LARGE"


async def test_zip_routes_accept_what_the_global_json_limit_rejects(api_factory):
    api = await api_factory(no_ollama)
    big = make_zip({"data.py": os.urandom(300_000)}, compression=0)  # 300 KB: > 128 KiB API_MAX_BODY_BYTES
    assert len(big) > ApiSettings().max_body_bytes
    # Too big for the 512 KiB source-file rule? No: 300 KB < 512 KiB, but not UTF-8, so excluded - yet accepted.
    response = await post(api, INSPECT, data=big)
    assert response.status_code == 200
    assert response.json()["excluded"][0]["reason"] in {"binary_content", "invalid_encoding"}


async def test_the_global_body_limit_still_applies_to_every_other_route(api_factory):
    api = await api_factory(no_ollama)
    response = await api.client.post("/api/ai/generate", json={"prompt": "x" * 200_000})
    assert response.status_code == 413
    # ...and a ZIP is still not accepted where JSON is expected.
    response = await api.client.post("/api/ai/generate", content=make_zip(PYTHON_PROJECT), headers=ZIP)
    assert response.status_code == 415


async def test_the_exact_size_limit_is_inclusive(api_factory):
    data = make_zip(PYTHON_PROJECT)
    api = await api_factory(no_ollama, project=ProjectSettings(max_zip_bytes=len(data)))
    assert (await post(api, INSPECT, data=data)).status_code == 200
    api2 = await api_factory(no_ollama, project=ProjectSettings(max_zip_bytes=len(data) - 1))
    assert (await post(api2, INSPECT, data=data)).status_code == 413


# --------------------------------------------------------------------------- #
# Independence from Ollama, no retention, no source in logs
# --------------------------------------------------------------------------- #
async def test_project_endpoints_never_contact_ollama(api_factory):
    api = await api_factory(no_ollama)
    await post(api, INSPECT, MIXED_PROJECT)
    await post(api, CONTEXT, MIXED_PROJECT, params={"file": "app/main.py", "intent": "debug"})
    await post(api, CONTEXT, data=b"garbage")
    assert api.transport.requests == []


async def test_project_endpoints_work_while_ollama_is_down(api_factory):
    def down(request):
        raise httpx.ConnectError("Ollama is not running")

    api = await api_factory(down)
    assert (await post(api, INSPECT, PYTHON_PROJECT)).status_code == 200
    assert (await api.client.get("/api/ai/health")).json()["status"] == "unavailable"
    assert (await post(api, CONTEXT, PYTHON_PROJECT)).status_code == 200


async def test_nothing_is_retained_or_written_after_a_request(api_factory, tmp_path, monkeypatch):
    # Point the temp directory at a private folder: anything the app wrote to "the" temp dir would
    # land here, and unrelated processes (other test runs) cannot make the comparison flaky.
    private_tmp = tmp_path / "tmp"
    private_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private_tmp))
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    api = await api_factory(no_ollama)
    state_before = set(vars(api.app.state))

    for _ in range(3):
        await post(api, INSPECT, MIXED_PROJECT)
        await post(api, CONTEXT, MIXED_PROJECT, params={"file": "app/main.py", "intent": "explain"})

    assert set(vars(api.app.state)) == state_before
    assert list(private_tmp.iterdir()) == []
    assert list(work.iterdir()) == []


async def test_uploaded_source_never_reaches_the_logs(api_factory, caplog):
    caplog.set_level(logging.DEBUG)
    api = await api_factory(no_ollama)
    secret = "SECRET_MARKER_8f3a"
    files = {
        f"{secret}_module.py": f'password = "{secret}"\n',
        "broken.py": f'x = "{secret}" +\n',
        "page.html": f"<script src='{secret}.js'></script>",
    }
    await post(api, INSPECT, files)
    await post(api, CONTEXT, files, params={"file": "broken.py", "intent": "debug"})
    await post(api, INSPECT, {f"../{secret}.py": "x = 1\n"})  # rejected archive with the marker in a name
    assert secret not in caplog.text


# --------------------------------------------------------------------------- #
# Resource limits
# --------------------------------------------------------------------------- #
async def test_processing_deadline_answers_504(api_factory, monkeypatch):
    import app.services.project_models as models  # noqa: PLC0415

    ticks = iter(range(0, 10_000))  # a fake clock that moves one second per reading
    monkeypatch.setattr(models.time, "perf_counter", lambda: float(next(ticks)))
    api = await api_factory(no_ollama, project=ProjectSettings(processing_timeout=0.5))
    response = await post(api, INSPECT, PYTHON_PROJECT)
    assert response.status_code == 504 and error_code(response) == "PROJECT_PROCESSING_TIMEOUT"


async def test_concurrent_analyses_are_limited_with_a_retryable_503(api_factory, monkeypatch):
    import app.api.projects as projects  # noqa: PLC0415

    started, release = threading.Event(), threading.Event()
    real = projects.analyze_project

    def slow(data, settings):
        started.set()
        release.wait(10)
        return real(data, settings)

    monkeypatch.setattr(projects, "analyze_project", slow)
    api = await api_factory(no_ollama, project=ProjectSettings(max_concurrent=1, queue_wait_timeout=0))

    first = asyncio.create_task(post(api, INSPECT, PYTHON_PROJECT))
    while not started.is_set():
        await asyncio.sleep(0.01)
    second = await post(api, INSPECT, PYTHON_PROJECT)
    release.set()

    assert second.status_code == 503 and error_code(second) == "PROJECT_BUSY"
    assert second.headers["retry-after"] == "5"
    assert (await first).status_code == 200


async def test_the_event_loop_stays_responsive_during_analysis(api_factory, monkeypatch):
    import app.api.projects as projects  # noqa: PLC0415

    started, release = threading.Event(), threading.Event()
    real = projects.analyze_project

    def slow(data, settings):
        started.set()
        release.wait(10)
        return real(data, settings)

    monkeypatch.setattr(projects, "analyze_project", slow)
    api = await api_factory(no_ollama)
    task = asyncio.create_task(post(api, INSPECT, PYTHON_PROJECT))
    while not started.is_set():
        await asyncio.sleep(0.01)

    health = await asyncio.wait_for(api.client.get("/openapi.json"), 5)  # served while analysis is "running"
    release.set()

    assert health.status_code == 200
    assert (await task).status_code == 200


# --------------------------------------------------------------------------- #
# Browser-facing behaviour and documentation
# --------------------------------------------------------------------------- #
async def test_cors_allows_the_frontend_origin_for_zip_uploads(api_factory):
    api = await api_factory(no_ollama)
    origin = "http://127.0.0.1:5173"

    preflight = await api.client.options(
        INSPECT,
        headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
    )
    actual = await post(api, INSPECT, PYTHON_PROJECT, headers={**ZIP, "Origin": origin})

    assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == origin
    assert actual.headers["access-control-allow-origin"] == origin
    error = await post(api, INSPECT, data=b"junk", headers={**ZIP, "Origin": origin})
    assert error.status_code == 422 and error.headers["access-control-allow-origin"] == origin


async def test_foreign_host_headers_are_still_refused(api_factory):
    api = await api_factory(no_ollama)
    response = await post(api, INSPECT, PYTHON_PROJECT, headers={**ZIP, "Host": "evil.example"})
    assert response.status_code == 400 and error_code(response) == "INVALID_HOST"


async def test_openapi_documents_the_zip_body_and_response_models(api_factory):
    api = await api_factory(no_ollama)
    spec = (await api.client.get("/openapi.json")).json()
    for path in (INSPECT, CONTEXT):
        operation = spec["paths"][path]["post"]
        assert "application/zip" in operation["requestBody"]["content"]
        assert {"200", "413", "415", "422", "503", "504"} <= set(operation["responses"])
    assert {p["name"] for p in spec["paths"][CONTEXT]["post"]["parameters"]} == {
        "file", "symbol", "intent", "instruction_reserve_tokens",
    }
    assert "InspectResponse" in spec["components"]["schemas"] and "ContextResponse" in spec["components"]["schemas"]


async def test_existing_ai_endpoints_are_unchanged(api_factory):
    def ok(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.2"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:3b"}]})
        return httpx.Response(200, json={"response": "hi", "done": True})

    api = await api_factory(ok)
    assert (await api.client.get("/api/ai/health")).json()["status"] == "ready"
    assert (await api.client.post("/api/ai/generate", json={"prompt": "hello"})).json()["response"] == "hi"
