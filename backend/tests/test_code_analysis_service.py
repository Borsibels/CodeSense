"""CodeAnalysisService orchestration with a scripted generator (no Ollama, no network)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.api.errors import ApiError
from app.api.limiter import InferenceLimiter
from app.config import ProjectSettings
from app.services import (
    OllamaUnavailableError,
    OllamaTimeoutError,
    PromptTooLargeError,
    StructuredOutputInvalidError,
    StructuredOutputTruncatedError,
    TokenBudget,
)
from app.services.analysis_models import (
    BeginnerExplainDraft,
    CompactBeginnerExplainDraft,
    CompactDebugDraft,
    DebugDraft,
    TechnicalExplainDraft,
)
from app.services.code_analysis_service import (
    NOTICE,
    AnalysisBudgetError,
    AnalysisRequest,
    AnalysisUnavailableError,
    CodeAnalysisService,
    NoAnalyzableSourceError,
)
from app.services.context_selection import SelectionError
from app.services.ollama_service import OllamaGeneration
from app.services.project_pipeline import analyze_project
from app.services.structured import StructuredResult
from analysis_fixtures import BY_NAME, INJECTION_PROJECT, OFF_BY_ONE
from project_fixtures import IGNORED_PROJECT, big_python_project, make_zip

pytestmark = pytest.mark.anyio

BUDGET = TokenBudget(4096, 1024, 256)


def generation(**over):
    base = dict(text="{}", done_reason="stop", truncated=False, prompt_eval_count=600, eval_count=250)
    return OllamaGeneration(**{**base, **over})


def beginner_draft(**over):
    base = dict(
        summary="It adds up prices.", analogy="Like a cashier adding a receipt.",
        sections=[dict(file_path="cart.py", start_line=4, end_line=9, title="Adding", description="Adds each price.")],
        role_in_app="It computes the bill.", concept_name="Loop", concept_explanation="A loop repeats work.", assumptions=["I did not run it."],
    )
    return BeginnerExplainDraft.model_validate({**base, **over})


def debug_draft(**over):
    finding = dict(
        file_path="cart.py", evidence="7 |     for i in range(1, len(prices)):", start_line=7, end_line=7, title="Skips first",
        category="off_by_one", severity="medium", confidence="medium", problem="p", what_could_happen="w", likely_cause="c", suggestion="s",
    )
    base = dict(summary="One thing looks odd.", findings=[finding])
    return DebugDraft.model_validate({**base, **over})


class FakeStructured:
    """Scripted stand-in for StructuredGenerator; records every call."""

    def __init__(self, *outcomes, max_retries=1):
        self.outcomes = list(outcomes)
        self.calls: list[SimpleNamespace] = []
        self.max_retries = max_retries
        self.ollama = SimpleNamespace(settings=SimpleNamespace(model="qwen2.5-coder:3b"))
        self.events: list[str] = []

    async def generate(self, prompt, schema_model, *, max_retries=None):
        self.events.append("generate")
        self.calls.append(SimpleNamespace(prompt=prompt, model=schema_model, max_retries=max_retries))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        value, attempts = outcome if isinstance(outcome, tuple) else (outcome, 1)
        return StructuredResult(value=value, attempts=attempts, generation=generation())


def make_service(structured, *, budget=BUDGET, **kw):
    events = structured.events

    @asynccontextmanager
    async def slot():
        events.append("slot_enter")
        try:
            yield
        finally:
            events.append("slot_exit")

    kw.setdefault("inference_slot", slot)
    kw.setdefault("nonce_factory", lambda: "0123456789ab")
    return CodeAnalysisService(structured, budget, reserve=ProjectSettings().instruction_reserve_tokens, **kw)


def project_of(files):
    return analyze_project(make_zip(files), ProjectSettings())


async def run(service, files, intent="explain", depth="beginner", path="cart.py", symbol=None, **kw):
    return await service.analyze(project_of(files), AnalysisRequest(intent, depth, path, symbol), **kw)


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
async def test_beginner_explain_returns_the_plain_english_structure():
    fake = FakeStructured(beginner_draft())
    out = await run(make_service(fake), OFF_BY_ONE)

    assert out.status == "completed" and out.notice == NOTICE
    assert (out.intent, out.depth, out.target_file) == ("explain", "beginner", "cart.py")
    assert out.summary == "It adds up prices."
    assert out.analogy == "Like a cashier adding a receipt."
    assert out.role_in_app == "It computes the bill."
    assert out.concept_to_learn.name == "Loop" and out.concept_to_learn.explanation == "A loop repeats work."
    assert [e.title for e in out.explanations] == ["Adding"] and out.explanations[0].location_status == "in_context"
    assert out.findings == [] and out.assumptions == ["I did not run it."]
    assert fake.calls[0].model is BeginnerExplainDraft
    assert out.generation.mode == "standard" and out.generation.attempts == 1
    assert out.generation.model == "qwen2.5-coder:3b" and out.generation.prompt_version == "analysis-v2"
    assert (out.generation.prompt_eval_count, out.generation.eval_count) == (600, 250)


@pytest.mark.parametrize("depth", ["intermediate", "advanced"])
async def test_developer_and_technical_depths_use_the_technical_schema_and_omit_beginner_extras(depth):
    draft = TechnicalExplainDraft.model_validate({"summary": "Sums prices.", "sections": [], "assumptions": []})
    fake = FakeStructured(draft)
    out = await run(make_service(fake), OFF_BY_ONE, depth=depth)

    assert fake.calls[0].model is TechnicalExplainDraft
    assert out.depth == depth
    assert (out.analogy, out.role_in_app, out.concept_to_learn) == (None, None, None)
    assert "never programmed" not in fake.calls[0].prompt


async def test_the_prompt_sent_is_delimited_neutralised_and_audience_specific():
    fake = FakeStructured(beginner_draft())
    await run(make_service(fake), OFF_BY_ONE)
    prompt = fake.calls[0].prompt
    assert "BEGIN_SOURCE_0123456789ab" in prompt and "END_SOURCE_0123456789ab" in prompt
    assert "for i in range(1, len(prices)):" in prompt
    assert "never programmed" in prompt


async def test_debug_findings_are_validated_and_carry_the_verification_status():
    fake = FakeStructured(debug_draft())
    out = await run(make_service(fake), OFF_BY_ONE, intent="debug")

    assert fake.calls[0].model is DebugDraft
    assert out.explanations == [] and (out.analogy, out.role_in_app, out.concept_to_learn) == (None, None, None)
    [finding] = out.findings
    assert finding.id == "F1" and finding.verification == "source_verified"
    assert finding.evidence.excerpt_matched and "range(1, len(prices))" in finding.evidence.source_excerpt


async def test_debug_with_no_findings_is_a_valid_clean_answer():
    out = await run(make_service(FakeStructured(debug_draft(findings=[]))), OFF_BY_ONE, intent="debug")
    assert out.findings == []


async def test_unsupported_findings_are_kept_but_labelled_and_uncited():
    bad = dict(file_path="ghost.py", evidence="x", start_line=1, end_line=1, title="Imaginary", category="logic", severity="high",
               confidence="high", problem="p", what_could_happen="w", likely_cause="c", suggestion="s")
    out = await run(make_service(FakeStructured(debug_draft(findings=[bad]))), OFF_BY_ONE, intent="debug")
    [finding] = out.findings
    assert finding.verification == "unsupported" and finding.confidence == "low"
    assert finding.file_path is None and finding.evidence is None


async def test_overview_without_a_file_works():
    fake = FakeStructured(beginner_draft(sections=[]))
    out = await run(make_service(fake), BY_NAME["multi_file_py"].files, intent="overview", path=None)
    assert out.intent == "overview" and out.target_file is None


async def test_timings_and_generation_metadata_are_reported_deterministically():
    out = await run(make_service(FakeStructured(beginner_draft())), OFF_BY_ONE, project_ms=12.5)
    assert set(out.timings_ms) == {"project", "context", "inference", "validation", "total"}
    assert out.timings_ms["project"] == 12.5 and out.timings_ms["total"] >= 12.5


async def test_on_generation_hook_receives_the_estimated_prompt_tokens_and_the_generation():
    seen = []
    fake = FakeStructured(beginner_draft())
    out = await run(make_service(fake, on_generation=lambda tokens, gen: seen.append((tokens, gen.eval_count))), OFF_BY_ONE)
    assert seen == [(out.coverage.prompt_estimated_tokens, 250)]


# --------------------------------------------------------------------------- #
# Coverage and limitations are deterministic
# --------------------------------------------------------------------------- #
async def test_coverage_comes_from_the_real_context_not_from_anything_the_model_says():
    draft = beginner_draft(summary="I fully analysed every file in this 758-line project and found it complete.", sections=[])
    out = await run(make_service(FakeStructured(draft)), big_python_project(), path="pipeline.py")

    partial = {p.path: p for p in out.coverage.partial_files}
    assert "pipeline.py" in partial
    assert partial["pipeline.py"].omitted_ranges and partial["pipeline.py"].included_ranges[0].start_line == 1
    assert "pipeline.py" not in out.coverage.full_files
    assert any("Only part of" in text and "pipeline.py" in text for text in out.limitations)
    assert out.coverage.prompt_estimated_tokens <= out.coverage.input_limit
    assert out.coverage.is_estimate is True
    assert out.coverage.instruction_estimated_tokens > 0
    assert out.coverage.context_estimated_tokens + out.coverage.instruction_estimated_tokens >= out.coverage.prompt_estimated_tokens - 1


async def test_outline_only_and_skipped_files_are_listed_in_limitations():
    out = await run(make_service(FakeStructured(beginner_draft(sections=[]))), BY_NAME["multi_file_py"].files, path="shop/cart.py")
    assert out.coverage.outline_only_files
    assert any("only as a list of signatures" in text for text in out.limitations)


async def test_excluded_archive_files_are_reported_with_reasons():
    out = await run(make_service(FakeStructured(beginner_draft(sections=[]))), {**IGNORED_PROJECT}, path="main.py")
    assert out.coverage.excluded_total == len(out.coverage.excluded_files) > 0
    assert all(e.reason for e in out.coverage.excluded_files)
    assert any("skipped" in text for text in out.limitations)


async def test_a_fully_shown_small_project_has_no_coverage_limitations():
    out = await run(make_service(FakeStructured(beginner_draft(sections=[]))), OFF_BY_ONE)
    assert out.limitations == [] and out.coverage.full_files == ["cart.py"]


async def test_hostile_source_is_defused_flagged_and_still_analysed():
    fake = FakeStructured(debug_draft(findings=[]))
    out = await run(make_service(fake), INJECTION_PROJECT, intent="debug", path="report.py")

    prompt = fake.calls[0].prompt
    assert "<|im_start|>" not in prompt and "<|im_end|>" not in prompt
    assert "Ignore all previous instructions" in prompt  # it is data, shown as such, between the markers
    assert prompt.index("BEGIN_SOURCE_") < prompt.index("Ignore all previous instructions") < prompt.index("END_SOURCE_")
    assert any("special chat-control sequence" in text for text in out.limitations)
    assert any("looks like instructions to an AI" in text for text in out.limitations)


# --------------------------------------------------------------------------- #
# Inference slot discipline
# --------------------------------------------------------------------------- #
async def test_all_model_calls_happen_inside_one_slot_and_validation_after_it():
    fake = FakeStructured(StructuredOutputTruncatedError(1, "cut"), CompactBeginnerExplainDraft.model_validate(
        {"summary": "s", "sections": [], "role_in_app": "r", "concept_name": "c", "concept_explanation": "e"}))
    await run(make_service(fake), OFF_BY_ONE)
    assert fake.events == ["slot_enter", "generate", "generate", "slot_exit"]


async def test_slot_is_released_when_generation_fails():
    fake = FakeStructured(OllamaUnavailableError("down"))
    with pytest.raises(OllamaUnavailableError):
        await run(make_service(fake), OFF_BY_ONE)
    assert fake.events == ["slot_enter", "generate", "slot_exit"]


async def test_a_busy_slot_fails_before_any_inference():
    limiter = InferenceLimiter(1, 0)
    fake = FakeStructured(beginner_draft())
    service = make_service(fake, inference_slot=limiter.slot)
    async with limiter.slot():
        with pytest.raises(ApiError) as caught:
            await run(service, OFF_BY_ONE)
    assert caught.value.code == "AI_BUSY" and fake.calls == []


async def test_cancellation_releases_the_real_limiter_slot():
    limiter = InferenceLimiter(1, 1)
    started = asyncio.Event()

    class Hanging(FakeStructured):
        async def generate(self, prompt, schema_model, *, max_retries=None):
            started.set()
            await asyncio.sleep(60)

    service = make_service(Hanging(), inference_slot=limiter.slot)
    task = asyncio.create_task(run(service, OFF_BY_ONE))
    await started.wait()
    assert limiter.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert limiter.active == 0


async def test_no_inference_slot_is_taken_when_the_request_is_rejected_earlier():
    fake = FakeStructured()
    service = make_service(fake)
    with pytest.raises(SelectionError) as caught:
        await run(service, OFF_BY_ONE, path="missing.py")
    assert caught.value.code == "FILE_NOT_FOUND"
    with pytest.raises(NoAnalyzableSourceError):
        await run(service, {"README.md": "# no code\n"}, intent="overview", path=None)
    assert fake.events == []


@pytest.mark.parametrize("path, symbol, code", [(None, None, "FILE_REQUIRED"), ("cart.py", "nope", "SYMBOL_NOT_FOUND")])
async def test_selection_errors_surface_with_their_codes(path, symbol, code):
    fake = FakeStructured()
    with pytest.raises(SelectionError) as caught:
        await run(make_service(fake), OFF_BY_ONE, intent="debug", path=path, symbol=symbol)
    assert caught.value.code == code and fake.calls == []


# --------------------------------------------------------------------------- #
# Retry and truncation strategy
# --------------------------------------------------------------------------- #
async def test_standard_attempt_allows_exactly_one_validation_retry():
    fake = FakeStructured(beginner_draft(), max_retries=1)
    await run(make_service(fake), OFF_BY_ONE)
    assert fake.calls[0].max_retries == 1


async def test_a_larger_configured_retry_count_is_capped_to_keep_latency_bounded():
    fake = FakeStructured(beginner_draft(), max_retries=5)
    await run(make_service(fake), OFF_BY_ONE)
    assert fake.calls[0].max_retries == 1


async def test_zero_configured_retries_stays_zero():
    fake = FakeStructured(beginner_draft(), max_retries=0)
    await run(make_service(fake), OFF_BY_ONE)
    assert fake.calls[0].max_retries == 0


async def test_retry_is_disabled_when_the_retry_prompt_would_not_fit():
    class NoRetryRoom:
        """Budget proxy that says any prompt carrying the retry sentence is too large."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def check(self, text):
            result = self._inner.check(text)
            if "Your previous reply was rejected" in text:
                return type(result)(tokens=result.limit + 1, is_estimate=True, limit=result.limit)
            return result

    fake = FakeStructured(beginner_draft(), max_retries=1)
    await run(make_service(fake, budget=NoRetryRoom(BUDGET)), OFF_BY_ONE)
    assert fake.calls[0].max_retries == 0


