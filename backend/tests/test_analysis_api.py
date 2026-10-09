"""POST /api/ai/analyze through the real FastAPI app with a mocked Ollama transport."""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
import pytest

from app.config import ApiSettings, ProjectSettings
from app.services.analysis_models import AnalysisResponse, CompactDebugDraft
from analysis_fixtures import INJECTION_PROJECT, OFF_BY_ONE
from project_fixtures import (
    IGNORED_PROJECT,
    PYTHON_PROJECT,
    make_encrypted_zip,
    make_symlink_zip,
    make_zip,
    make_zip_bomb,
)

pytestmark = pytest.mark.anyio

ZIP = {"Content-Type": "application/zip"}
ANALYZE = "/api/ai/analyze"

EXPLAIN_JSON = {
    "summary": "This file adds up prices and applies a discount.",
    "analogy": "",
    "sections": [{"file_path": "cart.py", "start_line": 4, "end_line": 9, "title": "Adding up", "description": "It goes through each price."}],
    "role_in_app": "It works out the bill.",
    "concept_name": "Loop",
    "concept_explanation": "A loop repeats the same work.",
    "assumptions": [],
}
DEBUG_JSON = {
    "summary": "One thing looks suspicious.",
    "findings": [
        {
            "file_path": "cart.py", "evidence": "7 |     for i in range(1, len(prices)):", "start_line": 7, "end_line": 7,
            "title": "Skips the first price", "category": "off_by_one", "severity": "medium", "confidence": "medium",
            "problem": "The loop may skip the first price.", "what_could_happen": "The total may be too low.",
            "likely_cause": "It starts counting at 1.", "suggestion": "Start at 0.",
        }
    ],
}


def ollama_reply(payload, **extra) -> httpx.Response:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(200, json={"response": text, "done": True, "done_reason": "stop", "prompt_eval_count": 700, "eval_count": 220, **extra})


def scripted(*replies):
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/generate":
            raise AssertionError(f"unexpected Ollama call {request.url.path}")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return handler


