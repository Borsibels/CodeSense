"""Finding triage: tiers are presentation heuristics; demoted findings stay; the outcome never claims "no bugs"."""

from __future__ import annotations

import pytest

from app.config import ProjectSettings
from app.services import TokenBudget
from app.services.analysis_models import FindingOut
from app.services.bug_patterns import run_pattern_checks
from app.services.context_assembly import assemble_context
from app.services.context_selection import ContextRequest, select_context
from app.services.finding_triage import (
    TIER_REASON_TEXT,
    debug_outcome,
    demo_ranges,
    examined_description,
    is_input_assumption,
    outcome_text,
    pattern_checks_out,
    triage_findings,
)
from app.services.project_pipeline import analyze_project
from analysis_fixtures import BY_NAME, CLEAN_PROJECT, OFF_BY_ONE
from project_fixtures import big_python_project, make_zip

BUDGET = TokenBudget(4096, 1024, 256)


def project_of(files):
    return analyze_project(make_zip(files), ProjectSettings())


def make_finding(**over) -> FindingOut:
    base = dict(
        id="F1", title="Skips the first item", category="logic", problem="The loop starts at 1.", what_could_happen="w",
        likely_cause="c", suggestion="s", severity="medium", confidence="medium", verification="source_verified",
        file_path="cart.py", start_line=7, end_line=7,
    )
    return FindingOut.model_validate({**base, **over})


def context_for(project, path, intent="debug", symbol=None, reserve=1100):
    request = ContextRequest(intent, path, symbol)
    return assemble_context(project, select_context(project, request), request, BUDGET, reserve)


# --------------------------------------------------------------------------- #
# Demo blocks
# --------------------------------------------------------------------------- #
def test_the_main_guard_block_is_a_demo_range():
    project = project_of(OFF_BY_ONE)
    [(start, end)] = demo_ranges(project, "cart.py")
    lines = OFF_BY_ONE["cart.py"].split("\n")
    assert lines[start - 1].startswith("if __name__") and end >= start + 2


def test_a_guard_written_the_other_way_round_or_inside_a_try_is_found_too():
    files = {"a.py": 'try:\n    if "__main__" == __name__:\n        print(1)\nexcept ImportError:\n    pass\n'}
    assert demo_ranges(project_of(files), "a.py") == [(2, 3)]


def test_other_ifs_other_languages_and_broken_files_have_no_demo_range():
    assert demo_ranges(project_of({"a.py": "if x == 1:\n    pass\n"}), "a.py") == []
    assert demo_ranges(project_of({"a.js": "if (require.main === module) { run(); }\n"}), "a.js") == []
    assert demo_ranges(project_of({"a.py": "def f(:\n"}), "a.py") == []
    assert demo_ranges(project_of({"a.py": "x = 1\n"}), "missing.py") == []


# --------------------------------------------------------------------------- #
# "The claim is about unseen input": categories of claim, not fixture wording
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "title, problem",
    [
        ("Possible crash on empty list", "Iterating an empty list raises an error."),
        ("Null pointer risk", "The value may be null when the form is blank."),
        ("Missing input validation", "The function trusts what it is given."),
        ("No validation of the amount", "Negative amounts are accepted."),
        ("Lack of error handling", "Nothing catches a failure."),
        ("Division by zero", "The ratio divides by a count that may be 0."),
        ("Potential ZeroDivisionError", "x"),
        ("Unexpected input", "A string could be passed where a number is expected."),
        ("Invalid data type", "x"),
        ("Edge case not covered", "x"),
        ("Corner case", "x"),
        ("Check for missing values", "x"),
        ("Return value", "The result is None when nothing matches."),
        ("Undefined access", "The property may be undefined."),
        ("Unsanitized text", "x"),
        ("Value problem", "Negative numbers are not handled and negative quantities break the total."),
        ("Missing check", "x"),
        ("Insufficient checking", "x"),
        ("Boundary condition", "x"),
        ("Handles NaN badly", "x"),
    ],
)
def test_claims_that_depend_on_input_the_code_never_saw_are_recognised(title, problem):
    assert is_input_assumption(title, problem)


