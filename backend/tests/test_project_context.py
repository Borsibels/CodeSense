"""Context selection and token-budgeted assembly."""

from __future__ import annotations

import random
import re
import time

import pytest

from app.config import ProjectSettings
from app.services import TokenBudget
from app.services.context_assembly import assemble_context, merge_ranges, subtract_ranges
from app.services.context_selection import ContextRequest, SelectionError, select_context
from app.services.project_pipeline import analyze_project
from project_fixtures import (
    CYCLE_PROJECT,
    IGNORED_PROJECT,
    MIXED_PROJECT,
    PYTHON_PROJECT,
    WEB_PROJECT,
    big_python_project,
    make_zip,
)

LIMITS = ProjectSettings()
BUDGET = TokenBudget(total_context=4096, reserved_generation=1024, safety_margin=256)  # input limit 2816
GUTTER = re.compile(r"^\s*(\d+) \|(?: (.*))?$")


def build(files, intent="overview", path=None, symbol=None, reserve=700, budget=BUDGET, project=None):
    project = project or analyze_project(make_zip(files), LIMITS)
    request = ContextRequest(intent, path, symbol)
    selection = select_context(project, request)
    return project, selection, assemble_context(project, selection, request, budget, reserve)


def shown_lines(result):
    """``{path: {line_number: text}}`` parsed back out of the rendered context."""
    out: dict[str, dict[int, str]] = {}
    current = None
    for line in result.text.split("\n"):
        if line.startswith("FILE "):
            current = line.split()[1]
            out.setdefault(current, {})
        elif line.startswith(("OUTLINE ", "SIFT")):
            current = None
        elif current:
            m = GUTTER.match(line)
            if m:
                out[current][int(m.group(1))] = m.group(2) or ""
    return out


def status(result):
    return {c.path: c.status for c in result.coverage}


# --------------------------------------------------------------------------- #
# Range helpers
# --------------------------------------------------------------------------- #
def test_range_helpers():
    assert merge_ranges([(5, 6), (1, 2), (3, 4), (10, 12), (11, 20)]) == [(1, 6), (10, 20)]
    assert subtract_ranges((1, 10), [(3, 4), (8, 12)]) == [(1, 2), (5, 7)]
    assert subtract_ranges((1, 10), []) == [(1, 10)]
    assert subtract_ranges((3, 4), [(1, 10)]) == []


# --------------------------------------------------------------------------- #
# Selection: validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("intent", ["explain", "debug"])
def test_focused_intents_need_a_file(intent):
    with pytest.raises(SelectionError) as info:
        build(PYTHON_PROJECT, intent)
    assert info.value.code == "FILE_REQUIRED"


def test_symbol_without_file_is_refused():
    with pytest.raises(SelectionError) as info:
        build(PYTHON_PROJECT, "overview", None, "run")
    assert info.value.code == "FILE_REQUIRED"


def test_unknown_file():
    with pytest.raises(SelectionError) as info:
        build(PYTHON_PROJECT, "explain", "app/nope.py")
    assert info.value.code == "FILE_NOT_FOUND"


def test_a_file_that_was_excluded_says_why():
    with pytest.raises(SelectionError) as info:
        build(PYTHON_PROJECT, "explain", "README.md")
    assert info.value.code == "FILE_NOT_ANALYZED" and "unsupported_extension" in info.value.message


def test_unknown_symbol_lists_what_exists():
    with pytest.raises(SelectionError) as info:
        build(PYTHON_PROJECT, "debug", "app/models.py", "Missing")
    assert info.value.code == "SYMBOL_NOT_FOUND"
    assert "User" in info.value.message and "User.greet" in info.value.message


@pytest.mark.parametrize("raw", ["./app/utils.py", "app\\utils.py", " app/utils.py "])
def test_path_spelling_is_normalised(raw):
    _, selection, _ = build(PYTHON_PROJECT, "explain", raw)
    assert selection.target_path == "app/utils.py"


# --------------------------------------------------------------------------- #
# Selection: prioritisation
# --------------------------------------------------------------------------- #
def test_the_selected_file_comes_first_then_dependencies_then_users():
    _, selection, _ = build(PYTHON_PROJECT, "explain", "app/models.py")
    order = [(c.path, c.kind) for c in selection.candidates]
    assert order[0] == ("app/models.py", "code")
    assert order.index(("app/utils.py", "outline")) < order.index(("app/main.py", "outline")) or ("app/main.py", "code") in order
    paths_in_order = list(dict.fromkeys(c.path for c in selection.candidates))
    assert paths_in_order[:2] == ["app/models.py", "app/utils.py"]  # file, then its direct dependency
    assert paths_in_order.index("app/main.py") > paths_in_order.index("app/utils.py")  # importer later