def no_ollama(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Ollama must not be contacted: {request.url}")


async def analyze(api, files=OFF_BY_ONE, *, data=None, headers=ZIP, **params):
    content = data if data is not None else make_zip(files)
    return await api.client.post(ANALYZE, content=content, headers=headers, params=params)


def error_code(response) -> str:
    body = response.json()
    assert set(body) == {"error"} and {"code", "message"} <= set(body["error"])
    return body["error"]["code"]


def ollama_bodies(api) -> list[dict]:
    return [json.loads(r.content) for r in api.transport.requests if r.url.path == "/api/generate"]


# --------------------------------------------------------------------------- #
# Success
# --------------------------------------------------------------------------- #
async def test_default_request_is_a_beginner_explanation(api_factory):
    api = await api_factory(scripted(ollama_reply(EXPLAIN_JSON)))

    response = await analyze(api, file_path="cart.py")

    assert response.status_code == 200
    body = response.json()
    AnalysisResponse.model_validate(body)
    assert (body["status"], body["intent"], body["depth"]) == ("completed", "explain", "beginner")
    assert body["summary"] == EXPLAIN_JSON["summary"]
    assert body["concept_to_learn"] == {"name": "Loop", "explanation": "A loop repeats the same work."}
    assert body["role_in_app"] == "It works out the bill." and body["analogy"] is None
    assert body["explanations"][0]["location_status"] == "in_context"
    assert body["findings"] == []
    assert body["notice"].startswith("AI-generated analysis")


async def test_response_is_flat_and_every_top_level_field_is_present(api_factory):
    api = await api_factory(scripted(ollama_reply(EXPLAIN_JSON)))
    body = (await analyze(api, file_path="cart.py")).json()
    assert set(body) == {
        "status", "notice", "intent", "depth", "target_file", "target_symbols", "summary", "analogy", "explanations",
        "role_in_app", "concept_to_learn", "findings", "assumptions", "glossary", "limitations", "coverage", "generation", "timings_ms",
        # Phase 4.5, additive and approved: debug_outcome, pattern_checks, relationships.
        "debug_outcome", "pattern_checks", "relationships",
    }
    assert "analysis" not in body


@pytest.mark.parametrize("depth", ["beginner", "intermediate", "advanced"])
async def test_each_depth_reaches_the_model_with_its_own_wording(api_factory, depth):
    technical = {"summary": "s", "sections": [], "assumptions": []}
    api = await api_factory(scripted(ollama_reply(EXPLAIN_JSON if depth == "beginner" else technical)))

    response = await analyze(api, file_path="cart.py", depth=depth)

    assert response.status_code == 200 and response.json()["depth"] == depth
    prompt = ollama_bodies(api)[0]["prompt"]
    assert ("never programmed" in prompt) == (depth == "beginner")
    assert ("experienced engineer" in prompt) == (depth == "advanced")


async def test_overview_needs_no_file(api_factory):
    api = await api_factory(scripted(ollama_reply({**EXPLAIN_JSON, "sections": []})))
    response = await analyze(api, PYTHON_PROJECT, intent="overview")
    assert response.status_code == 200 and response.json()["target_file"] is None


async def test_debug_returns_validated_findings(api_factory):
    api = await api_factory(scripted(ollama_reply(DEBUG_JSON)))

    body = (await analyze(api, file_path="cart.py", intent="debug")).json()

    [finding] = body["findings"]
    assert finding["id"] == "F1" and finding["verification"] == "source_verified"
    assert finding["file_path"] == "cart.py" and (finding["start_line"], finding["end_line"]) == (7, 7)
    assert finding["evidence"]["excerpt_matched"] is True
    assert body["explanations"] == []
    assert "does not prove a bug exists" in body["notice"]


async def test_symbol_selection_is_passed_through(api_factory):
    api = await api_factory(scripted(ollama_reply({**EXPLAIN_JSON, "sections": []})))
    body = (await analyze(api, file_path="cart.py", symbol="total_price")).json()
    assert body["target_symbols"] == ["total_price"]


async def test_the_model_and_its_options_are_fixed_by_the_server(api_factory):
    api = await api_factory(scripted(ollama_reply(EXPLAIN_JSON)))

    # Attempts to override the model or sampling through the query string are ignored.
    await analyze(api, file_path="cart.py", model="evil-model", temperature="2", num_predict="99999", num_ctx="999999")

    [sent] = ollama_bodies(api)
    assert sent["model"] == "qwen2.5-coder:3b"
    assert sent["options"] == {"num_ctx": 4096, "num_predict": 1024, "temperature": 0.2}
    assert sent["stream"] is False
    assert sent["format"]["type"] == "object" and "summary" in sent["format"]["properties"]


async def test_works_without_any_browser_origin_or_cors_configuration(api_factory):
    api = await api_factory(scripted(ollama_reply(EXPLAIN_JSON)), api=ApiSettings(cors_origins=()))
    assert (await analyze(api, file_path="cart.py")).status_code == 200


# --------------------------------------------------------------------------- #
# Request validation (no Ollama traffic for any of these)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "params, status, code",
    [
        ({}, 422, "FILE_REQUIRED"),  # default intent is explain
        ({"intent": "explain"}, 422, "FILE_REQUIRED"),
        ({"intent": "debug"}, 422, "FILE_REQUIRED"),
        ({"intent": "overview", "symbol": "x"}, 422, "FILE_REQUIRED"),
        ({"file_path": "  "}, 422, "FILE_REQUIRED"),
        ({"file_path": "nope.py"}, 422, "FILE_NOT_FOUND"),
        ({"file_path": "../cart.py"}, 422, "FILE_NOT_FOUND"),
        ({"file_path": "cart.py", "symbol": "ghost"}, 422, "SYMBOL_NOT_FOUND"),
        ({"file_path": "cart.py", "intent": "refactor"}, 422, "INVALID_REQUEST"),
        ({"file_path": "cart.py", "depth": "expert"}, 422, "INVALID_REQUEST"),
        ({"file_path": "cart.py", "depth": "BEGINNER"}, 422, "INVALID_REQUEST"),
        ({"file_path": "x" * 301}, 422, "INVALID_REQUEST"),
        ({"file_path": "cart.py", "symbol": "s" * 201}, 422, "INVALID_REQUEST"),
    ],
)
async def test_request_errors_use_the_standard_envelope_without_touching_the_model(api_factory, params, status, code):
    api = await api_factory(no_ollama)
    response = await analyze(api, **params)
    assert response.status_code == status and error_code(response) == code


async def test_validation_errors_name_the_field(api_factory):
    api = await api_factory(no_ollama)
    response = await analyze(api, file_path="cart.py", depth="expert")
    assert response.json()["error"]["details"][0]["field"] == "depth"