@pytest.mark.parametrize(
    "title, problem",
    [
        ("Off-by-one error in loop", "The loop starts at index 1, so it skips the first element of the list."),
        ("Wrong comparison operator", "The condition uses > where >= is needed, so 18 is excluded."),
        ("Subtracts the wrong thing", "The function subtracts the percent itself instead of a percentage of the price."),
        ("Method passed without parentheses", "cart.total is a method but format_money receives it uncalled."),
        ("Reversed comparison", "The check keeps the smaller value, so it finds the minimum, not the maximum."),
        ("Unused variable", "The variable discount is assigned but never used."),
        ("Variable used before assignment", "total is read before it is set."),
        ("Wrong id in the page lookup", "The script looks for an element id that the page does not define."),
        ("Infinite loop", "The loop variable is never advanced, so the loop runs forever."),
        # The English word "none" is prose, not the Python value.
        ("Duplicated logic", "None of the branches differ, so the condition is redundant."),
    ],
)
def test_logic_claims_about_the_code_itself_are_not_input_assumptions(title, problem):
    assert not is_input_assumption(title, problem)


def test_only_the_title_and_the_start_of_the_problem_are_read():
    long_problem = "The loop skips the first element of the list because it starts at one. " + "Also this fails for empty input." * 3
    assert len(long_problem[:90]) < len(long_problem)
    assert not is_input_assumption("Skips first item", long_problem)


# --------------------------------------------------------------------------- #
# Tiers
# --------------------------------------------------------------------------- #
def test_a_verified_finding_in_ordinary_code_is_a_possible_problem_with_no_reasons():
    project = project_of(OFF_BY_ONE)
    [out] = triage_findings(project, [make_finding()], []).findings
    assert (out.tier, out.tier_reasons) == ("possible_problem", [])


def test_every_demoting_reason_demotes_and_is_reported():
    project = project_of(OFF_BY_ONE)
    demo_line = OFF_BY_ONE["cart.py"].split("\n").index('    items = [10.0, 5.5, 3.25]') + 1
    cases = {
        "QUOTE_NOT_MATCHED": make_finding(verification="hypothesis"),
        "LOCATION_NOT_VERIFIED": make_finding(verification="unsupported", file_path=None, start_line=None, end_line=None),
        "IN_DEMO_CODE": make_finding(start_line=demo_line, end_line=demo_line),
        "INPUT_ASSUMPTION": make_finding(title="Missing input validation"),
    }
    for reason, finding in cases.items():
        [out] = triage_findings(project, [finding], []).findings
        assert out.tier == "worth_checking" and reason in out.tier_reasons, reason
        assert reason in TIER_REASON_TEXT


def test_demoted_findings_are_never_removed_or_edited():
    project = project_of(OFF_BY_ONE)
    originals = [make_finding(id="F1", title="Missing input validation"), make_finding(id="F2", verification="hypothesis"), make_finding(id="F3")]
    result = triage_findings(project, originals, []).findings
    assert len(result) == 3
    by_title = {f.title for f in result}
    assert by_title == {"Missing input validation", "Skips the first item"}
    for before in originals:
        after = next(f for f in result if f.verification == before.verification and f.title == before.title)
        assert after.model_dump(exclude={"id", "tier", "tier_reasons"}) == before.model_dump(exclude={"id", "tier", "tier_reasons"})


def test_possible_problems_come_first_keep_their_order_and_ids_follow_the_display_order():
    project = project_of(OFF_BY_ONE)
    originals = [
        make_finding(id="F1", title="Missing input validation"),  # demoted
        make_finding(id="F2", title="Loop skips item A"),
        make_finding(id="F3", verification="hypothesis", title="Looks odd"),  # demoted
        make_finding(id="F4", title="Loop skips item B"),
    ]
    result = triage_findings(project, originals, []).findings
    assert [(f.id, f.title, f.tier) for f in result] == [
        ("F1", "Loop skips item A", "possible_problem"),
        ("F2", "Loop skips item B", "possible_problem"),
        ("F3", "Missing input validation", "worth_checking"),
        ("F4", "Looks odd", "worth_checking"),
    ]


