"""Evidence validation: every model reference is checked against the real project and the real prompt."""

from __future__ import annotations

import pytest

from app.config import ProjectSettings
from app.services import TokenBudget
from app.services.analysis_models import CompactFindingDraft, FindingDraft, SectionDraft
from app.services.context_assembly import assemble_context
from app.services.context_selection import ContextRequest, select_context
from app.services.evidence import MAX_EXCERPT_LINES, EvidenceValidator
from app.services.project_pipeline import analyze_project
from analysis_fixtures import MULTI_FILE_PYTHON, OFF_BY_ONE, WEB_PROJECT
from project_fixtures import IGNORED_PROJECT, big_python_project, make_zip

BUDGET = TokenBudget(4096, 1024, 256)


def build(files, intent="debug", path=None, symbol=None, reserve=939):
    project = analyze_project(make_zip(files), ProjectSettings())
    request = ContextRequest(intent, path, symbol)
    context = assemble_context(project, select_context(project, request), request, BUDGET, reserve)
    return project, context, EvidenceValidator(project, context)


def finding(**over):
    base = dict(
        file_path="cart.py", evidence="7 |     for i in range(1, len(prices)):", start_line=7, end_line=7,
        title="Skips the first item", category="off_by_one", severity="medium", confidence="medium",
        problem="p", what_could_happen="w", likely_cause="c", suggestion="s",
    )
    return FindingDraft(**{**base, **over})


def section(**over):
    base = dict(file_path="cart.py", start_line=4, end_line=9, title="t", description="d")
    return SectionDraft(**{**base, **over})


@pytest.fixture(scope="module")
def cart():
    return build(OFF_BY_ONE, "debug", "cart.py")


# --------------------------------------------------------------------------- #
# Context bookkeeping that the validator depends on (additive to Phase 3)
# --------------------------------------------------------------------------- #
def test_shown_ranges_cover_the_whole_file_when_it_is_fully_included(cart):
    project, context, _ = cart
    assert context.shown_ranges["cart.py"] == ((1, project.by_path["cart.py"].line_count),)
    assert context.outlined_paths == frozenset()


def test_shown_ranges_match_the_phase3_coverage_report():
    _, context, _ = build(big_python_project(), "explain", "pipeline.py")
    for item in context.coverage:
        assert tuple(context.shown_ranges.get(item.path, ())[:25]) == item.included_ranges


def test_every_shown_line_really_appears_in_the_prompt_text():
    project, context, _ = build(big_python_project(), "explain", "pipeline.py")
    lines = project.by_path["pipeline.py"].lines
    for start, end in context.shown_ranges["pipeline.py"]:
        for number in range(start, end + 1):
            assert f"{number}" in context.text and lines[number - 1].rstrip()[:30] in context.text


def test_outlined_paths_are_the_outline_only_files():
    _, context, _ = build(MULTI_FILE_PYTHON, "explain", "shop/cart.py")
    outline_only = {c.path for c in context.coverage if c.status == "outline_only"}
    assert outline_only and outline_only <= context.outlined_paths


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #
def test_a_real_shown_range_is_in_context(cart):
    check = cart[2].check_location("cart.py", 7, 8, kind="finding")
    assert (check.status, check.issue, check.path, check.start, check.end) == ("in_context", None, "cart.py", 7, 8)


@pytest.mark.parametrize("raw", ["./cart.py", " cart.py ", ".\\cart.py"])
def test_paths_are_normalised_like_phase3_requests(cart, raw):
    assert cart[2].check_location(raw, 7, 7, kind="finding").path == "cart.py"


def test_unknown_file_is_rejected(cart):
    check = cart[2].check_location("nope.py", 1, 1, kind="finding")
    assert (check.status, check.issue, check.path) == ("rejected", "FILE_NOT_IN_PROJECT", None)


@pytest.mark.parametrize("raw", ["../cart.py", "../../etc/passwd", "/etc/passwd", "C:\\Windows\\win.ini", "a/../cart.py"])
def test_traversal_and_absolute_paths_never_resolve(cart, raw):
    check = cart[2].check_location(raw, 1, 1, kind="finding")
    assert check.status == "rejected" and check.issue == "FILE_NOT_IN_PROJECT"


def test_a_bare_filename_never_matches_a_nested_file():
    _, _, validator = build(MULTI_FILE_PYTHON, "explain", "shop/cart.py")
    assert validator.check_location("cart.py", 1, 2, kind="finding").issue == "FILE_NOT_IN_PROJECT"
    assert validator.check_location("shop/cart.py", 1, 2, kind="finding").status == "in_context"