async def test_truncation_triggers_one_compact_attempt_with_a_smaller_schema_and_no_retry():
    compact = CompactBeginnerExplainDraft.model_validate(
        {"summary": "Short.", "sections": [], "role_in_app": "r", "concept_name": "c", "concept_explanation": "e"})
    fake = FakeStructured(StructuredOutputTruncatedError(2, "cut"), compact)
    out = await run(make_service(fake), OFF_BY_ONE)

    assert len(fake.calls) == 2
    assert fake.calls[1].model is CompactBeginnerExplainDraft and fake.calls[1].max_retries == 0
    assert "Be brief" in fake.calls[1].prompt and "Be brief" not in fake.calls[0].prompt
    assert out.generation.mode == "compact" and out.generation.attempts == 3
    assert any("cut off at its length limit" in text for text in out.limitations)
    assert out.analogy is None and out.assumptions == []  # the compact schema has no such fields


async def test_compact_debug_fallback_uses_the_single_finding_schema():
    compact = CompactDebugDraft.model_validate({"summary": "s", "findings": []})
    fake = FakeStructured(StructuredOutputTruncatedError(1, "cut"), compact)
    out = await run(make_service(fake), OFF_BY_ONE, intent="debug")
    assert fake.calls[1].model is CompactDebugDraft and out.generation.mode == "compact"