async def test_an_excluded_file_cannot_be_analysed(api_factory):
    api = await api_factory(no_ollama)
    response = await analyze(api, IGNORED_PROJECT, file_path="node_modules/lib/index.js")
    assert response.status_code == 422 and error_code(response) == "FILE_NOT_ANALYZED"


async def test_an_archive_without_supported_source_is_a_clear_client_error(api_factory):
    api = await api_factory(no_ollama)
    response = await analyze(api, {"README.md": "# docs only\n"}, intent="overview")
    assert response.status_code == 422 and error_code(response) == "NO_ANALYZABLE_SOURCE"
    assert response.json()["error"]["message"]


@pytest.mark.parametrize(
    "data, code",
    [
        (b"", "INVALID_ARCHIVE"),
        (b"not a zip", "INVALID_ARCHIVE"),
        (make_symlink_zip(), "UNSUPPORTED_ENTRY"),
        (make_encrypted_zip(), "ENCRYPTED_ARCHIVE"),
        (make_zip({"../evil.py": "x"}), "UNSAFE_PATH"),
        (make_zip_bomb(4), "SUSPICIOUS_COMPRESSION"),
        (make_zip({f"f{i}.py": "" for i in range(501)}), "TOO_MANY_ENTRIES"),
    ],
    ids=["empty", "garbage", "symlink", "encrypted", "traversal", "zip-bomb", "too-many"],
)
async def test_hostile_archives_are_rejected_exactly_like_phase_3(api_factory, data, code):
    api = await api_factory(no_ollama)
    response = await analyze(api, data=data, file_path="cart.py")
    assert response.status_code == 422 and error_code(response) == code


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "multipart/form-data; boundary=x"])
async def test_only_zip_content_types_are_accepted(api_factory, content_type):
    api = await api_factory(no_ollama)
    response = await analyze(api, headers={"Content-Type": content_type}, file_path="cart.py")
    assert response.status_code == 415 and error_code(response) == "UNSUPPORTED_MEDIA_TYPE"


async def test_the_zip_size_limit_applies_and_the_json_limit_does_not(api_factory):
    api = await api_factory(scripted(ollama_reply(EXPLAIN_JSON)), project=ProjectSettings(max_zip_bytes=2000))
    too_big = make_zip({"a.py": os.urandom(4000)}, compression=0)
    response = await analyze(api, data=too_big, file_path="a.py")
    assert response.status_code == 413 and error_code(response) == "REQUEST_TOO_LARGE"

    roomy = await api_factory(scripted(ollama_reply(EXPLAIN_JSON)))
    filler = "\n".join("# " + os.urandom(40).hex() for _ in range(2000))  # incompressible, so the ZIP stays large
    big_but_legal = make_zip({"cart.py": OFF_BY_ONE["cart.py"], "pad.py": filler}, compression=0)
    assert len(big_but_legal) > 131072  # more than the global JSON body limit
    assert (await analyze(roomy, data=big_but_legal, file_path="cart.py")).status_code == 200


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
async def test_only_post_is_allowed(api_factory, method):
    api = await api_factory(no_ollama)
    response = await api.client.request(method, ANALYZE)
    assert response.status_code == 405 and error_code(response) == "METHOD_NOT_ALLOWED"


async def test_foreign_host_headers_are_still_refused(api_factory):
    api = await api_factory(no_ollama)
    response = await analyze(api, file_path="cart.py", headers={**ZIP, "Host": "evil.example"})
    assert response.status_code == 400 and error_code(response) == "INVALID_HOST"