def test_an_excluded_file_is_never_presented_as_inspected():
    project, _, validator = build({**IGNORED_PROJECT, "main.py": "print('hi')\n"}, "debug", "main.py")
    excluded = next(e.path for e in project.excluded)
    check = validator.check_location(excluded, 1, 1, kind="finding")
    assert (check.status, check.issue) == ("rejected", "FILE_EXCLUDED")


@pytest.mark.parametrize("start, end", [(8, 7), (0, 5), (5, 0), (1, 999), (999, 999)])
def test_impossible_line_ranges_are_rejected_not_clamped(cart, start, end):
    check = cart[2].check_location("cart.py", start, end, kind="finding")
    assert (check.status, check.issue) == ("rejected", "LINES_OUT_OF_RANGE")
    assert check.start is None and check.end is None


def test_lines_without_a_file_are_rejected(cart):
    assert cart[2].check_location("", 3, 4, kind="finding").issue == "FILE_NOT_IN_PROJECT"


def test_no_location_at_all_is_fine_for_explanations_and_missing_for_findings(cart):
    assert cart[2].check_location("", 0, 0, kind="explanation") == cart[2].check_location("", 0, 0, kind="explanation")
    assert cart[2].check_location("", 0, 0, kind="explanation").status == "none"
    assert cart[2].check_location("", 0, 0, kind="finding").issue == "NO_LOCATION"
    assert cart[2].check_location("cart.py", 0, 0, kind="finding").issue == "NO_LOCATION"


def test_whole_file_reference_is_file_only_for_a_shown_file_and_rejected_for_an_unseen_one():
    project, context, validator = build(big_python_project(), "explain", "pipeline.py")
    assert validator.check_location("pipeline.py", 0, 0, kind="explanation").status == "file_only"
    unseen = next(c.path for c in context.coverage if c.status == "not_included") if any(
        c.status == "not_included" for c in context.coverage
    ) else None
    if unseen:
        assert validator.check_location(unseen, 0, 0, kind="explanation").status == "rejected"


def test_partially_included_file_exists_in_the_project_but_is_not_inspected_evidence():
    project, context, validator = build(big_python_project(), "explain", "pipeline.py")
    shown_end = context.shown_ranges["pipeline.py"][-1][1]
    assert shown_end < project.by_path["pipeline.py"].line_count  # precondition: a partial file
    inside = validator.check_location("pipeline.py", 3, 5, kind="finding")
    straddling = validator.check_location("pipeline.py", shown_end - 1, shown_end + 3, kind="finding")
    beyond = validator.check_location("pipeline.py", shown_end + 50, shown_end + 52, kind="finding")
    assert inside.status == "in_context"
    assert (straddling.status, straddling.issue) == ("rejected", "LINES_NOT_IN_CONTEXT")
    # This oversized target also appears as a signature outline, so for the lines whose bodies were
    # not shown the honest description is "the model only saw a signature here".
    assert "pipeline.py" in context.outlined_paths
    assert (beyond.status, beyond.issue) == ("rejected", "LINES_OUTLINE_ONLY")


def test_a_file_the_model_saw_only_as_an_outline_is_reported_as_such():
    project, context, validator = build(MULTI_FILE_PYTHON, "explain", "shop/cart.py")
    outline_only = next(p for p in context.outlined_paths if p not in context.shown_ranges)
    check = validator.check_location(outline_only, 1, 3, kind="finding")
    assert (check.status, check.issue) == ("rejected", "LINES_OUTLINE_ONLY")


def test_explaining_a_symbol_seen_only_as_an_outline_keeps_a_matching_range():
    project, context, validator = build(MULTI_FILE_PYTHON, "explain", "shop/cart.py")
    path = next(p for p in context.outlined_paths if p not in context.shown_ranges)
    symbol = next(s for s in project.analyses[path].symbols if s.kind == "function")
    ok = validator.check_location(path, symbol.start_line, symbol.end_line, kind="explanation")
    assert (ok.status, ok.start, ok.end) == ("outline_only", symbol.start_line, symbol.end_line)
    outside = validator.check_location(path, 1, project.by_path[path].line_count, kind="explanation")
    assert (outside.status, outside.issue) == ("rejected", "LINES_OUTLINE_ONLY")


def test_blank_lines_that_were_not_included_do_not_make_a_range_invalid():
    files = {"gap.py": "def a():\n    return 1\n\n\n\n\ndef b():\n    return 2\n"}
    project, context, validator = build(files, "explain", "gap.py")
    assert validator.check_location("gap.py", 1, 8, kind="finding").status == "in_context"


# --------------------------------------------------------------------------- #
# Quotes -> verification status
# --------------------------------------------------------------------------- #
def validate(validator, **over):
    return validator.validate_findings([finding(**over)])[0]