async def test_truncation_in_the_compact_attempt_too_is_a_failure_not_a_third_try():
    fake = FakeStructured(StructuredOutputTruncatedError(1, "cut"), StructuredOutputTruncatedError(1, "cut again"))
    with pytest.raises(StructuredOutputTruncatedError):
        await run(make_service(fake), OFF_BY_ONE)
    assert len(fake.calls) == 2


async def test_compact_fallback_can_be_disabled():
    fake = FakeStructured(StructuredOutputTruncatedError(1, "cut"))
    with pytest.raises(StructuredOutputTruncatedError):
        await run(make_service(fake, compact_fallback=False), OFF_BY_ONE)
    assert len(fake.calls) == 1


async def test_compact_fallback_is_skipped_when_too_much_time_is_gone():
    ticks = iter(range(0, 1_000_000, 1000))  # every clock read advances 1000 s
    fake = FakeStructured(StructuredOutputTruncatedError(1, "cut"))
    with pytest.raises(StructuredOutputTruncatedError):
        await run(make_service(fake, clock=lambda: float(next(ticks))), OFF_BY_ONE)
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "error",
    [OllamaUnavailableError("down"), OllamaTimeoutError("slow"), StructuredOutputInvalidError(2, "bad json")],
)
async def test_only_truncation_changes_strategy_other_failures_are_not_retried_here(error):
    fake = FakeStructured(error)
    with pytest.raises(type(error)):
        await run(make_service(fake), OFF_BY_ONE)
    assert len(fake.calls) == 1