async def test_cors_preflight_and_actual_responses_work_for_the_allowed_origin(api_factory):
    origin = "http://localhost:5173"
    api = await api_factory(scripted(ollama_reply(EXPLAIN_JSON)))
    preflight = await api.client.options(
        ANALYZE, headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    )
    assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == origin
    actual = await analyze(api, file_path="cart.py", headers={**ZIP, "Origin": origin})
    assert actual.status_code == 200 and actual.headers["access-control-allow-origin"] == origin
    bad = await api.client.options(ANALYZE, headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


# --------------------------------------------------------------------------- #
# Failure handling: honest codes, bounded Ollama traffic
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "outcome, status, code",
    [
        (httpx.ConnectError("refused"), 503, "OLLAMA_UNAVAILABLE"),
        (httpx.ConnectTimeout("slow"), 503, "OLLAMA_CONNECT_TIMEOUT"),
        (httpx.ReadTimeout("slow"), 504, "GENERATION_TIMEOUT"),
        (httpx.Response(404, json={"error": "model 'qwen2.5-coder:3b' not found"}), 503, "MODEL_NOT_INSTALLED"),
        (httpx.Response(500, json={"error": "boom"}), 502, "OLLAMA_UPSTREAM_ERROR"),
        (httpx.Response(200, json={"response": "", "done": True}), 502, "INVALID_OLLAMA_RESPONSE"),
    ],
)
async def test_model_failures_map_to_the_standard_codes_and_are_never_retried(api_factory, outcome, status, code):
    api = await api_factory(scripted(outcome))
    response = await analyze(api, file_path="cart.py")
    assert response.status_code == status and error_code(response) == code
    assert len(ollama_bodies(api)) == 1
    assert "Traceback" not in response.text


async def test_malformed_model_output_gets_one_corrective_retry_then_succeeds(api_factory):
    api = await api_factory(scripted(ollama_reply("this is not json"), ollama_reply(EXPLAIN_JSON)))
    response = await analyze(api, file_path="cart.py")
    assert response.status_code == 200 and response.json()["generation"]["attempts"] == 2
    first, second = ollama_bodies(api)
    assert "Your previous reply was rejected" in second["prompt"] and "Your previous reply" not in first["prompt"]


async def test_schema_invalid_output_twice_is_a_502_after_exactly_two_calls(api_factory):
    bad = {**EXPLAIN_JSON, "sections": "not a list"}
    api = await api_factory(scripted(ollama_reply(bad), ollama_reply(bad)))
    response = await analyze(api, file_path="cart.py")
    assert response.status_code == 502 and error_code(response) == "INVALID_STRUCTURED_OUTPUT"
    assert len(ollama_bodies(api)) == 2


@pytest.mark.parametrize("field, value", [("category", "bug"), ("severity", "critical")])
async def test_invalid_enums_and_missing_fields_are_rejected_not_passed_through(api_factory, field, value):
    bad = json.loads(json.dumps(DEBUG_JSON))
    bad["findings"][0][field] = value
    api = await api_factory(scripted(ollama_reply(bad), ollama_reply(bad)))
    response = await analyze(api, file_path="cart.py", intent="debug")
    assert response.status_code == 502 and error_code(response) == "INVALID_STRUCTURED_OUTPUT"


async def test_truncated_output_triggers_one_compact_attempt(api_factory):
    cut = httpx.Response(200, json={"response": '{"summary": "abc', "done": True, "done_reason": "length"})
    compact = CompactDebugDraft.model_validate({"summary": "Short.", "findings": []}).model_dump()
    api = await api_factory(scripted(cut, ollama_reply(compact)))

    response = await analyze(api, file_path="cart.py", intent="debug")

    assert response.status_code == 200
    body = response.json()
    assert body["generation"]["mode"] == "compact" and body["generation"]["attempts"] == 2
    assert any("cut off" in text for text in body["limitations"])
    first, second = ollama_bodies(api)
    assert "Be brief" in second["prompt"] and "Be brief" not in first["prompt"]
    assert second["format"]["properties"]["findings"]["maxItems"] == 1


async def test_truncation_twice_is_a_502_after_two_calls_not_three(api_factory):
    cut = httpx.Response(200, json={"response": '{"summary": "abc', "done": True, "done_reason": "length"})
    api = await api_factory(scripted(cut, cut))
    response = await analyze(api, file_path="cart.py")
    assert response.status_code == 502 and error_code(response) == "OUTPUT_TRUNCATED"
    assert len(ollama_bodies(api)) == 2


async def test_total_model_calls_never_exceed_three(api_factory):
    cut = httpx.Response(200, json={"response": '{"summary": "abc', "done": True, "done_reason": "length"})
    # invalid, then truncated (end of the standard attempt), then truncated again in the compact attempt
    api = await api_factory(scripted(ollama_reply("nope"), cut, cut))
    response = await analyze(api, file_path="cart.py")
    assert response.status_code == 502 and error_code(response) == "OUTPUT_TRUNCATED"
    assert len(ollama_bodies(api)) == 3