def test_an_independent_rule_on_the_same_lines_overrides_the_demotion_and_says_so():
    project = project_of(OFF_BY_ONE)
    hits = run_pattern_checks(project, "cart.py")
    finding = make_finding(title="Missing input validation", verification="hypothesis")  # would be demoted twice
    result = triage_findings(project, [finding], hits)
    [out] = result.findings
    assert out.tier == "possible_problem"
    assert out.tier_reasons == ["QUOTE_NOT_MATCHED", "INPUT_ASSUMPTION", "CORROBORATED_BY_RULE"]
    assert result.corroborated_by == {0: ["F1"]}


def test_a_rule_on_other_lines_or_in_another_file_does_not_corroborate():
    project = project_of(OFF_BY_ONE)
    hits = run_pattern_checks(project, "cart.py")  # focus lines 7-8
    [out] = triage_findings(project, [make_finding(start_line=12, end_line=12, title="Missing input validation")], hits).findings
    assert out.tier == "worth_checking" and "CORROBORATED_BY_RULE" not in out.tier_reasons
    [out] = triage_findings(project, [make_finding(file_path="other.py", title="Missing input validation")], hits).findings
    assert "CORROBORATED_BY_RULE" not in out.tier_reasons


def test_triage_is_deterministic_and_does_not_mutate_its_input():
    project = project_of(OFF_BY_ONE)
    findings = [make_finding(title="Missing input validation"), make_finding()]
    snapshot = [f.model_dump() for f in findings]
    first = triage_findings(project, findings, []).findings
    second = triage_findings(project, findings, []).findings
    assert [f.model_dump() for f in first] == [f.model_dump() for f in second]
    assert [f.model_dump() for f in findings] == snapshot


# --------------------------------------------------------------------------- #
# Outcome and summary
# --------------------------------------------------------------------------- #
def triaged(project, findings, path="cart.py", depth="beginner"):
    context = context_for(project, path)
    hits = run_pattern_checks(project, path)
    result = triage_findings(project, findings, hits)
    checks = pattern_checks_out(project, hits, result.corroborated_by, context, depth)
    outcome = debug_outcome(result.findings, hits)
    return outcome, result.findings, checks, context


def test_no_findings_and_no_rule_hits_is_no_clear_problem_that_is_not_a_proof():
    project = project_of(CLEAN_PROJECT)
    outcome, findings, checks, context = triaged(project, [], "greeting.py")
    text = outcome_text(outcome, findings, checks, context, project)
    assert outcome == "no_clear_problem"
    assert text.startswith("No clear problem was identified in the code examined")
    assert "not proof" in text and "bug-free" in text and "nothing was run" in text
    assert "all 19 lines of greeting.py" in text


def test_demoted_notes_alone_never_flip_the_outcome_but_are_counted_in_the_summary():
    project = project_of(CLEAN_PROJECT)
    notes = [make_finding(file_path="greeting.py", start_line=8, end_line=8, title="Missing input validation"),
             make_finding(file_path="greeting.py", start_line=9, end_line=9, title="Empty input")]
    outcome, findings, checks, context = triaged(project, notes, "greeting.py")
    text = outcome_text(outcome, findings, checks, context, project)
    assert outcome == "no_clear_problem" and all(f.tier == "worth_checking" for f in findings)
    assert "2 notes worth a second look are listed below" in text


def test_a_possible_problem_finding_makes_the_outcome_possible_problems_without_claiming_a_bug():
    project = project_of(OFF_BY_ONE)
    outcome, findings, checks, context = triaged(project, [make_finding(start_line=12, end_line=12, title="Odd discount maths")])
    text = outcome_text(outcome, findings, checks, context, project)
    assert outcome == "possible_problems"
    assert "possible problem" in text and "suspicions, not confirmed bugs" in text
    assert "no clear problem" not in text.lower() and "bug-free" not in text


