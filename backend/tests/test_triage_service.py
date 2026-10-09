"""Phase 4.5 through the real CodeAnalysisService (scripted model): tiers, outcome, summary, rules, relationships."""

from __future__ import annotations

import pytest

from app.services.analysis_models import AnalysisResponse, CompactDebugDraft, DebugDraft
from app.services.code_analysis_service import DEBUG_CAUTION, PATTERN_CAUTION, TRIAGE_CAUTION
from app.services.analysis_prompts import instructions
from test_code_analysis_service import FakeStructured, beginner_draft, debug_draft, make_service, run
from analysis_fixtures import BY_NAME, CLEAN_PROJECT, INJECTION_PROJECT, MULTI_FILE_PYTHON, OFF_BY_ONE, hidden_bug_files, line_of

pytestmark = pytest.mark.anyio


def finding(path, needle, files, **over):
    line = line_of(files, path, needle)
    base = dict(
        file_path=path, evidence=f"{line} | {files[path].split(chr(10))[line - 1]}", start_line=line, end_line=line, title="Something odd",
        category="logic", severity="medium", confidence="medium", problem="The code does something odd.", what_could_happen="w",
        likely_cause="c", suggestion="s",
    )
    return {**base, **over}


async def debug(files, findings, path, symbol=None, summary="Found 2 main issues."):
    fake = FakeStructured(DebugDraft.model_validate({"summary": summary, "findings": findings}))
    out = await run(make_service(fake), files, intent="debug", path=path, symbol=symbol)
    return out, fake


# --------------------------------------------------------------------------- #
# Tiers and outcome
# --------------------------------------------------------------------------- #
async def test_a_finding_on_a_planted_pattern_is_a_possible_problem_corroborated_by_a_rule():
    out, _ = await debug(OFF_BY_ONE, [finding("cart.py", "range(1, len(prices))", OFF_BY_ONE, title="Skips first")], "cart.py")
    [f] = out.findings
    assert f.verification == "source_verified"  # the evidence validator is untouched
    assert (f.tier, f.tier_reasons) == ("possible_problem", ["CORROBORATED_BY_RULE"])
    assert out.debug_outcome == "possible_problems"
    [check] = out.pattern_checks
    assert check.rule == "PY_SKIPS_FIRST_ITEM" and check.strength == "worth_checking" and check.corroborates == ["F1"]


async def test_clean_code_with_only_an_input_assumption_claim_is_no_clear_problem_and_the_claim_is_kept():
    claim = finding("greeting.py", "cleaned = name.strip()", CLEAN_PROJECT, title="Missing input validation", problem="name may be None.")
    out, _ = await debug(CLEAN_PROJECT, [claim], "greeting.py")
    [f] = out.findings
    assert f.verification == "source_verified" and f.tier == "worth_checking" and f.tier_reasons == ["INPUT_ASSUMPTION"]
    assert out.debug_outcome == "no_clear_problem"
    assert "No clear problem was identified in the code examined (all 19 lines of greeting.py)" in out.summary
    assert "not proof" in out.summary and "has not been proven bug-free" in out.summary
    assert "1 note worth a second look is listed below" in out.summary


async def test_a_finding_inside_the_demo_block_is_demoted_but_still_returned_and_verified():
    claim = finding("greeting.py", "print(line)", CLEAN_PROJECT, title="Print has no error handling check")
    out, _ = await debug(CLEAN_PROJECT, [claim], "greeting.py")
    [f] = out.findings
    assert f.tier == "worth_checking" and "IN_DEMO_CODE" in f.tier_reasons and f.evidence.excerpt_matched


async def test_an_uncorroborated_logic_claim_in_ordinary_code_is_a_possible_problem_without_pretending_to_be_a_bug():
    claim = finding("greeting.py", "return \"Hello, \"", CLEAN_PROJECT, title="Greeting format", problem="The greeting joins strings without a separator.")
    out, _ = await debug(CLEAN_PROJECT, [claim], "greeting.py")
    assert out.findings[0].tier == "possible_problem" and out.findings[0].tier_reasons == []
    assert out.debug_outcome == "possible_problems"
    assert "suspicions, not confirmed bugs" in out.summary and "bug-free" not in out.summary


async def test_the_ai_debug_summary_is_discarded_in_favour_of_the_deterministic_one():
    out, _ = await debug(OFF_BY_ONE, [], "cart.py", summary="I found 2 main issues and the code is perfect.")
    assert "2 main issues" not in out.summary and "perfect" not in out.summary
    assert out.summary.startswith("No clear problem was identified")


async def test_tiers_never_remove_a_finding_so_recall_cannot_drop():
    findings = [
        finding("cart.py", "total = 0", OFF_BY_ONE, title="Missing input validation"),
        finding("cart.py", "total += prices[i]", OFF_BY_ONE, title="Loop issue"),
        finding("cart.py", "items = [10.0, 5.5, 3.25]", OFF_BY_ONE, title="Hardcoded values"),
    ]
    out, _ = await debug(OFF_BY_ONE, findings, "cart.py")
    assert len(out.findings) == 3
    assert [f.id for f in out.findings] == ["F1", "F2", "F3"]
    assert [f.tier for f in out.findings] == sorted((f.tier for f in out.findings), key=lambda t: t != "possible_problem")


async def test_the_compact_fallback_answer_is_triaged_the_same_way():
    from app.services import StructuredOutputTruncatedError

    compact = CompactDebugDraft.model_validate({"summary": "s", "findings": [
        finding("cart.py", "range(1, len(prices))", OFF_BY_ONE, title="Skips first")]})
    fake = FakeStructured(StructuredOutputTruncatedError(1, "cut"), compact)
    out = await run(make_service(fake), OFF_BY_ONE, intent="debug", path="cart.py")
    assert out.generation.mode == "compact" and out.findings[0].tier == "possible_problem"