def test_symbol_selection_targets_only_that_symbol_plus_the_header():
    _, selection, result = build(PYTHON_PROJECT, "explain", "app/models.py", "User.greet")
    first = selection.candidates[0]
    assert (first.path, first.symbol, first.start_line, first.end_line) == ("app/models.py", "User.greet", 10, 11)
    assert selection.target_symbols == ("User.greet",)
    shown = shown_lines(result)["app/models.py"]
    assert {10, 11} <= set(shown)  # the symbol...
    assert 1 in shown  # ...and the imports above it
    assert 13 not in shown  # unrelated method is not pulled in for `explain`


def test_bare_symbol_name_matches_methods():
    _, selection, _ = build(PYTHON_PROJECT, "explain", "app/models.py", "greet")
    assert selection.target_symbols == ("User.greet",)


def test_debug_includes_only_the_dependency_code_the_target_uses():
    _, _, result = build(PYTHON_PROJECT, "debug", "app/main.py", "run")
    utils = shown_lines(result)["app/utils.py"]
    assert any("def helper" in text for text in utils.values())
    assert not any("def unused_name" in text for text in utils.values())  # imported but never mentioned in run()


def test_explain_gives_dependency_outlines_not_bodies():
    _, _, result = build(PYTHON_PROJECT, "explain", "app/main.py")
    assert "app/utils.py" not in shown_lines(result)
    assert status(result)["app/utils.py"] == "outline_only"
    assert "OUTLINE app/utils.py" in result.text and "def helper(text)" in result.text


def test_importers_are_shown_where_they_use_the_target():
    _, _, result = build(PYTHON_PROJECT, "debug", "app/utils.py", "helper")
    models = shown_lines(result)["app/models.py"]
    assert any('helper("hello "' in text for text in models.values())  # User.greet calls helper
    assert not any("def save" in text for text in models.values())


def test_overview_prefers_entry_points_and_structure_over_code():
    _, _, result = build(MIXED_PROJECT, "overview")
    assert "Entry points (heuristic): app/main.py, web/index.html" in result.text
    assert result.summary.files_outline_only + result.summary.files_full == result.summary.files_total
    assert status(result)["app/main.py"] in ("full", "outline_only")
    first_mention = min(i for i in (result.text.find("FILE app/main.py"), result.text.find("OUTLINE app/main.py")) if i >= 0)
    assert first_mention < result.text.index("OUTLINE app/utils.py")  # entry points before ordinary files


def test_dependency_information_is_part_of_the_context():
    _, _, result = build(WEB_PROJECT, "explain", "js/app.js")
    assert "js/app.js depends on: js/util.js (line 1), ./config (line 2) [MISSING from the project]" in result.text
    assert "js/app.js is used by: index.html (line 6)" in result.text


def test_import_cycles_are_mentioned_for_files_inside_them():
    _, _, result = build(CYCLE_PROJECT, "explain", "a.py")
    assert "Import cycle involving this file: a.py -> b.py -> c.py -> a.py" in result.text


def test_selection_is_deterministic():
    _, first, _ = build(MIXED_PROJECT, "debug", "app/main.py", "run")
    _, second, _ = build(MIXED_PROJECT, "debug", "app/main.py", "run")
    assert first == second


# --------------------------------------------------------------------------- #
# Assembly: line fidelity
# --------------------------------------------------------------------------- #
def test_every_numbered_line_matches_the_source_exactly():
    project, _, result = build(MIXED_PROJECT, "debug", "app/main.py", "run")
    assert shown_lines(result)
    for path, lines in shown_lines(result).items():
        source = project.by_path[path].lines
        for number, text in lines.items():
            assert text == source[number - 1].rstrip(), (path, number)


def test_gaps_are_marked_explicitly():
    _, _, result = build(PYTHON_PROJECT, "explain", "app/models.py", "User.greet")
    assert "[... lines 4-9 not included ...]" in result.text
    assert "PARTIAL: lines 1-3, 10-11 of 14" in result.text


def test_a_fully_included_file_is_labelled_full_and_has_no_gap_markers():
    _, _, result = build({"a.py": "x = 1\ny = 2\n"}, "explain", "a.py")
    assert "FILE a.py [FULL: all 2 lines]" in result.text
    assert "not included" not in result.text
    assert status(result) == {"a.py": "full"}


def test_blank_gaps_do_not_make_a_file_partial():
    project, _, result = build(PYTHON_PROJECT, "explain", "app/utils.py")
    assert status(result)["app/utils.py"] == "full"  # blank lines between functions are not "missing code"