async def test_at_most_three_model_calls_for_one_request():
    # 2 standard attempts (counted inside the generator) + 1 compact = the documented maximum.
    fake = FakeStructured(StructuredOutputTruncatedError(2, "cut"), CompactDebugDraft.model_validate({"summary": "s", "findings": []}))
    out = await run(make_service(fake), OFF_BY_ONE, intent="debug")
    assert out.generation.attempts == 3 and len(fake.calls) == 2


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
async def test_beginner_templates_get_a_bigger_reserve_than_developer_ones_so_context_shrinks_honestly():
    service = make_service(FakeStructured())
    assert service.reserve_for("debug", "beginner") > service.reserve_for("explain", "advanced") >= 700
    assert BUDGET.input_limit - service.reserve_for("debug", "beginner") >= 1500


async def test_the_context_uses_the_per_request_reserve():
    out = await run(make_service(FakeStructured(beginner_draft(sections=[]))), big_python_project(), path="pipeline.py")
    service = make_service(FakeStructured())
    assert out.coverage.context_estimated_tokens <= BUDGET.input_limit - service.reserve_for("explain", "beginner")


async def test_an_overshooting_prompt_is_rebuilt_once_then_fails_loudly_never_silently():
    class AlwaysTooBig:
        def __init__(self, inner):
            self._inner, self.calls = inner, 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def ensure_fits(self, text):
            self.calls += 1
            raise PromptTooLargeError(self._inner.check(text))

    budget = AlwaysTooBig(BUDGET)
    fake = FakeStructured(beginner_draft())
    with pytest.raises(AnalysisBudgetError):
        await run(make_service(fake, budget=budget), OFF_BY_ONE)
    assert budget.calls == 2 and fake.calls == [] and fake.events == []