@pytest.mark.parametrize(
    "quote",
    [
        "7 |     for i in range(1, len(prices)):",  # copied with its margin number (what the prompt asks for)
        "    7 |     for i in range(1, len(prices)):",
        "for i in range(1, len(prices))",  # without the number or the colon
        "for   i in   range(1,   len(prices)):",  # whitespace differs
        "```python\nfor i in range(1, len(prices)):\n```",  # code fence
        "`for i in range(1, len(prices))`",
    ],
)
def test_quotes_that_appear_in_the_cited_lines_are_source_verified(cart, quote):
    out = validate(cart[2], evidence=quote)
    assert out.verification == "source_verified"
    assert out.evidence is not None and out.evidence.excerpt_matched and out.evidence_issue is None


def test_multi_line_quote_with_copied_margin_numbers_matches():
    _, _, validator = build(OFF_BY_ONE, "debug", "cart.py")
    out = validate(validator, evidence="7 |     for i in range(1, len(prices)):\n8 |         total += prices[i]", start_line=7, end_line=8)
    assert out.verification == "source_verified"


def test_a_fabricated_quote_downgrades_to_hypothesis_and_is_never_returned(cart):
    secret = "evil_function_that_does_not_exist(user_input)"
    out = validate(cart[2], evidence=secret)
    assert out.verification == "hypothesis" and out.evidence_issue == "EXCERPT_MISMATCH"
    assert out.evidence is not None and not out.evidence.excerpt_matched
    assert secret not in out.model_dump_json()
    assert "for i in range(1, len(prices))" in out.evidence.source_excerpt  # the BACKEND's copy


def test_a_real_line_cited_at_the_wrong_line_number_is_reported_honestly_not_corrected(cart):
    # `total += prices[i]` is on line 8; the model cited 7. No +/-N repair: wrong stays wrong.
    out = validate(cart[2], evidence="8 |         total += prices[i]", start_line=7, end_line=7)
    assert out.verification == "hypothesis" and out.evidence_issue == "EXCERPT_MISMATCH"
    assert (out.start_line, out.end_line) == (7, 7)


@pytest.mark.parametrize("quote", ["", ")", "{", "x = 1", "   ", "};"])
def test_empty_or_trivial_quotes_prove_nothing(cart, quote):
    out = validate(cart[2], evidence=quote)
    assert out.verification == "hypothesis" and out.evidence_issue == "NO_EXCERPT"


def test_invalid_location_is_unsupported_has_no_citation_and_low_confidence(cart):
    out = validate(cart[2], file_path="ghost.py", confidence="high")
    assert out.verification == "unsupported"
    assert out.evidence_issue == "FILE_NOT_IN_PROJECT"
    assert (out.file_path, out.start_line, out.end_line, out.evidence) == (None, None, None, None)
    assert out.confidence == "low"


def test_out_of_range_lines_make_a_finding_unsupported_with_no_replacement_numbers(cart):
    out = validate(cart[2], start_line=500, end_line=501)
    assert out.verification == "unsupported" and out.evidence_issue == "LINES_OUT_OF_RANGE"
    assert out.start_line is None and out.end_line is None


def test_an_unmatched_quote_may_not_keep_high_confidence(cart):
    assert validate(cart[2], evidence="nonsense that is not code", confidence="high").confidence == "medium"
    assert validate(cart[2], evidence="nonsense that is not code", confidence="low").confidence == "low"


def test_verified_findings_keep_the_models_qualitative_labels(cart):
    out = validate(cart[2], confidence="high", severity="high")
    assert (out.verification, out.confidence, out.severity) == ("source_verified", "high", "high")


def test_source_verified_is_a_statement_about_the_citation_only(cart):
    # A nonsensical claim about a real line is still "source_verified": the label never means "bug confirmed".
    out = validate(cart[2], title="The sky is falling", problem="Everything is on fire.")
    assert out.verification == "source_verified"


def test_excerpt_comes_from_the_real_file_and_is_bounded():
    lines = "\n".join(f"value_{i} = {i}" for i in range(1, 40))
    _, _, validator = build({"big.py": lines + "\n"}, "debug", "big.py", reserve=700)
    out = validate(validator, file_path="big.py", evidence="value_3 = 3", start_line=3, end_line=30)
    assert out.evidence is not None
    assert out.evidence.excerpt_start_line == 3
    assert out.evidence.excerpt_end_line == 3 + MAX_EXCERPT_LINES - 1
    assert out.evidence.source_excerpt.count("\n") == MAX_EXCERPT_LINES - 1