def test_very_long_lines_are_clipped_visibly():
    long_line = "x = '" + "a" * 600 + "'\n"
    _, _, result = build({"a.py": long_line}, "explain", "a.py")
    assert "characters not shown]" in result.text
    assert any("clipped" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# Assembly: the token budget
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reserve", [0, 300, 700, 1500, 2300, 2700])
@pytest.mark.parametrize(
    "files, intent, path, symbol",
    [
        (big_python_project(), "explain", "pipeline.py", None),
        (big_python_project(), "debug", "main.py", "main"),
        (big_python_project(), "overview", None, None),
        (MIXED_PROJECT, "debug", "app/main.py", "run"),
        (MIXED_PROJECT, "overview", None, None),
        (WEB_PROJECT, "explain", "js/app.js", None),
    ],
)
def test_context_never_exceeds_the_budget(files, intent, path, symbol, reserve):
    _, _, result = build(files, intent, path, symbol, reserve)
    limit = BUDGET.input_limit - reserve
    assert result.budget.context_limit == limit
    assert BUDGET.count(result.text).tokens <= limit
    assert result.budget.estimated_tokens == BUDGET.count(result.text).tokens
    assert result.budget.remaining_tokens == limit - result.budget.estimated_tokens
    assert result.budget.is_estimate is True


def test_the_input_budget_is_the_phase_2_5_budget():
    _, _, result = build(PYTHON_PROJECT, "overview")
    assert result.budget.input_limit == 2816 == 4096 - 1024 - 256
    assert result.budget.context_limit == 2816 - 700


def test_the_instruction_reserve_is_never_spent_on_code():
    _, _, tight = build(big_python_project(), "explain", "pipeline.py", reserve=2000)
    assert BUDGET.count(tight.text).tokens <= 816
    _, _, loose = build(big_python_project(), "explain", "pipeline.py", reserve=200)
    assert loose.budget.estimated_tokens > tight.budget.estimated_tokens


def test_a_budget_too_small_for_anything_says_so():
    project = analyze_project(make_zip(PYTHON_PROJECT), LIMITS)
    request = ContextRequest("overview")
    result = assemble_context(project, select_context(project, request), request, BUDGET, BUDGET.input_limit)
    assert result.text == "" and result.summary.files_not_included == result.summary.files_total
    assert any("too small" in w for w in result.warnings)


def test_a_custom_budget_is_respected():
    small = TokenBudget(total_context=2048, reserved_generation=512, safety_margin=128)
    _, _, result = build(big_python_project(), "explain", "pipeline.py", budget=small, reserve=300)
    assert result.budget.input_limit == 1408
    assert small.count(result.text).tokens <= 1108


# --------------------------------------------------------------------------- #
# Assembly: partial coverage and omissions
# --------------------------------------------------------------------------- #
def test_oversized_target_is_partial_and_never_called_full():
    project, _, result = build(big_python_project(), "explain", "pipeline.py")
    coverage = {c.path: c for c in result.coverage}["pipeline.py"]
    assert coverage.status == "partial"
    assert coverage.omitted_ranges and coverage.included_ranges
    assert "FULL" not in result.text.split("FILE pipeline.py")[1].split("\n")[0]
    assert "PARTIAL" in result.text
    assert any("partially included" in w for w in result.warnings)
    included = sum(b - a + 1 for a, b in coverage.included_ranges)
    assert included < project.by_path["pipeline.py"].line_count


def test_oversized_target_gets_an_outline_so_the_model_sees_the_whole_file_shape():
    _, _, result = build(big_python_project(), "explain", "pipeline.py")
    assert "OUTLINE pipeline.py" in result.text and "def compute_0(" in result.text
    assert "more entries not listed" in result.text  # the outline itself reports its truncation
    cov = {c.path: c for c in result.coverage}["pipeline.py"]
    assert cov.outline_entries_listed < cov.outline_entries_total


def test_a_partial_target_does_not_starve_its_dependencies_and_users():
    _, _, result = build(big_python_project(), "explain", "pipeline.py")
    assert status(result)["main.py"] != "not_included"  # the file that imports it still appears


def test_every_requested_region_is_either_included_or_reported_omitted():
    project, selection, result = build(big_python_project(), "debug", "main.py", "main")
    coverage = {c.path: c for c in result.coverage}
    omitted = [(o.path, o.start_line, o.end_line) for o in result.omitted_regions if o.kind == "code"]
    for candidate in selection.candidates:
        if candidate.kind != "code":
            continue
        lines = project.by_path[candidate.path].lines
        covered = {n for a, b in coverage[candidate.path].included_ranges for n in range(a, b + 1)}
        reported = {n for p, a, b in omitted if p == candidate.path for n in range(a, b + 1)}
        for n in range(candidate.start_line, candidate.end_line + 1):
            if lines[n - 1].strip():
                assert n in covered or n in reported, (candidate.path, n)


def test_omitted_outlines_are_reported_by_file():
    _, _, result = build(big_python_project(), "explain", "pipeline.py", reserve=2200)
    omitted_files = {o.path for o in result.omitted_regions}
    not_included = {c.path for c in result.coverage if c.status == "not_included"}
    assert not_included <= omitted_files
    assert result.omitted_region_total >= len(result.omitted_regions)


def test_coverage_summary_adds_up():
    _, _, result = build(MIXED_PROJECT, "debug", "app/main.py", "run")
    s = result.summary
    assert s.files_total == len(result.coverage) == 9
    assert s.files_full + s.files_partial + s.files_outline_only + s.files_not_included == s.files_total


def test_a_single_oversized_symbol_contributes_a_bounded_leading_block():
    body = "def giant():\n" + "".join(f"    value_{i} = {i} * {i + 1}\n" for i in range(400)) + "    return 0\n"
    project, _, result = build({"g.py": body}, "debug", "g.py", "giant")
    cov = result.coverage[0]
    assert cov.status == "partial"
    (start, end), = cov.included_ranges
    assert start == 1 and 3 <= end < 400
    assert f"[... lines {end + 1}-" in result.text
    assert BUDGET.count(result.text).tokens <= result.budget.context_limit


def test_empty_project():
    _, _, result = build({"README.md": "# nothing\n"}, "overview")
    assert result.coverage == ()
    assert result.summary.files_total == 0
    assert any("no supported source files" in w for w in result.warnings)
    assert "Project: 0 source files" in result.text


def test_projects_with_only_ignored_files_are_empty_too():
    _, _, result = build({"node_modules/x.js": "var a;\n"}, "overview")
    assert result.summary.files_total == 0


def test_heuristic_parser_warning_appears_for_js_and_css():
    _, _, result = build(WEB_PROJECT, "overview")
    assert any("heuristic scanner" in w for w in result.warnings)
    _, _, python_only = build(PYTHON_PROJECT, "overview")
    assert not any("heuristic" in w for w in python_only.warnings)


def test_syntax_errors_are_surfaced_in_the_context():
    files = {"bad.py": "def broken(:\n    pass\n", "good.py": "x = 1\n"}
    _, _, result = build(files, "explain", "bad.py")
    assert "NOTE: static analysis reports SyntaxError" in result.text and "at line 1" in result.text
    assert any("syntax error" in w for w in result.warnings)


def test_line_numbers_can_be_switched_off():
    project = analyze_project(make_zip({"a.py": "x = 1\n"}), LIMITS)
    request = ContextRequest("explain", "a.py")
    result = assemble_context(project, select_context(project, request), request, BUDGET, 700, line_numbers=False)
    assert "x = 1" in result.text and " | x = 1" not in result.text


# --------------------------------------------------------------------------- #
# Determinism and scale
# --------------------------------------------------------------------------- #
def test_output_is_identical_across_runs_and_zip_orderings():
    items = sorted(MIXED_PROJECT.items())
    shuffled = dict(random.Random(11).sample(items, len(items)))
    _, _, first = build(dict(items), "debug", "app/main.py", "run")
    _, _, second = build(shuffled, "debug", "app/main.py", "run")
    assert first == second


def test_ignored_files_never_enter_the_context():
    _, _, result = build(IGNORED_PROJECT, "overview")
    assert "node_modules" not in result.text and "dist/" not in result.text
    assert [c.path for c in result.coverage] == ["main.py"]


def test_a_large_project_stays_within_budget_and_is_fast():
    files = {
        f"pkg{i % 10}/mod{i}.py": "".join(
            f"def func_{i}_{j}(a, b):\n    return a + b + {j}\n\n" for j in range(15)
        )
        + (f"from pkg{(i + 1) % 10} import mod{(i + 1) % 100}\n" if i % 3 == 0 else "")
        for i in range(100)
    }
    started = time.perf_counter()
    project, _, result = build(files, "overview")
    for intent, path in (("explain", "pkg1/mod1.py"), ("debug", "pkg2/mod2.py")):
        _, _, focused = build(files, intent, path, project=project)
        assert BUDGET.count(focused.text).tokens <= focused.budget.context_limit
    elapsed = time.perf_counter() - started
    assert BUDGET.count(result.text).tokens <= result.budget.context_limit
    assert result.summary.files_total == 100
    assert result.summary.files_not_included > 0  # a hundred files cannot all fit
    assert elapsed < 20  # generous: this guards against accidental quadratic blow-ups, not tuning


def test_a_file_with_thousands_of_symbols_does_not_make_assembly_quadratic():
    source = "".join(f"def f{i}(x):\n    return x + {i}\n\n" for i in range(12000))
    started = time.perf_counter()
    _, _, result = build({"huge.py": source, "main.py": "import huge\nhuge.f1(1)\n"}, "explain", "huge.py")
    assert time.perf_counter() - started < 15
    assert BUDGET.count(result.text).tokens <= result.budget.context_limit
    assert {c.path: c.status for c in result.coverage}["huge.py"] == "partial"