def test_a_rule_hit_alone_makes_the_outcome_possible_problems_even_when_the_ai_found_nothing():
    files = BY_NAME["injection_project"].files
    project = project_of(files)
    outcome, findings, checks, context = triaged(project, [], "report.py")
    assert findings == [] and outcome == "possible_problems"
    [check] = checks
    assert check.rule == "PY_INDEX_PAST_END" and check.strength == "problem_if_assumptions_hold" and check.assumptions
    assert "1 possible problem found" in outcome_text(outcome, findings, checks, context, project)


def test_a_worth_checking_rule_hit_is_a_note_not_a_problem():
    project = project_of(OFF_BY_ONE)
    outcome, findings, checks, context = triaged(project, [])
    assert outcome == "no_clear_problem"
    assert [c.strength for c in checks] == ["worth_checking"]
    assert "1 note worth a second look is listed below" in outcome_text(outcome, findings, checks, context, project)


def test_a_rule_that_corroborates_a_finding_is_not_double_counted():
    project = project_of(BY_NAME["injection_project"].files)
    finding = make_finding(file_path="report.py", start_line=17, end_line=17, title="Index past the end")
    outcome, findings, checks, context = triaged(project, [finding], "report.py")
    assert checks[0].corroborates == ["F1"]
    assert "1 possible problem found" in outcome_text(outcome, findings, checks, context, project)


def test_the_summary_says_what_part_of_the_file_was_actually_examined():
    files = big_python_project(functions=40, lines_each=14)
    project = project_of(files)
    context = context_for(project, "pipeline.py")
    total = project.by_path["pipeline.py"].line_count
    text = examined_description(context, project)
    assert text.startswith("pipeline.py, lines 1-") and f"of {total}" in text and "the rest was not shown" in text
    summary = outcome_text("no_clear_problem", [], [], context, project)
    assert "the rest was not shown" in summary and "not proof" in summary


def test_files_that_were_not_shown_are_counted_in_the_summary():
    import dataclasses

    project = project_of(CLEAN_PROJECT)
    context = context_for(project, "greeting.py")
    context = dataclasses.replace(context, summary=dataclasses.replace(context.summary, files_not_included=3))
    assert "3 other source files were not shown to the AI at all" in outcome_text("no_clear_problem", [], [], context, project)
    one = dataclasses.replace(context, summary=dataclasses.replace(context.summary, files_not_included=1))
    assert "1 other source file was not shown to the AI at all" in outcome_text("no_clear_problem", [], [], one, project)


def test_pattern_hits_outside_the_shown_lines_are_flagged_as_such():
    from analysis_fixtures import hidden_bug_files

    project = project_of(hidden_bug_files())
    outcome, findings, checks, context = triaged(project, [], "pipeline.py")
    [check] = [c for c in checks if c.rule == "PY_INDEX_PAST_END"]
    assert check.shown_to_ai is False and outcome == "possible_problems"
    assert "not shown" in outcome_text(outcome, findings, checks, context, project)


def test_pattern_checks_copy_their_excerpt_from_the_file_and_use_the_requested_depth():
    project = project_of(BY_NAME["injection_project"].files)
    _, _, beginner, _ = triaged(project, [], "report.py", depth="beginner")
    _, _, advanced, _ = triaged(project, [], "report.py", depth="advanced")
    assert "for i in range(len(scores) + 1)" in beginner[0].source_excerpt
    assert beginner[0].explanation != advanced[0].explanation and "IndexError" in advanced[0].explanation
    assert beginner[0].shown_to_ai and beginner[0].parser == "confirmed"


def test_outcome_wording_never_asserts_the_absence_of_bugs():
    project = project_of(CLEAN_PROJECT)
    outcome, findings, checks, context = triaged(project, [], "greeting.py")
    text = outcome_text(outcome, findings, checks, context, project).lower()
    for forbidden in ("no bugs", "bug free", "is safe", "no problems", "nothing wrong", "all good", "looks correct"):
        assert forbidden not in text
    assert "not proof" in text and "has not been proven bug-free" in text  # the negated forms are the only ones allowed