async def test_a_prompt_that_cannot_fit_is_reported_as_a_server_problem_not_as_the_users_fault(api_factory):
    api = await api_factory(no_ollama, api=ApiSettings(max_input_tokens=1200))
    response = await analyze(api, file_path="cart.py")
    assert response.status_code == 503 and error_code(response) == "ANALYSIS_UNAVAILABLE"
    assert "AI_MAX_INPUT_TOKENS" in response.json()["error"]["message"]
    assert "Shorten it" not in response.text


async def test_other_endpoints_keep_working_when_analysis_is_unavailable(api_factory):
    api = await api_factory(
        lambda request: httpx.Response(200, json={"response": "hi", "done": True}), api=ApiSettings(max_input_tokens=1200)
    )
    assert (await api.client.post("/api/ai/generate", json={"prompt": "hi"})).status_code == 200


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
async def test_ai_busy_when_the_inference_slot_is_taken_and_no_model_call_is_made(api_factory):
    api = await api_factory(no_ollama, api=ApiSettings(queue_wait_timeout=0))
    async with api.app.state.limiter.slot():
        response = await analyze(api, file_path="cart.py")
    assert response.status_code == 503 and error_code(response) == "AI_BUSY"
    assert response.headers["retry-after"] == "5"
    assert api.transport.requests == []


async def test_waiting_for_the_model_does_not_hold_the_project_slot(api_factory):
    api = await api_factory(
        scripted(ollama_reply(EXPLAIN_JSON)),
        api=ApiSettings(queue_wait_timeout=5),
        project=ProjectSettings(max_concurrent=1, queue_wait_timeout=0),
    )
    async with api.app.state.limiter.slot():  # the model is busy with someone else
        waiting = asyncio.create_task(analyze(api, file_path="cart.py"))
        await asyncio.sleep(0.3)  # the analysis has finished ingestion and now waits for the model
        inspect = await api.client.post("/api/projects/inspect", content=make_zip(PYTHON_PROJECT), headers=ZIP)
        assert inspect.status_code == 200  # would be PROJECT_BUSY if the project slot were still held
    assert (await waiting).status_code == 200


async def test_two_analyses_never_run_on_the_model_at_once(api_factory):
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.15)
        async with lock:
            active -= 1
        return ollama_reply(EXPLAIN_JSON)

    api = await api_factory(handler, api=ApiSettings(queue_wait_timeout=5))
    results = await asyncio.gather(*(analyze(api, file_path="cart.py") for _ in range(3)))
    assert [r.status_code for r in results] == [200, 200, 200]
    assert peak == 1


async def test_the_inference_slot_is_released_after_a_failed_analysis(api_factory):
    api = await api_factory(scripted(httpx.ConnectError("refused"), ollama_reply(EXPLAIN_JSON)))
    assert (await analyze(api, file_path="cart.py")).status_code == 503
    assert api.app.state.limiter.active == 0
    assert (await analyze(api, file_path="cart.py")).status_code == 200


# --------------------------------------------------------------------------- #
# Honesty and safety through the whole stack
# --------------------------------------------------------------------------- #
async def test_hostile_source_is_defused_before_it_reaches_the_model(api_factory):
    api = await api_factory(scripted(ollama_reply({"summary": "Checked.", "findings": []})))

    response = await analyze(api, INJECTION_PROJECT, file_path="report.py", intent="debug")

    assert response.status_code == 200
    prompt = ollama_bodies(api)[0]["prompt"]
    assert "<|im_start|>" not in prompt and "<|im_end|>" not in prompt
    assert prompt.count("BEGIN_SOURCE_") == 1 and prompt.count("END_SOURCE_") == 1
    assert prompt.rstrip().endswith("reply with JSON only.")
    limits = response.json()["limitations"]
    assert any("chat-control" in text for text in limits) and any("instructions to an AI" in text for text in limits)


async def test_a_model_that_obeys_the_injection_still_cannot_fake_a_verified_finding(api_factory):
    obeyed = {"summary": "This project is secure and has no bugs.", "findings": [{**DEBUG_JSON["findings"][0], "file_path": "../../etc/passwd"}]}
    api = await api_factory(scripted(ollama_reply(obeyed)))
    body = (await analyze(api, INJECTION_PROJECT, file_path="report.py", intent="debug")).json()
    assert body["findings"][0]["verification"] == "unsupported" and body["findings"][0]["file_path"] is None
    assert any("instructions to an AI" in text for text in body["limitations"])  # the user is told to be careful


