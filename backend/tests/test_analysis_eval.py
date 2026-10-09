"""The Phase 4.5 evaluation harness: ground truth, scoring, the corrected jargon metric, and the seal."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analysis_eval as ev  # noqa: E402
from analysis_fixtures import BY_NAME, FIXTURES  # noqa: E402
from heldout_fixtures import HELDOUT_FIXTURES, manifest  # noqa: E402

MANIFEST = Path(__file__).resolve().parent / "data" / "heldout_manifest.json"
ALL = tuple(FIXTURES) + tuple(HELDOUT_FIXTURES)


# --------------------------------------------------------------------------- #
# Ground truth is real: every planted bug points at the line it claims to
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fixture", [f for f in ALL if f.bug], ids=lambda f: f.name)
def test_planted_bug_spans_are_inside_the_file_and_non_blank(fixture):
    path, start, end = fixture.bug
    lines = fixture.files[path].split("\n")
    assert 1 <= start <= end <= len(lines)
    assert any(line.strip() for line in lines[start - 1 : end])


@pytest.mark.parametrize("fixture", [f for f in ALL if f.bug_function], ids=lambda f: f.name)
def test_the_bug_function_exists_and_contains_the_bug_span(fixture):
    project = ev.project_of(fixture)
    low, high = ev.function_range(project, fixture.bug[0], fixture.bug_function)
    assert low <= fixture.bug[1] and fixture.bug[2] <= high


@pytest.mark.parametrize("fixture", ALL, ids=lambda f: f.name)
def test_every_fixture_is_valid_source_and_names_a_real_file(fixture):
    if fixture.file_path:
        assert fixture.file_path in fixture.files
    for path, text in fixture.files.items():
        if path.endswith(".py"):
            ast.parse(text)
    assert fixture.has_bug == bool(fixture.bug)


def test_clean_fixtures_exist_in_every_category_the_plan_requires():
    categories = {f.category for f in ALL}
    for needed in ("correct_python", "buggy_python", "correct_js", "buggy_js", "html_css", "cross_file", "partial_context", "injection", "ambiguous"):
        assert needed in categories, needed


# --------------------------------------------------------------------------- #
# The sealed held-out set
# --------------------------------------------------------------------------- #
def test_the_held_out_set_has_not_changed_since_it_was_sealed():
    sealed = json.loads(MANIFEST.read_text(encoding="utf-8"))["fixtures"]
    assert manifest() == sealed, "heldout_fixtures.py changed after sealing: that invalidates the held-out evaluation"


def test_the_held_out_set_is_separate_from_the_development_set():
    assert len(HELDOUT_FIXTURES) >= 4
    assert all(f.split == "heldout" for f in HELDOUT_FIXTURES)
    assert all(f.split == "dev" for f in FIXTURES)
    assert not {f.name for f in HELDOUT_FIXTURES} & {f.name for f in FIXTURES}
    assert ev.fixtures_for_split("dev") == tuple(FIXTURES) and ev.fixtures_for_split("heldout") == tuple(HELDOUT_FIXTURES)


def test_the_held_out_set_has_both_clean_and_buggy_fixtures_in_both_languages():
    assert any(not f.has_bug for f in HELDOUT_FIXTURES) and any(f.has_bug for f in HELDOUT_FIXTURES)
    assert {"correct_python", "buggy_python", "correct_js", "buggy_js"} <= {f.category for f in HELDOUT_FIXTURES}


def test_the_stdlib_bug_is_exactly_one_edit_of_the_stdlib_code():
    import heldout_fixtures as h

    clean, bug = h.SEARCH["search.py"].split("\n"), h.SEARCH_BUG["search.py"].split("\n")
    assert len(clean) == len(bug)
    assert sum(1 for a, b in zip(clean, bug) if a != b) == 1


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def finding(path, start, end, **extra):
    return {"id": "F1", "file_path": path, "start_line": start, "end_line": end, "verification": "source_verified",
            "category": "logic", **extra}


def test_a_finding_on_the_planted_lines_is_a_true_positive_anywhere_else_is_a_false_positive():
    fixture = BY_NAME["off_by_one_py"]
    path, start, end = fixture.bug
    score = ev.score_debug({"findings": [finding(path, end, end), finding(path, 20, 20)]}, fixture)
    assert [r["kind"] for r in score["findings"]] == ["tp", "fp"]
    assert score["bug_cited_any"] and score["fp_total"] == 1
    assert score["bug_function_cited"]


def test_a_real_secondary_observation_is_not_counted_as_a_false_positive():
    fixture = BY_NAME["js_logic_error"]
    path, start, end = fixture.acceptable[0]
    score = ev.score_debug({"findings": [finding(path, start, end)]}, fixture)
    assert score["findings"][0]["kind"] == "acceptable" and score["fp_total"] == 0


def test_function_localisation_credits_a_finding_inside_the_buggy_function_but_off_the_line():
    fixture = BY_NAME["discount_py"]
    score = ev.score_debug({"findings": [finding("pricing.py", 6, 6)]}, fixture)  # the `discount =` line, same function
    assert not score["bug_cited_any"] and score["bug_function_cited"]


def test_a_phase_4_response_has_no_tier_and_any_finding_means_a_problem_is_reported():
    fixture = BY_NAME["clean_project"]
    score = ev.score_debug({"findings": [finding("greeting.py", 7, 7)]}, fixture)
    assert score["findings"][0]["tier"] == "untiered" and score["reports_problem"] and score["debug_outcome"] is None


def test_the_outcome_field_decides_when_it_is_present():
    fixture = BY_NAME["clean_project"]
    body = {"debug_outcome": "no_clear_problem", "findings": [finding("greeting.py", 7, 7, tier="worth_checking")]}
    score = ev.score_debug(body, fixture)
    assert not score["reports_problem"] and score["untiered_reports_problem"]


def test_aggregation_separates_clean_and_buggy_runs():
    clean, buggy = BY_NAME["clean_project"], BY_NAME["off_by_one_py"]
    scores = [
        ev.score_debug({"debug_outcome": "no_clear_problem", "findings": []}, clean),
        ev.score_debug({"debug_outcome": "possible_problems", "findings": [finding("greeting.py", 7, 7, tier="possible_problem")]}, clean),
        ev.score_debug({"debug_outcome": "possible_problems", "findings": [finding("cart.py", 7, 7, tier="possible_problem")]}, buggy),
    ]
    agg = ev.aggregate_debug(scores)
    assert (agg["clean_runs"], agg["clean_no_clear_problem"], agg["bug_runs"], agg["bug_cited_any"], agg["bug_cited_possible_tier"]) == (2, 1, 1, 1, 1)
    assert agg["clean_nonbug_items_in_possible_tier_per_run"] == 0.5


def test_ambiguous_code_reporting_a_problem_is_an_overclaim():
    fixture = BY_NAME["ambiguous_slice"]
    assert ev.score_debug({"debug_outcome": "possible_problems", "findings": []}, fixture)["overclaim"]
    assert not ev.score_debug({"debug_outcome": "no_clear_problem", "findings": []}, fixture)["overclaim"]


# --------------------------------------------------------------------------- #
# The corrected jargon metric
# --------------------------------------------------------------------------- #
def test_everyday_words_with_a_technical_sense_are_no_longer_headline_jargon():
    report = ev.jargon_report("The loop goes through a list and returns the count.")
    assert report["true_jargon"] == []
    assert {"loop", "list", "returns"} <= set(report["everyday_technical"])
    assert report["phase4_style_per_100_words"] > report["true_jargon_per_100_words"] == 0


def test_real_jargon_is_counted_and_an_inline_definition_removes_it():
    assert "iterate" in ev.jargon_report("It will iterate over each name.")["true_jargon"]
    assert "parameter" in ev.jargon_report("It takes a parameter called price.")["true_jargon"]
    assert ev.jargon_report("It takes a parameter (a piece of information you give a function).")["true_jargon"] == ["function"]
    assert ev.jargon_report("")["true_jargon_per_100_words"] == 0.0


def test_list_was_the_biggest_inflator_of_the_old_metric():
    text = "A list of names. Each list item is checked. The list is sorted." + " Plain words only here." * 5
    report = ev.jargon_report(text)
    assert report["true_jargon_per_100_words"] == 0.0 and report["phase4_style_per_100_words"] > 5


# --------------------------------------------------------------------------- #
# Explanation checks
# --------------------------------------------------------------------------- #
def test_invented_identifiers_are_names_that_appear_nowhere_in_the_project():
    fixture = BY_NAME["multi_file_py"]
    project = ev.project_of(fixture)
    body = {"summary": "It calls `line_total` and then `compute_discount_rate`.", "role_in_app": "", "explanations": []}
    assert ev.invented_identifiers(body, project) == ["compute_discount_rate"]


def test_a_role_that_names_unrelated_files_or_only_speculates_is_flagged():
    fixture = BY_NAME["multi_file_py"]
    project = ev.project_of(fixture)
    generic = ev.role_checks({"role_in_app": "It could be used for data filtering, statistical analysis, or games."}, project, "shop/cart.py")
    assert generic["generic"]
    grounded = ev.role_checks({"role_in_app": "main.py uses it to build an order."}, project, "shop/cart.py")
    assert not grounded["generic"] and grounded["mentions_non_neighbour"] == []
    wrong = ev.role_checks({"role_in_app": "It is called from the entry file."}, project, "shop/pricing.py")
    assert not wrong["generic"]


def test_explicit_wrong_ownership_claims_are_contradictions():
    fixture = BY_NAME["multi_file_py"]
    project = ev.project_of(fixture)
    bad = {"summary": "The format_money method of the Cart class turns a number into text.", "role_in_app": "", "explanations": []}
    good = {"summary": "The total method of the Cart class adds up the lines.", "role_in_app": "", "explanations": []}
    assert ev.relationship_contradictions(bad, project)
    assert ev.relationship_contradictions(good, project) == []


# --------------------------------------------------------------------------- #
# Replay runs the real service on a recorded draft
# --------------------------------------------------------------------------- #
def test_a_recorded_draft_replays_through_the_production_service_without_a_model():
    fixture = BY_NAME["off_by_one_py"]
    draft = {"type": "DebugDraft", "value": {"summary": "x", "findings": [{
        "file_path": "cart.py", "evidence": "7 |     for i in range(1, len(prices)):", "start_line": 7, "end_line": 7, "title": "Skips first",
        "category": "off_by_one", "severity": "medium", "confidence": "medium", "problem": "p", "what_could_happen": "w",
        "likely_cause": "c", "suggestion": "s"}]}}
    body = asyncio.run(ev.replay_response(fixture, draft))
    assert body["intent"] == "debug" and body["findings"][0]["verification"] == "source_verified"
    assert ev.score_debug(body, fixture)["bug_cited_any"]


def test_the_blinded_review_sheet_hides_the_variant(tmp_path):
    answers = [{"variant": f"v{i}", "fixture": "simple_loop", "response": {"summary": f"s{i}", "explanations": []}} for i in range(4)]
    sheet, key = tmp_path / "sheet.json", tmp_path / "key.json"
    ev.write_review_sheet(answers, sheet, key)
    text = sheet.read_text(encoding="utf-8")
    assert "variant" not in text and "v0" not in text
    assert {v["variant"] for v in json.loads(key.read_text(encoding="utf-8")).values()} == {"v0", "v1", "v2", "v3"}