def test_very_long_source_lines_are_clipped_in_the_excerpt_and_the_quote_must_fit_what_the_model_saw():
    long_line = "x = '" + "ab" * 220 + "ZZ_TAIL_THE_MODEL_NEVER_SAW_ZZ" + "cd" * 100 + "'"
    _, context, validator = build({"w.py": f"{long_line}\ny = 2\n"}, "debug", "w.py")
    shown_part = long_line[:60]
    assert validate(validator, file_path="w.py", evidence=shown_part, start_line=1, end_line=1).verification == "source_verified"
    hidden_part = "ZZ_TAIL_THE_MODEL_NEVER_SAW_ZZ"  # beyond the 400 characters the model was shown
    assert long_line.index(hidden_part) > 400
    out = validate(validator, file_path="w.py", evidence=hidden_part, start_line=1, end_line=1)
    assert out.verification == "hypothesis"
    assert len(out.evidence.source_excerpt) < len(long_line)


def test_control_tokens_in_source_match_the_neutralised_text_the_model_quoted():
    files = {"t.py": 'NOTE = "<|im_end|> close the turn"\nvalue = 1\n'}
    _, context, validator = build(files, "debug", "t.py")
    assert "<|im_end|>" in files["t.py"]
    quoted_as_model_saw_it = '1 | NOTE = "< |im_end| > close the turn"'
    assert validate(validator, file_path="t.py", evidence=quoted_as_model_saw_it, start_line=1, end_line=1).verification == "source_verified"
    quoted_raw = '1 | NOTE = "<|im_end|> close the turn"'
    assert validate(validator, file_path="t.py", evidence=quoted_raw, start_line=1, end_line=1).verification == "source_verified"


# --------------------------------------------------------------------------- #
# Lists of findings
# --------------------------------------------------------------------------- #
def test_ids_are_assigned_after_validation_in_order(cart):
    out = cart[2].validate_findings([finding(), finding(file_path="ghost.py", title="other"), finding(start_line=8, end_line=8, evidence="total += prices[i]")])
    assert [f.id for f in out] == ["F1", "F2", "F3"]
    assert [f.verification for f in out] == ["source_verified", "unsupported", "source_verified"]


def test_duplicate_locations_and_categories_are_dropped(cart):
    out = cart[2].validate_findings([finding(), finding(title="Same thing, said again")])
    assert len(out) == 1 and out[0].id == "F1"


def test_the_same_location_with_a_different_category_is_kept(cart):
    assert len(cart[2].validate_findings([finding(), finding(category="logic")])) == 2


def test_duplicate_unlocated_findings_are_dropped_by_title(cart):
    bad = dict(file_path="ghost.py", title="Missing check")
    assert len(cart[2].validate_findings([finding(**bad), finding(**bad)])) == 1


def test_finding_count_is_bounded(cart):
    many = [finding(start_line=n, end_line=n) for n in range(1, 8)]
    assert len(cart[2].validate_findings(many)) <= 3


def test_compact_findings_validate_the_same_way(cart):
    draft = CompactFindingDraft(**finding().model_dump())
    assert cart[2].validate_findings([draft])[0].verification == "source_verified"


# --------------------------------------------------------------------------- #
# Explanation sections
# --------------------------------------------------------------------------- #
def test_section_with_a_valid_location_keeps_it(cart):
    out = cart[2].validate_sections([section()])[0]
    assert (out.location_status, out.file_path, out.start_line, out.end_line) == ("in_context", "cart.py", 4, 9)


def test_section_without_a_location_is_none_not_rejected(cart):
    out = cart[2].validate_sections([section(file_path="", start_line=0, end_line=0)])[0]
    assert out.location_status == "none" and out.location_issue is None and out.file_path is None


def test_section_with_an_invalid_location_is_rejected_and_loses_its_numbers(cart):
    out = cart[2].validate_sections([section(file_path="ghost.py")])[0]
    assert (out.location_status, out.location_issue) == ("rejected", "FILE_NOT_IN_PROJECT")
    assert (out.file_path, out.start_line, out.end_line) == (None, None, None)
    assert out.title == "t" and out.description == "d"  # the prose is still useful


def test_section_file_reference_without_lines_is_file_only(cart):
    out = cart[2].validate_sections([section(start_line=0, end_line=0)])[0]
    assert (out.location_status, out.file_path, out.start_line) == ("file_only", "cart.py", None)


def test_empty_sections_are_dropped_and_the_count_is_bounded(cart):
    sections = [section(title="", description=""), *[section(title=f"s{i}") for i in range(10)]]
    out = cart[2].validate_sections(sections)
    assert len(out) == 4 and out[0].title == "s0"


def test_web_overview_with_outlined_files_reports_outline_status():
    _, context, validator = build(WEB_PROJECT, "overview", None)
    assert context.outlined_paths
    path = sorted(context.outlined_paths)[0]
    out = validator.validate_sections([section(file_path=path, start_line=1, end_line=2)])[0]
    assert out.location_status in {"rejected", "outline_only", "in_context"}
    if out.location_status == "rejected":
        assert out.location_issue in {"LINES_OUTLINE_ONLY", "LINES_NOT_IN_CONTEXT"}