async def test_a_too_small_token_budget_makes_analysis_unavailable_with_a_clear_message():
    tiny = TokenBudget(4096, 1024, 256, max_input_tokens=1200)
    fake = FakeStructured(beginner_draft())
    service = make_service(fake, budget=tiny)
    assert service.unavailable_reason
    with pytest.raises(AnalysisUnavailableError) as caught:
        await run(service, OFF_BY_ONE)
    assert "AI_MAX_INPUT_TOKENS" in caught.value.message and fake.calls == []


async def test_nothing_fitting_the_window_is_reported_as_no_analyzable_source(monkeypatch):
    import dataclasses

    from app.services import code_analysis_service as module

    real = module.assemble_context

    def nothing_fits(*args, **kwargs):
        context = real(*args, **kwargs)
        empty = dataclasses.replace(context.summary, files_full=0, files_partial=0, files_outline_only=0)
        return dataclasses.replace(context, summary=empty)

    monkeypatch.setattr(module, "assemble_context", nothing_fits)
    fake = FakeStructured(beginner_draft())
    with pytest.raises(NoAnalyzableSourceError) as caught:
        await run(make_service(fake), OFF_BY_ONE)
    assert "fit" in caught.value.message and fake.events == []


# --------------------------------------------------------------------------- #
# Privacy: nothing from the source or the model reaches the log
# --------------------------------------------------------------------------- #
CANARY = "CANARY_SECRET_TOKEN_9f3a"