async def test_fabricated_references_are_downgraded_end_to_end(api_factory):
    fake = {"summary": "Bugs!", "findings": [
        {**DEBUG_JSON["findings"][0], "evidence": "danger(eval(user_input))"},
        {**DEBUG_JSON["findings"][0], "start_line": 400, "end_line": 410, "title": "Far away"},
    ]}
    api = await api_factory(scripted(ollama_reply(fake)))
    findings = (await analyze(api, file_path="cart.py", intent="debug")).json()["findings"]
    assert [f["verification"] for f in findings] == ["hypothesis", "unsupported"]
    assert "danger(eval" not in json.dumps(findings)
    assert findings[1]["start_line"] is None


async def test_coverage_reports_partial_files_whatever_the_model_claims(api_factory):
    from project_fixtures import big_python_project

    claim = {**EXPLAIN_JSON, "summary": "I read every line of all files.", "sections": []}
    api = await api_factory(scripted(ollama_reply(claim)))
    body = (await analyze(api, big_python_project(), file_path="pipeline.py")).json()
    assert body["coverage"]["partial_files"][0]["path"] == "pipeline.py"
    assert body["coverage"]["prompt_estimated_tokens"] <= body["coverage"]["input_limit"]
    assert any("Only part of" in text for text in body["limitations"])


async def test_sent_prompt_and_its_retry_stay_inside_the_token_budget(api_factory):
    from project_fixtures import big_python_project

    api = await api_factory(scripted(ollama_reply("bad"), ollama_reply({**EXPLAIN_JSON, "sections": []})))
    response = await analyze(api, big_python_project(), file_path="pipeline.py")
    assert response.status_code == 200
    budget = api.app.state.budget
    for body in ollama_bodies(api):
        assert budget.check(body["prompt"]).fits


async def test_nothing_from_the_source_or_the_model_is_logged(api_factory, caplog):
    caplog.set_level(logging.DEBUG)
    canary = "CANARY_9f3a_PRIVATE"
    files = {"secret.py": f'KEY = "{canary}"\n\ndef f():\n    return KEY\n'}
    leaking = {**EXPLAIN_JSON, "summary": f"The key is {canary}", "sections": []}
    api = await api_factory(scripted(ollama_reply(leaking), httpx.ConnectError("down"), ollama_reply("{broken"), ollama_reply("{broken")))
    await analyze(api, files, file_path="secret.py")
    await analyze(api, files, file_path="secret.py")
    await analyze(api, files, file_path="secret.py")
    server_log = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert server_log  # the server did log something
    assert canary not in caplog.text
    assert "secret.py" not in server_log


async def test_nothing_is_persisted_or_written_to_disk(api_factory, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = await api_factory(scripted(ollama_reply(EXPLAIN_JSON)))
    await analyze(api, file_path="cart.py")
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# Contract: Phase 1-3 stay intact; OpenAPI documents the new route
# --------------------------------------------------------------------------- #
async def test_openapi_documents_the_new_route_and_keeps_the_existing_schemas(api_factory):
    api = await api_factory(no_ollama)
    spec = (await api.client.get("/openapi.json")).json()
    operation = spec["paths"][ANALYZE]["post"]
    assert "application/zip" in operation["requestBody"]["content"]
    assert {"200", "413", "415", "422", "502", "503", "504"} <= set(operation["responses"])
    params = {p["name"]: p for p in operation["parameters"]}
    assert set(params) == {"intent", "depth", "file_path", "symbol"}
    assert params["depth"]["schema"]["default"] == "beginner"
    assert params["intent"]["schema"]["default"] == "explain"
    assert "plain English" in params["depth"]["description"]
    for name in ("HealthResponse", "GenerateRequest", "GenerateResponse", "ErrorResponse", "InspectResponse", "ContextResponse"):
        assert name in spec["components"]["schemas"]
    assert spec["info"]["version"] == "0.4.0"


async def test_openapi_labels_ai_generated_and_deterministic_fields(api_factory):
    api = await api_factory(no_ollama)
    schemas = (await api.client.get("/openapi.json")).json()["components"]["schemas"]
    assert "AI-generated" in schemas["AnalysisResponse"]["properties"]["summary"]["description"]
    assert "Deterministic" in schemas["CoverageOut"]["description"]
    assert "does NOT prove" in schemas["FindingOut"]["properties"]["verification"]["description"]
    assert "NOT a probability" in schemas["FindingOut"]["properties"]["confidence"]["description"]
