"""Deterministic bug-pattern rules: positive tests, negative tests, and the precision gate.

Every rule must (a) fire on the mistake it exists for, (b) stay silent on the guarded / idiomatic
variants, and (c) fire ZERO times on the clean fixtures and on this backend's own code. Several negatives
below are real false alarms that an early draft of a rule produced over the Python standard library or
node_modules; they are kept as regression tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ProjectSettings
from app.services.bug_patterns import RULES, PatternHit, run_pattern_checks
from app.services.project_pipeline import analyze_project
from analysis_fixtures import BY_NAME, FIXTURES
from heldout_fixtures import HELDOUT_FIXTURES
from project_fixtures import make_zip


def checks(files, path=None, ranges=None):
    if isinstance(files, str):
        files = {"m.py": files}
    path = path or next(iter(files))
    project = analyze_project(make_zip(files), ProjectSettings())
    return run_pattern_checks(project, path, ranges)


def rules(source, path="m.py"):
    return [h.rule for h in checks({path: source}, path)]


# --------------------------------------------------------------------------- #
# Python: PY_INDEX_PAST_END
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "header",
    ["range(len(a) + 1)", "range(len(a) + 2)", "range(1 + len(a))", "range(0, len(a) + 1)", "range(1, len(a) + 1)"],
)
def test_a_loop_that_indexes_one_past_the_end_is_found(header):
    src = f"def total(a):\n    t = 0\n    for i in {header}:\n        t += a[i]\n    return t\n"
    [hit] = checks(src)
    assert hit.rule == "PY_INDEX_PAST_END" and hit.focus_lines == (3, 4)
    assert hit.strength == "problem_if_assumptions_hold" and hit.parser == "confirmed"


def test_a_dotted_sequence_is_followed_too():
    src = "class C:\n    def f(self):\n        for i in range(len(self.items) + 1):\n            print(self.items[i])\n"
    assert rules(src) == ["PY_INDEX_PAST_END"]


@pytest.mark.parametrize(
    "src",
    [
        "def f(a):\n    for i in range(len(a)):\n        print(a[i])\n",  # correct bound
        "def f(a):\n    for i in range(len(a) + 1):\n        print(i)\n",  # never indexes
        "def f(a):\n    for i in range(len(a) + 1):\n        print(a[i - 1])\n",  # 1-based walk: no a[i]
        "def f(a):\n    for i in range(len(a) + 1):\n        if i < len(a):\n            print(a[i])\n",  # guarded
        "def f(a):\n    for i in range(len(a) + 1):\n        if i == len(a):\n            break\n        print(a[i])\n",  # guarded
        "def f(a):\n    try:\n        for i in range(len(a) + 1):\n            print(a[i])\n    except IndexError:\n        pass\n",
        "def f(a):\n    try:\n        for i in range(len(a) + 1):\n            print(a[i])\n    except Exception:\n        pass\n",
        "def f(a):\n    for i in range(len(a) + 1):\n        a.append(i)\n        print(a[i])\n",  # length changes
        "def f(a, b):\n    for i in range(len(a) + 1):\n        print(b[i])\n",  # a different sequence
        "def f(a):\n    for i in range(len(a) + 1):\n        print(a[i])\n    return a\n\nlen = lambda x: 0\n",  # `len` is shadowed
        "def f(a):\n    '''for i in range(len(a) + 1): a[i]'''\n    s = 'for i in range(len(a) + 1): a[i]'\n    # for i in range(len(a)+1): a[i]\n",
        "def f(a):\n    for i in range(len(a) - 1):\n        print(a[i], a[i + 1])\n",  # the safe pairwise walk
    ],
)
def test_guarded_correct_or_non_matching_loops_stay_silent(src):
    assert "PY_INDEX_PAST_END" not in rules(src)


# --------------------------------------------------------------------------- #
# Python: PY_SKIPS_FIRST_ITEM (worth checking, not a definite problem)
# --------------------------------------------------------------------------- #
def test_a_loop_from_one_that_never_touches_the_first_item_is_worth_checking():
    [hit] = checks(BY_NAME["off_by_one_py"].files)
    assert hit.rule == "PY_SKIPS_FIRST_ITEM" and hit.focus_lines == (7, 8)
    assert hit.strength == "worth_checking"  # depends on intent, so never "a definite problem"
    assert any("every item" in a for a in hit.assumptions)


@pytest.mark.parametrize(
    "src",
    [
        "def best(x):\n    top = x[0]\n    for i in range(1, len(x)):\n        if x[i] > top:\n            top = x[i]\n    return top\n",
        "def best(x):\n    top = x[0:1]\n    for i in range(1, len(x)):\n        top += x[i]\n    return top\n",
        "def diffs(x):\n    out = []\n    for i in range(1, len(x)):\n        out.append(x[i] - x[i - 1])\n    return out\n",
        "def f(x):\n    first = x.pop(0)\n    for i in range(1, len(x)):\n        print(x[i])\n",
        "def kmp(prefix):\n    table = [0] * len(prefix)\n    for i in range(1, len(prefix)):\n        idx = table[i - 1]\n        if prefix[i] == prefix[idx]:\n            table[i] = idx + 1\n    return table\n",  # real stdlib idiom
        "def f(x, y):\n    out = [0] * len(x)\n    for i in range(1, len(x)):\n        out[i] = x[i]\n    return out\n",  # fills a derived table
        "def f(x):\n    for i in range(len(x)):\n        print(x[i])\n",
        "def f(x):\n    for i in range(2, len(x)):\n        print(x[i])\n",
    ],
)
def test_the_deliberate_start_at_one_idioms_stay_silent(src):
    assert "PY_SKIPS_FIRST_ITEM" not in rules(src)


# --------------------------------------------------------------------------- #
# Python: PY_IS_LITERAL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "expr", ['name is "bob"', "count is 0", "count is not 5", "x is -1", "'a' is name", "x is 1.5", "x is b'q'", "a == b is 3"]
)
def test_is_against_a_text_or_number_literal_is_found(expr):
    assert rules(f"def f(name, count, x, a, b):\n    return {expr}\n") == ["PY_IS_LITERAL"]


@pytest.mark.parametrize(
    "expr", ["x is None", "x is not None", "x is True", "x is False", "x is y", "x is ()", "x is ...", "x == 'a'", "x in ('a', 'b')"]
)
def test_is_against_singletons_names_or_equality_stays_silent(expr):
    assert rules(f"def f(x, y):\n    return {expr}\n") == []


def test_code_inside_strings_and_comments_never_matches():
    assert rules('def f(x):\n    # if x is "a": pass\n    return "x is 5"\n') == []


# --------------------------------------------------------------------------- #
# Python: PY_MUTABLE_DEFAULT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "src",
    [
        "def add(tag, tags=[]):\n    tags.append(tag)\n    return tags\n",
        "def add(k, v, seen={}):\n    seen.update({k: v})\n    return seen\n",
        "def add(x, s=set()):\n    s.add(x)\n    return s\n",
        "def add(x, items=list()):\n    items.extend([x])\n    return items\n",
        "def add(x, items=[]):\n    items += [x]\n    return items\n",
        "def add(x, *, items=[]):\n    items.insert(0, x)\n    return items\n",
        "async def add(x, items=[]):\n    items.append(x)\n",
    ],
)
def test_a_default_container_that_the_function_changes_is_found(src):
    [hit] = checks(src)
    assert hit.rule == "PY_MUTABLE_DEFAULT" and hit.strength == "problem_if_assumptions_hold"
    assert any("cache" in a for a in hit.assumptions)  # says out loud that a deliberate cache is the exception


@pytest.mark.parametrize(
    "src",
    [
        "def add(x, items=None):\n    items = items or []\n    items.append(x)\n    return items\n",
        "def add(x, items=[]):\n    return items + [x]\n",
        "def add(x, items=[]):\n    items = list(items)\n    items.append(x)\n    return items\n",
        "def add(x, items=()):\n    return items + (x,)\n",
        # Real standard-library idioms an early draft flagged: caches, "seen" sets, counters kept on purpose.
        "def synopsis(filename, cache={}):\n    if filename in cache:\n        return cache[filename]\n    cache[filename] = len(filename)\n    return cache[filename]\n",
        "def seen(p, m={}):\n    if p in m:\n        return True\n    m[p] = True\n",
        "def line(side, num_lines=[0, 0]):\n    num_lines[side] += 1\n    return num_lines[side]\n",
        "def memo(n, cache={}):\n    cache[n] = n * 2\n    return cache[n]\n",
        "def uniq(x, seen=[]):\n    if x not in seen:\n        seen.append(x)\n    return seen\n",  # used as state
    ],
)
def test_rebinding_pure_use_and_deliberate_state_idioms_stay_silent(src):
    assert "PY_MUTABLE_DEFAULT" not in rules(src)


def test_a_nested_functions_mutation_is_attributed_to_the_nested_function_only():
    src = "def f(x, items=[]):\n    def inner(items=[]):\n        items.append(1)\n    return inner\n"
    [hit] = checks(src)  # `inner` really does mutate its own default; the outer `f` does not
    assert hit.focus_lines == (2, 3)


# --------------------------------------------------------------------------- #
# Python: syntax error
# --------------------------------------------------------------------------- #
def test_a_file_python_cannot_read_is_reported_with_its_assumption():
    [hit] = checks("def f(:\n    pass\n")
    assert hit.rule == "PY_SYNTAX_ERROR" and hit.focus_lines == (1,)
    assert "Python 2" in hit.assumptions[0]


def test_valid_python_has_no_syntax_hit():
    assert rules("def f(x):\n    return x\n") == []


# --------------------------------------------------------------------------- #
# JavaScript
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "loop",
    [
        "for (let i = 0; i <= arr.length; i++) { total += arr[i]; }",
        "for (var i = 0; i <= arr.length; ++i) { total += arr[i]; }",
        "for (let i = 0; i <= arr.length; i += 1) { total += arr[i]; }",
        "for (let i = 0; i <= arr.length; i++) total += arr[i];",
        "for (i = 0; i <= arr.length; i++) { total += arr[i]; }",
    ],
)
def test_js_less_or_equal_length_loop_is_found(loop):
    [hit] = checks({"a.js": f"function f(arr) {{\n  let total = 0;\n  {loop}\n  return total;\n}}\n"}, "a.js")
    assert hit.rule == "JS_INDEX_PAST_END" and hit.parser == "heuristic" and hit.focus_lines == (3,)
    assert hit.strength == "problem_if_assumptions_hold"


def test_js_this_dot_items_chain_is_followed():
    src = "class C {\n  sum() {\n    let t = 0;\n    for (let i = 0; i <= this.items.length; i++) {\n      t += this.items[i];\n    }\n    return t;\n  }\n}\n"
    assert [h.rule for h in checks({"a.js": src}, "a.js")] == ["JS_INDEX_PAST_END"]


@pytest.mark.parametrize(
    "loop",
    [
        "for (let i = 0; i < arr.length; i++) { total += arr[i]; }",
        "for (let i = 0; i <= arr.length - 1; i++) { total += arr[i]; }",
        "for (let i = 1; i <= arr.length; i++) { total += arr[i - 1]; }",  # 1-based walk
        "for (let i = 0; i <= arr.length; i++) { if (arr[i] === undefined) break; total += arr[i]; }",
        "for (let i = 0; i <= arr.length; i++) { if (i < arr.length) total += arr[i]; }",
        "for (let i = 0; i <= arr.length; i++) { total += other[i]; }",
        "for (const x of arr) { total += x; }",
        "for (let i = 0; i <= arr.length; i += 2) { total += arr[i]; }",
        'for (let i = 0; i <= arr.length; i++) { total += "arr[i]"; }',
        "// for (let i = 0; i <= arr.length; i++) { total += arr[i]; }",
    ],
)
def test_js_correct_guarded_or_commented_loops_stay_silent(loop):
    assert checks({"a.js": f"function f(arr, other) {{\n  let total = 0;\n  {loop}\n  return total;\n}}\n"}, "a.js") == []


@pytest.mark.parametrize("cond", ["x = 5", "done = true", 'name = "bob"', "mode = null", "x = -1", "this.ready = false"])
def test_js_a_fixed_value_assigned_in_an_if_is_found(cond):
    [hit] = checks({"a.js": f"function f(x, done, name, mode) {{\n  if ({cond}) {{\n    go();\n  }}\n}}\n"}, "a.js")
    assert hit.rule == "JS_ASSIGN_IN_CONDITION" and hit.focus_lines == (2,)
    assert "mistake" in hit.assumptions[0]


@pytest.mark.parametrize(
    "cond",
    [
        "match = re.exec(s)",  # deliberate idiom: 19 of 19 real hits in node_modules were this
        "(match = re.exec(s))",
        "(x = 5)",
        "x == 5", "x === 5", "x != 5", "x !== 5", "x <= 5", "x >= 5", "x += 5", "x -= 5", "a && b", "x = y",
        "arr.some((v = 1) => v)",
    ],
)
def test_js_idiomatic_or_ordinary_conditions_stay_silent(cond):
    assert checks({"a.js": f"function f(x, y, s, re, arr, match) {{\n  if ({cond}) {{\n    go();\n  }}\n}}\n"}, "a.js") == []


@pytest.mark.parametrize("expr", ["x === NaN", "x == NaN", "x !== NaN", "x != NaN", "NaN === x", "NaN !== x", "Number.NaN === x"])
def test_js_comparing_with_nan_is_found(expr):
    [hit] = checks({"a.js": f"function f(x) {{\n  return {expr};\n}}\n"}, "a.js")
    assert hit.rule == "JS_NAN_COMPARE" and hit.focus_lines == (2,)


@pytest.mark.parametrize("expr", ["Number.isNaN(x)", "isNaN(x)", "x = NaN", "x <= NaN", "x >= NaN", 'x === "NaN"', "obj.NaN === x"])
def test_js_proper_nan_tests_assignments_and_strings_stay_silent(expr):
    assert checks({"a.js": f"function f(x, obj) {{\n  return {expr};\n}}\n"}, "a.js") == []


def test_js_with_unbalanced_brackets_or_jsx_is_skipped_rather_than_guessed():
    broken = "function f(arr) {\n  for (let i = 0; i <= arr.length; i++) { t += arr[i]; }\n"  # never closed
    assert checks({"a.js": broken}, "a.js") == []
    jsx = "const A = () => { for (let i = 0; i <= arr.length; i++) { t += arr[i]; } return <b>x</b>; };\n"
    assert checks({"a.jsx": jsx}, "a.jsx") == []


# --------------------------------------------------------------------------- #
# Missing local files (from the dependency graph)
# --------------------------------------------------------------------------- #
def test_a_script_that_is_not_in_the_upload_is_worth_checking_with_its_assumptions():
    files = {"index.html": '<html><body><script src="app.js"></script><script src="missing.js"></script></body></html>', "app.js": "var a = 1;\n"}
    [hit] = checks(files, "index.html")
    assert hit.rule == "MISSING_LOCAL_FILE" and hit.strength == "worth_checking"
    assert "missing.js" in hit.technical and any("whole project" in a for a in hit.assumptions)


def test_present_files_and_external_urls_are_not_reported():
    files = {"index.html": '<script src="app.js"></script><script src="https://cdn.example.com/x.js"></script>', "app.js": "var a = 1;\n"}
    assert checks(files, "index.html") == []


# --------------------------------------------------------------------------- #
# Scope: the selected symbol's lines only
# --------------------------------------------------------------------------- #
def test_ranges_restrict_hits_to_the_selected_lines():
    src = "def a(x):\n    return x is 'q'\n\n\ndef b(x):\n    return x is 'r'\n"
    assert [h.focus_lines for h in checks(src)] == [(2,), (6,)]
    assert [h.focus_lines for h in checks(src, ranges=[(5, 6)])] == [(6,)]
    assert checks(src, ranges=[(3, 4)]) == []


def test_an_unknown_file_yields_nothing():
    project = analyze_project(make_zip({"m.py": "x = 1\n"}), ProjectSettings())
    assert run_pattern_checks(project, "nope.py") == []


# --------------------------------------------------------------------------- #
# Every hit is honest about itself
# --------------------------------------------------------------------------- #
def all_hits() -> list[PatternHit]:
    out = []
    for f in FIXTURES:
        project = analyze_project(f.zip_bytes, ProjectSettings())
        for path in project.by_path:
            out += run_pattern_checks(project, path)
    return out


def test_every_rule_in_the_catalogue_states_its_assumptions_and_strength():
    for info in RULES.values():
        assert info.assumptions, info.rule
        assert info.strength in ("problem_if_assumptions_hold", "worth_checking")


def test_no_hit_is_ever_presented_as_unconditional():
    hits = all_hits()
    assert hits
    for hit in hits:
        assert hit.strength in ("problem_if_assumptions_hold", "worth_checking")
        assert hit.assumptions and hit.title and hit.beginner and hit.technical
        assert hit.explanation("beginner") == hit.beginner and hit.explanation("advanced") == hit.technical
        text = (hit.beginner + hit.technical).lower()
        assert "definitely" not in text and "certainly" not in text and "guaranteed" not in text


def test_js_hits_are_labelled_heuristic_and_python_hits_confirmed():
    for hit in all_hits():
        assert hit.parser == ("heuristic" if hit.language == "javascript" else "confirmed")


# --------------------------------------------------------------------------- #
# Positive coverage on the fixtures, and the documented blind spots
# --------------------------------------------------------------------------- #
def fixture_hits(name):
    f = BY_NAME[name]
    project = analyze_project(f.zip_bytes, ProjectSettings())
    return [(h.rule, h.path, h.focus_lines) for p in project.by_path for h in run_pattern_checks(project, p)]


def test_pattern_bugs_in_the_fixtures_are_found_exactly_where_they_are():
    assert fixture_hits("off_by_one_py") == [("PY_SKIPS_FIRST_ITEM", "cart.py", (7, 8))]
    assert fixture_hits("injection_project") == [("PY_INDEX_PAST_END", "report.py", (17, 18))]
    [(rule, path, lines)] = fixture_hits("hidden_bug")
    assert (rule, path) == ("PY_INDEX_PAST_END", "pipeline.py") and lines[0] == BY_NAME["hidden_bug"].bug[1]


@pytest.mark.parametrize("name", ["js_logic_error", "discount_py", "adult_js", "cross_file_call", "html_id_mismatch", "ambiguous_slice"])
def test_intent_dependent_bugs_are_deliberately_not_detected(name):
    # A reversed comparison, a wrong formula, `> 18` vs "18 or older", `rows[1:]`: they depend on intent, which no
    # structural rule can know. A rule for them would only overfit the fixtures, so the AI (and the user) own these.
    assert fixture_hits(name) == []


# --------------------------------------------------------------------------- #
# The precision gate
# --------------------------------------------------------------------------- #
CLEAN = [f for f in list(FIXTURES) + list(HELDOUT_FIXTURES) if not f.has_bug]


@pytest.mark.parametrize("fixture", CLEAN, ids=lambda f: f.name)
def test_no_rule_fires_on_a_clean_fixture(fixture):
    project = analyze_project(fixture.zip_bytes, ProjectSettings())
    assert [h for p in project.by_path for h in run_pattern_checks(project, p)] == []


def test_no_rule_fires_on_this_backends_own_code():
    root = Path(__file__).resolve().parents[1]
    files = {
        str(p.relative_to(root)).replace("\\", "/"): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts
    }
    project = analyze_project(make_zip(files), ProjectSettings())
    assert len(project.files) > 50
    found = [(h.rule, h.path, h.focus_lines) for path in project.by_path for h in run_pattern_checks(project, path)]
    assert found == []


def test_running_every_rule_on_a_large_file_is_fast_and_bounded():
    import time

    body = "\n".join(f"def f{i}(a):\n    for j in range(len(a)):\n        a[j] += {i}\n" for i in range(2000))
    project = analyze_project(make_zip({"big.py": body}), ProjectSettings())
    started = time.perf_counter()
    assert run_pattern_checks(project, "big.py") == []
    assert time.perf_counter() - started < 2.0


# --------------------------------------------------------------------------- #
# Robustness: rules are advisory and must never take an analysis down or run away
# --------------------------------------------------------------------------- #
def test_a_rule_that_crashes_is_skipped_and_the_others_still_run(monkeypatch, caplog):
    from app.services import bug_patterns

    def boom(*args, **kwargs):
        raise RuntimeError("SECRET_SOURCE_TEXT")

    monkeypatch.setattr(bug_patterns, "_py_is_literal", boom)
    src = "def f(x):\n    return x is 'q'\n\n\ndef g(a):\n    for i in range(len(a) + 1):\n        print(a[i])\n"
    with caplog.at_level("WARNING"):
        found = [h.rule for h in checks(src)]
    assert found == ["PY_INDEX_PAST_END"]  # PY_IS_LITERAL was skipped, the loop rule still ran
    assert "PY_IS_LITERAL failed (RuntimeError)" in caplog.text and "SECRET_SOURCE_TEXT" not in caplog.text


def test_a_file_with_thousands_of_look_alike_loops_is_bounded():
    import time

    loops = "".join(
        f"for (let i{n} = 0; i{n} <= arr{n}.length; i{n}++) {{ if (i{n} < arr{n}.length) {{ t += arr{n}[i{n}] + {n * 7919 % 1013}; }} }}\n"
        for n in range(4000)
    )
    started = time.perf_counter()
    assert checks({"a.js": "function f(arr) { let t = 0;\n" + loops + "}\n"}, "a.js") == []
    assert time.perf_counter() - started < 3.0

    py = "def f(a):\n" + "".join(
        f"    for i{n} in range(len(a) + 1):\n        if i{n} < len(a):\n            print(a[i{n}])\n" for n in range(2000)
    )
    started = time.perf_counter()
    assert [h for h in checks(py) if h.rule == "PY_INDEX_PAST_END"] == []
    assert time.perf_counter() - started < 3.0


def test_at_most_a_handful_of_hits_per_rule_are_returned_even_for_a_file_full_of_the_mistake():
    py = "def f(a):\n" + "".join(f"    for i{n} in range(len(a) + 1):\n        print(a[i{n}])\n" for n in range(40))
    assert len([h for h in checks(py) if h.rule == "PY_INDEX_PAST_END"]) <= 10


def test_javascript_analysis_and_rules_reuse_the_same_scan(monkeypatch):
    from app.services import js_analysis
    original = js_analysis.tokenize
    calls = []
    def counted(text):
        calls.append(text)
        return original(text)
    monkeypatch.setattr(js_analysis, 'tokenize', counted)
    project = analyze_project(make_zip({'a.js': 'function f(a) { return a.length; }'}), ProjectSettings())
    assert run_pattern_checks(project, 'a.js') == []
    assert run_pattern_checks(project, 'a.js') == []
    assert len(calls) == 1