async def test_source_and_model_text_never_reach_the_log_on_success_or_failure(caplog):
    caplog.set_level(logging.DEBUG)
    files = {"cart.py": f'SECRET = "{CANARY}"\n\ndef total(p):\n    return sum(p)\n'}
    draft = beginner_draft(summary=f"The code hides {CANARY}.", sections=[])
    await run(make_service(FakeStructured(draft)), files)
    with pytest.raises(OllamaUnavailableError):
        await run(make_service(FakeStructured(OllamaUnavailableError("down"))), files)
    with pytest.raises(StructuredOutputTruncatedError):
        await run(make_service(FakeStructured(StructuredOutputTruncatedError(1, "x"), StructuredOutputTruncatedError(1, "y"))), files)
    assert CANARY not in caplog.text
    assert "cart.py" not in caplog.text


# --------------------------------------------------------------------------- #
# Glossary (deterministic, beginner depth only)
# --------------------------------------------------------------------------- #
async def test_beginner_answers_get_a_glossary_built_from_the_ai_text_by_the_backend():
    draft = beginner_draft(summary="A loop goes through a list.", sections=[], concept_name="Loop", concept_explanation="A loop repeats work.")
    out = await run(make_service(FakeStructured(draft)), OFF_BY_ONE)
    assert [g.term for g in out.glossary] == ["loop", "list"]
    assert all(g.meaning for g in out.glossary)


async def test_glossary_scans_findings_too():
    out = await run(make_service(FakeStructured(debug_draft())), OFF_BY_ONE, intent="debug")
    # the canned finding text is "p", "w", ... so nothing to define; add real words
    finding = dict(debug_draft().findings[0].model_dump(), problem="The loop skips the first item in the list.")
    out = await run(make_service(FakeStructured(debug_draft(findings=[finding]))), OFF_BY_ONE, intent="debug")
    assert {g.term for g in out.glossary} >= {"loop", "list"}


@pytest.mark.parametrize("depth", ["intermediate", "advanced"])
async def test_developer_and_technical_depths_get_no_glossary(depth):
    draft = TechnicalExplainDraft.model_validate({"summary": "A loop over a list of variables.", "sections": [], "assumptions": []})
    out = await run(make_service(FakeStructured(draft)), OFF_BY_ONE, depth=depth)
    assert out.glossary == []


async def test_quoted_source_code_is_not_scanned_for_glossary_terms():
    draft = beginner_draft(summary="It adds prices.", sections=[], concept_name="Addition", concept_explanation="Adding numbers.", analogy="", role_in_app="Makes the bill.", assumptions=[])
    out = await run(make_service(FakeStructured(draft)), {"cart.py": "def loop_list_function(variable):\n    return variable\n"}, path="cart.py")
    assert out.glossary == []


async def test_every_debug_answer_carries_the_plain_language_caution_and_explanations_do_not():
    from app.services.code_analysis_service import DEBUG_CAUTION

    debug = await run(make_service(FakeStructured(debug_draft(findings=[]))), OFF_BY_ONE, intent="debug")
    explain = await run(make_service(FakeStructured(beginner_draft(sections=[]))), OFF_BY_ONE)
    assert DEBUG_CAUTION in debug.limitations and "not as a fact" in DEBUG_CAUTION
    assert DEBUG_CAUTION not in explain.limitations
