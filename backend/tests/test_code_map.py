"""The Phase 4.5 CODE MAP: exact class/function membership from the parser, in the explain/overview context only."""

from __future__ import annotations

import pytest

from app.config import ProjectSettings
from app.services import TokenBudget
from app.services.context_assembly import CODE_MAP_MAX_IMPORTS, CODE_MAP_MAX_TOKENS, assemble_context
from app.services.context_selection import ContextRequest, select_context
from app.services.project_pipeline import analyze_project
from analysis_fixtures import JS_LOGIC_ERROR, MULTI_FILE_PYTHON, WEB_PROJECT
from project_fixtures import make_zip

BUDGET = TokenBudget(4096, 1024, 256)


def context(files, path, intent="explain", symbol=None, reserve=1100, budget=BUDGET):
    project = analyze_project(make_zip(files), ProjectSettings())
    request = ContextRequest(intent, path, symbol)
    return project, assemble_context(project, select_context(project, request), request, budget, reserve)


def code_map_block(ctx) -> str:
    lines = ctx.text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith("CODE MAP")), None)
    if start is None:
        return ""
    end = start + 1
    while end < len(lines) and (lines[end].startswith("  L") or lines[end].startswith("  imported:")):
        end += 1
    return "\n".join(lines[start:end])


def test_a_python_class_is_mapped_with_its_exact_methods_and_the_names_it_imports():
    _, ctx = context(MULTI_FILE_PYTHON, "shop/cart.py")
    assert code_map_block(ctx) == (
        "CODE MAP of shop/cart.py (Python parser, exact):\n"
        "  L5-13 class Cart | L6-7 method Cart.__init__ | L9-10 method Cart.add | L12-13 method Cart.total\n"
        "  imported: line_total from shop/pricing.py (function, L4-5)"
    )


def test_a_function_is_never_listed_under_a_class_it_does_not_belong_to():
    _, ctx = context(MULTI_FILE_PYTHON, "shop/cart.py")
    block = code_map_block(ctx)
    assert "format_money" not in block and "Cart.line_total" not in block
    assert "method Cart.total" in block and "function line_total" not in block


def test_javascript_is_labelled_heuristic_not_exact():
    _, ctx = context(JS_LOGIC_ERROR, "stats.js")
    block = code_map_block(ctx)
    assert "JavaScript scanner, heuristic" in block and "function findLargest" in block and "exact" not in block


def test_the_map_is_for_explain_and_overview_only_so_the_debug_context_is_unchanged():
    _, debug_ctx = context(MULTI_FILE_PYTHON, "shop/cart.py", intent="debug")
    assert "CODE MAP" not in debug_ctx.text
    _, overview_ctx = context(MULTI_FILE_PYTHON, "shop/cart.py", intent="overview")
    assert "CODE MAP of shop/cart.py" in overview_ctx.text


def test_an_overview_without_a_focus_file_has_no_map():
    _, ctx = context(MULTI_FILE_PYTHON, None, intent="overview")
    assert "CODE MAP" not in ctx.text


def test_html_and_css_targets_have_no_map():
    _, ctx = context(WEB_PROJECT, "index.html")
    assert "CODE MAP" not in ctx.text


def many_methods(n):
    body = "\n".join(f"    def method_number_{i}(self, value):\n        return value + {i}\n" for i in range(n))
    return {"big.py": f"class Big:\n{body}"}


def test_the_map_is_capped_at_120_estimated_tokens_and_says_how_much_it_left_out():
    project, ctx = context(many_methods(60), "big.py")
    block = code_map_block(ctx)
    assert BUDGET.count(block + "\n").tokens <= CODE_MAP_MAX_TOKENS
    assert "(+" in block and "more)" in block
    assert block.count("method Big.method_number_") < 60


def test_the_selected_symbol_and_its_class_are_kept_when_the_map_must_be_cut():
    project, ctx = context(many_methods(60), "big.py", symbol="Big.method_number_55")
    block = code_map_block(ctx)
    assert "Big.method_number_55" in block and "class Big" in block


def test_the_map_is_counted_inside_the_context_budget():
    project, ctx = context(MULTI_FILE_PYTHON, "shop/cart.py")
    assert ctx.budget.estimated_tokens <= ctx.budget.context_limit
    assert BUDGET.count(ctx.text).tokens <= BUDGET.input_limit - 1100


def test_a_tiny_budget_drops_the_map_instead_of_overflowing():
    tiny = TokenBudget(4096, 1024, 256, max_input_tokens=1300)
    _, ctx = context(MULTI_FILE_PYTHON, "shop/cart.py", reserve=1100, budget=tiny)
    assert ctx.budget.estimated_tokens <= ctx.budget.context_limit


def test_the_number_of_imports_listed_is_bounded():
    files = {f"m{i}.py": f"def f{i}():\n    return {i}\n" for i in range(8)}
    files["main.py"] = "\n".join(f"from m{i} import f{i}" for i in range(8)) + "\n\n\ndef run():\n    return 1\n"
    _, ctx = context(files, "main.py")
    imported = [line for line in code_map_block(ctx).split("\n") if line.startswith("  imported:")]
    assert len(imported[0].split("; ")) <= CODE_MAP_MAX_IMPORTS


def test_the_code_map_never_changes_which_lines_count_as_shown_to_the_model():
    project, with_map = context(MULTI_FILE_PYTHON, "shop/cart.py")
    assert with_map.shown_ranges["shop/cart.py"] == ((1, 13),)  # evidence validation still sees the real file ranges