# --------------------------------------------------------------------------- #
# Pattern checks
# --------------------------------------------------------------------------- #
async def test_a_rule_hit_is_reported_even_when_the_ai_finds_nothing_and_it_states_its_assumptions():
    out, _ = await debug(INJECTION_PROJECT, [], "report.py")
    assert out.findings == [] and out.debug_outcome == "possible_problems"
    [check] = out.pattern_checks
    assert check.rule == "PY_INDEX_PAST_END" and check.strength == "problem_if_assumptions_hold"
    assert check.assumptions and check.parser == "confirmed" and check.shown_to_ai
    assert "range(len(scores) + 1)" in check.source_excerpt
    assert "1 possible problem found" in out.summary


async def test_a_bug_in_lines_the_ai_never_saw_is_found_by_the_rule_and_labelled_as_unseen():
    files = hidden_bug_files()
    out, _ = await debug(files, [], "pipeline.py")
    [check] = [c for c in out.pattern_checks if c.rule == "PY_INDEX_PAST_END"]
    assert check.shown_to_ai is False
    assert out.debug_outcome == "possible_problems" and "not shown" in out.summary
    assert out.coverage.partial_files  # and the response still says the file was only partly shown


async def test_pattern_checks_respect_the_selected_symbol():
    inside, _ = await debug(OFF_BY_ONE, [], "cart.py", symbol="total_price")
    outside, _ = await debug(OFF_BY_ONE, [], "cart.py", symbol="apply_discount")
    assert [c.rule for c in inside.pattern_checks] == ["PY_SKIPS_FIRST_ITEM"]
    assert outside.pattern_checks == []


async def test_clean_fixtures_get_no_pattern_checks_and_no_clear_problem():
    for name in ("clean_project", "clean_counter", "clean_multi_cart", "clean_names_js"):
        fixture = BY_NAME[name]
        out, _ = await debug(fixture.files, [], fixture.file_path)
        assert out.pattern_checks == [] and out.debug_outcome == "no_clear_problem", name


# --------------------------------------------------------------------------- #
# What does NOT change
# --------------------------------------------------------------------------- #
async def test_one_model_call_and_a_debug_prompt_that_knows_nothing_about_tiers_or_rules():
    out, fake = await debug(INJECTION_PROJECT, [], "report.py")
    assert len(fake.calls) == 1 and out.generation.attempts == 1
    prompt = fake.calls[0].prompt
    for leaked in ("possible_problem", "worth_checking", "PY_INDEX_PAST_END", "pattern", "tier"):
        assert leaked not in prompt


async def test_debug_answers_carry_the_ordering_and_pattern_cautions_next_to_the_ai_caution():
    out, _ = await debug(CLEAN_PROJECT, [], "greeting.py")
    assert DEBUG_CAUTION in out.limitations and TRIAGE_CAUTION in out.limitations and PATTERN_CAUTION in out.limitations
    assert "not a verdict" in TRIAGE_CAUTION and "does not mean the code is correct" in PATTERN_CAUTION


async def test_the_debug_task_wording_is_the_one_that_was_measured_in_phase_4():
    # The byte-for-byte pin is EXPECTED_DEBUG_DIGEST in test_analysis_prompts.py; this states the meaning in words.
    text = instructions("debug", "beginner", "standard")
    assert "Find up to 3 places where the code may go wrong" in text and "empty findings list" in text
    assert "the single most suspicious line" in text


# --------------------------------------------------------------------------- #
# Explain and overview: deterministic relationships, no debug fields
# --------------------------------------------------------------------------- #
async def test_explain_returns_dependency_relationships_from_the_parsers_and_no_debug_fields():
    fake = FakeStructured(beginner_draft(sections=[]))
    out = await run(make_service(fake), MULTI_FILE_PYTHON, path="shop/cart.py")
    assert out.debug_outcome is None and out.pattern_checks == []
    edges = {(r.from_, r.to, r.kind, r.resolved) for r in out.relationships}
    assert ("shop/cart.py", "shop/pricing.py", "uses", True) in edges
    assert ("shop/main.py", "shop/cart.py", "uses", True) in edges
    assert out.summary == "It adds up prices."  # explain keeps the AI summary


async def test_relationships_serialise_with_the_frontends_field_names():
    fake = FakeStructured(beginner_draft(sections=[]))
    out = await run(make_service(fake), MULTI_FILE_PYTHON, path="shop/cart.py")
    dumped = out.model_dump(mode="json", by_alias=True)["relationships"][0]
    assert set(dumped) == {"from", "to", "kind", "resolved"}
    assert AnalysisResponse.model_validate(out.model_dump(mode="json", by_alias=True))


async def test_an_entry_point_is_marked_and_a_missing_file_is_unresolved():
    files = {"index.html": '<script src="app.js"></script><script src="gone.js"></script>', "app.js": "var a = 1;\n"}
    out = await run(make_service(FakeStructured(beginner_draft(sections=[]))), files, intent="explain", path="index.html")
    edges = {(r.from_, r.to, r.kind, r.resolved) for r in out.relationships}
    assert ("index.html", "app.js", "loads", True) in edges and ("index.html", "gone.js", "loads", False) in edges


async def test_an_overview_lists_entry_points_and_edges_without_a_selected_file():
    out = await run(make_service(FakeStructured(beginner_draft(sections=[]))), MULTI_FILE_PYTHON, intent="overview", path=None)
    kinds = {r.kind for r in out.relationships}
    assert "entry_point" in kinds and "uses" in kinds and len(out.relationships) <= 20
