"""Static analysis: Python (ast), JavaScript (scanner), HTML (html.parser), CSS (scanner)."""

from __future__ import annotations

import pytest

from app.services.project_models import ProjectFile
from app.services.static_analysis import analyze_file


def analyze(path: str, language: str, text: str):
    count = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    return analyze_file(ProjectFile(path, language, text, len(text.encode()), count))  # type: ignore[arg-type]


def py(text: str):
    return analyze("m.py", "python", text)


def js(text: str, path: str = "m.js"):
    return analyze(path, "javascript", text)


def by_name(analysis, name: str):
    return next(s for s in analysis.symbols if s.qualified_name == name)


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #
PY_SOURCE = '''"""Module doc.

More.
"""
import os, sys as system
from . import sibling
from ..pkg.mod import (alpha, beta)
from star import *


@decorator
def top(a, b: int = 2, *args, key=None, **kw) -> int:
    """Top doc."""
    return a


async def worker(x):
    return x


class Base(Parent, metaclass=Meta):
    """Base doc."""

    LIMIT = 3

    def method(self, value):
        def inner():
            pass
        return value

    async def amethod(self):
        pass

    class Nested:
        def deep(self):
            pass


CONSTANT = 10
typed: int = 5

if __name__ == "__main__":
    top(1, 2)
'''


def test_python_symbols_with_lines_signatures_and_hierarchy():
    a = py(PY_SOURCE)
    assert a.confidence == "confirmed" and a.parser == "python-ast"
    names = {s.qualified_name: s for s in a.symbols}
    assert set(names) == {
        "top", "worker", "Base", "Base.method", "Base.amethod", "Base.Nested", "Base.Nested.deep",
        "CONSTANT", "typed",
    }  # nested functions and class attributes are deliberately not top-level symbols
    assert names["top"].kind == "function" and names["worker"].kind == "async_function"
    assert names["Base"].kind == "class" and names["Base.method"].kind == "method"
    assert names["Base.amethod"].kind == "async_method"
    assert names["Base.method"].parent == "Base"
    # decorator line is part of the symbol's range
    assert (names["top"].start_line, names["top"].end_line) == (11, 14)
    assert (names["worker"].start_line, names["worker"].end_line) == (17, 18)
    assert (names["Base"].start_line, names["Base"].end_line) == (21, 36)
    assert (names["Base.method"].start_line, names["Base.method"].end_line) == (26, 29)
    assert names["top"].signature.startswith("def top(a, b: int")
    assert names["top"].signature.endswith("-> int")
    assert names["worker"].signature == "async def worker(x)"
    assert names["Base"].signature == "class Base(Parent, metaclass=Meta)"
    assert names["top"].doc == "Top doc." and names["Base"].doc == "Base doc."
    assert a.doc == "Module doc."
    assert all(s.confidence == "confirmed" for s in a.symbols)


def test_python_imports_with_levels_names_and_lines():
    refs = {(r.specifier, r.level, r.names, r.line) for r in py(PY_SOURCE).references}
    assert ("os", 0, (), 5) in refs
    assert ("sys", 0, (), 5) in refs
    assert ("", 1, ("sibling",), 6) in refs
    assert ("pkg.mod", 2, ("alpha", "beta"), 7) in refs
    assert ("star", 0, ("*",), 8) in refs


def test_python_imports_inside_functions_are_found_but_marked_not_top_level():
    a = py("import a\n\ndef f():\n    import b\n    from c import d\n")
    flags = {r.specifier: r.top_level for r in a.references}
    assert flags == {"a": True, "b": False, "c": False}


def test_python_main_guard_detection():
    assert py(PY_SOURCE).has_main_guard is True
    assert py('if "__main__" == __name__:\n    pass\n').has_main_guard is True
    assert py("def main():\n    pass\n").has_main_guard is False
    assert py('if __name__ == "other":\n    pass\n').has_main_guard is False


def test_python_definitions_inside_if_and_try_blocks_are_top_level():
    a = py("try:\n    import fast\n    def impl():\n        pass\nexcept ImportError:\n    def impl():\n        pass\n")
    assert [s.start_line for s in a.symbols if s.name == "impl"] == [3, 6]


def test_python_syntax_error_is_reported_with_a_line_and_does_not_raise():
    a = py("def ok():\n    pass\n\ndef broken(:\n    pass\n")
    assert a.has_syntax_error is True
    (d,) = a.diagnostics
    assert (d.severity, d.code, d.line) == ("error", "syntax_error", 4)
    assert "invalid syntax" in d.message.lower() or "SyntaxError" in d.message
    assert a.symbols == ()  # nothing is guessed from a file that does not parse


def test_python_indentation_error():
    a = py("def f():\nreturn 1\n")
    assert a.has_syntax_error and a.diagnostics[0].code == "syntax_error"


def test_python_star_import_and_dynamic_import_are_flagged_as_notes():
    a = py("from x import *\nimport importlib\nm = importlib.import_module(name)\nn = __import__('y')\n")
    codes = [(d.code, d.confidence) for d in a.diagnostics]
    assert ("star_import", "confirmed") in codes
    assert codes.count(("dynamic_import", "heuristic")) == 2


def test_python_deep_nesting_does_not_crash():
    source = "x = " + "(" * 400 + "1" + ")" * 400 + "\n"
    a = py(source)
    assert a.path == "m.py"  # either parsed or reported, never raised


def test_python_deeply_chained_elif_does_not_recurse_forever():
    chain = "if a == 0:\n    pass\n" + "".join(f"elif a == {i}:\n    pass\n" for i in range(1, 300))
    a = py(chain)
    assert a.has_syntax_error is False


def test_python_invalid_escape_sequences_do_not_become_failures():
    assert py('x = "\\d+"\n').has_syntax_error is False


# --------------------------------------------------------------------------- #
# JavaScript
# --------------------------------------------------------------------------- #
JS_SOURCE = '''import React, { useState as useS, useEffect } from 'react';
import * as utils from "./utils.js";
import './side-effect.css';
export { alpha, beta as gamma } from './reexport';
export * from './all';
const fs = require('fs');
const { a, b } = require("./pair");
let lazy = () => import('./lazy.js');

export default class Widget extends React.Component {
  static count = 0;
  constructor(props) { super(props); }
  async load() { return await fetch(`/x/${ `${this.id}` }`); }
  get label() { return 1; }
}

export function exported(a, b) {
  return a / b; // a division, not a regex
}

async function internal() {}
const arrow = async (x) => {
  return x;
};
const short = y => y * 2
var plain = 5;
module.exports = { exported, internal };
exports.extra = 1;
'''


def test_js_confirms_nothing_everything_is_heuristic():
    a = js(JS_SOURCE)
    assert a.confidence == "heuristic" and a.parser == "js-scanner"
    assert all(s.confidence == "heuristic" for s in a.symbols)
    assert all(r.confidence == "heuristic" for r in a.references)
    assert all(e.confidence == "heuristic" for e in a.exports)


def test_js_esm_and_commonjs_references_with_lines():
    refs = {(r.kind, r.specifier, r.line) for r in js(JS_SOURCE).references}
    assert ("import", "react", 1) in refs
    assert ("import", "./utils.js", 2) in refs
    assert ("import", "./side-effect.css", 3) in refs
    assert ("reexport", "./reexport", 4) in refs
    assert ("reexport", "./all", 5) in refs
    assert ("require", "fs", 6) in refs
    assert ("require", "./pair", 7) in refs
    assert ("dynamic_import", "./lazy.js", 8) in refs


def test_js_import_names():
    refs = {r.specifier: r.names for r in js(JS_SOURCE).references if r.kind == "import"}
    assert refs["react"] == ("default", "useState", "useEffect")
    assert refs["./utils.js"] == ("*",)
    assert refs["./side-effect.css"] == ()


def test_js_exports():
    exports = {(e.kind, e.name) for e in js(JS_SOURCE).exports}
    assert ("default", "Widget") in exports
    assert ("named", "exported") in exports
    assert ("reexport", "alpha") in exports and ("reexport", "gamma") in exports
    assert ("star", "*") in exports
    assert ("commonjs", "internal") in exports
    assert ("commonjs", "extra") in exports


def test_js_symbols_with_line_ranges():
    a = js(JS_SOURCE)
    names = {s.qualified_name: s for s in a.symbols}
    assert (names["Widget"].kind, names["Widget"].start_line, names["Widget"].end_line) == ("class", 10, 15)
    assert names["Widget.load"].kind == "async_method" and names["Widget.load"].start_line == 13
    assert names["Widget.constructor"].kind == "method"
    assert (names["exported"].start_line, names["exported"].end_line) == (17, 19) or names["exported"].start_line == 17
    assert names["internal"].kind == "async_function"
    assert names["arrow"].kind == "async_function" and (names["arrow"].start_line, names["arrow"].end_line) == (22, 24)
    assert names["short"].kind == "function" and names["short"].start_line == names["short"].end_line == 25
    assert names["plain"].kind == "variable"
    assert names["exported"].exported is True


def test_js_things_inside_strings_comments_and_templates_are_ignored():
    a = js(
        '// import fake from "./nope.js"\n'
        "/* require('nope2') */\n"
        'const s = "import x from \'./nope3.js\'";\n'
        "const t = `require('./nope4')`;\n"
        "const r = /import\\s+from/;\n"
        "import real from './real.js';\n"
    )
    assert [r.specifier for r in a.references] == ["./real.js"]
    assert not any("unterminated" in d.code for d in a.diagnostics)


def test_js_regex_versus_division_does_not_derail_the_scan():
    a = js("const a = x / 2; const b = /[/]\\//g; function after() {}\nconst c = (a) / b / 2;\n")
    assert "after" in {s.name for s in a.symbols}


def test_js_dynamic_require_is_a_note_not_a_reference():
    a = js("const name = 'x';\nconst m = require(name);\nconst n = import(path);\n")
    assert a.references == ()
    assert [d.code for d in a.diagnostics] == ["dynamic_reference", "dynamic_reference"]
    assert all(d.confidence == "heuristic" for d in a.diagnostics)


def test_js_member_calls_named_require_are_not_references():
    a = js("loader.require('x');\nobj.import('y');\n")
    assert a.references == ()


def test_js_nested_declarations_are_not_top_level_symbols():
    a = js("function outer() {\n  function inner() {}\n  const hidden = 1;\n}\n")
    assert [s.name for s in a.symbols] == ["outer"]


def test_js_unbalanced_brackets_and_unterminated_strings_are_warnings_not_errors():
    a = js("function f( {\n  const s = 'oops;\n")
    codes = {d.code for d in a.diagnostics}
    assert {"unbalanced_brackets", "unterminated_string"} <= codes
    assert all(d.severity == "warning" for d in a.diagnostics)
    assert a.has_syntax_error is False  # a scanner cannot rule on syntax


def test_js_jsx_is_flagged_as_heuristic_and_apostrophes_do_not_warn():
    a = js("export default function App() {\n  return <p>Don't panic</p>;\n}\n", path="App.jsx")
    assert [d.code for d in a.diagnostics] == ["jsx_heuristic"]
    assert "App" in {s.name for s in a.symbols}


def test_js_destructured_declarations_bind_each_name():
    a = js("const { one, two: renamed } = obj;\nconst [first, second] = list;\n")
    assert {s.name for s in a.symbols} == {"one", "renamed", "first", "second"}


def test_js_multiple_declarators():
    a = js("let n = 5, f = function() { return 1; }, g = (x) => x;\n")
    kinds = {s.name: s.kind for s in a.symbols}
    assert kinds == {"n": "variable", "f": "function", "g": "function"}


def test_js_deeply_nested_templates_terminate():
    a = js("const t = " + "`${" * 40 + "1" + "}`" * 40 + ";\nfunction after() {}\n")
    assert a.path == "m.js"


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
HTML_SOURCE = '''<!DOCTYPE html>
<html>
<head>
  <title>  My   page
  </title>
  <link rel="stylesheet" href="css/site.css?v=2">
  <link rel="icon" href="favicon.ico">
  <script src="https://cdn.example.com/lib.js"></script>
  <script type="module" src="js/main.js"></script>
  <style>body { color: red }</style>
  <script type="application/json">{"a": 1}</script>
</head>
<body id="top">
  <div id="app" class="x"></div>
  <img src="images/logo.png" alt="">
  <script>
    document.write("</div>");
    import fake from './not-a-reference.js';
  </script>
</body>
</html>
'''


def test_html_title_resources_and_lines():
    a = analyze("index.html", "html", HTML_SOURCE)
    assert a.confidence == "confirmed" and a.parser == "html.parser"
    assert a.title == "My page"
    refs = {(r.kind, r.specifier, r.line) for r in a.references}
    assert refs == {
        ("stylesheet", "css/site.css?v=2", 6),
        ("asset", "favicon.ico", 7),
        ("script", "https://cdn.example.com/lib.js", 8),
        ("script", "js/main.js", 9),
        ("asset", "images/logo.png", 15),
    }


def test_html_inline_script_style_counts_and_ids():
    a = analyze("index.html", "html", HTML_SOURCE)
    assert a.inline_script_count == 1  # the JSON data block is not a script
    assert a.inline_style_count == 1
    assert [(e.id, e.tag, e.line) for e in a.element_ids] == [("top", "body", 13), ("app", "div", 14)]


def test_html_script_bodies_are_never_interpreted():
    a = analyze("index.html", "html", HTML_SOURCE)
    assert "./not-a-reference.js" not in {r.specifier for r in a.references}


def test_html_malformed_markup_does_not_fail():
    a = analyze("bad.html", "html", "<div><p>unclosed <b>bold <script src=x.js></script>\n<<<>>> </div></p><title>t</title>")
    assert a.title == "t"
    assert [r.specifier for r in a.references] == ["x.js"]


def test_html_base_href_is_noted():
    a = analyze("a.html", "html", '<base href="/app/"><script src="x.js"></script>')
    assert [d.code for d in a.diagnostics] == ["base_href"]


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #
CSS_SOURCE = '''@charset "utf-8";
@import "reset.css";
@import url('theme/dark.css') screen;
/* .commented { color: red } */
.btn, .btn-primary:hover { background: url("img/bg.png"); }
#main > .item.active { margin: 0 }
@media (max-width: 600px) {
  .mobile { display: none }
}
@font-face { font-family: X; src: url(fonts/x.woff2) format("woff2"); }
a[href="x{y}"] { color: blue }
'''


def test_css_imports_urls_and_selectors():
    a = analyze("site.css", "css", CSS_SOURCE)
    assert a.confidence == "heuristic" and a.parser == "css-scanner"
    refs = [(r.kind, r.specifier, r.line) for r in a.references]
    assert refs == [
        ("css_import", "reset.css", 2),
        ("css_import", "theme/dark.css", 3),
        ("asset", "img/bg.png", 5),
        ("asset", "fonts/x.woff2", 10),
    ]
    assert a.css_classes == ("active", "btn", "btn-primary", "item", "mobile")
    assert a.css_ids == ("main",)
    rules = {s.name: (s.start_line, s.end_line) for s in a.symbols}
    assert rules[".btn, .btn-primary:hover"] == (5, 5)
    assert rules["#main > .item.active"] == (6, 6)
    assert rules[".mobile"] == (8, 8)  # nested in @media
    assert ".commented" not in rules
    assert all(s.kind == "rule" and s.confidence == "heuristic" for s in a.symbols)


def test_css_braces_in_strings_do_not_confuse_rule_ranges():
    a = analyze("s.css", "css", 'a[title="}"] { color: red }\nb { margin: 0 }\n')
    assert {s.name: (s.start_line, s.end_line) for s in a.symbols} == {'a[title="}"]': (1, 1), "b": (2, 2)}


def test_css_unbalanced_blocks_are_warnings():
    assert [d.code for d in analyze("s.css", "css", ".a { color: red\n").diagnostics] == ["unclosed_block"]
    assert [d.code for d in analyze("s.css", "css", ".a { } }\n").diagnostics] == ["unbalanced_braces"]


def test_css_multiline_rule_range():
    a = analyze("s.css", "css", ".a {\n  color: red;\n  margin: 0;\n}\n")
    assert [(s.start_line, s.end_line) for s in a.symbols] == [(1, 4)]


# --------------------------------------------------------------------------- #
# Cross-cutting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path, language, text",
    [("m.py", "python", PY_SOURCE), ("m.js", "javascript", JS_SOURCE), ("i.html", "html", HTML_SOURCE), ("s.css", "css", CSS_SOURCE)],
)
def test_analysis_is_deterministic(path, language, text):
    assert analyze(path, language, text) == analyze(path, language, text)


@pytest.mark.parametrize("language, path", [("python", "e.py"), ("javascript", "e.js"), ("html", "e.html"), ("css", "e.css")])
def test_empty_files_are_fine(language, path):
    a = analyze(path, language, "")
    assert a.symbols == () and a.references == () and a.line_count == 0


def test_an_analyser_crash_degrades_one_file(monkeypatch):
    import app.services.static_analysis as module  # noqa: PLC0415

    def boom(file):
        raise RuntimeError("SECRET source text must not appear in the result")

    monkeypatch.setitem(module._ANALYZERS, "python", boom)
    a = py("x = 1\n")
    assert a.diagnostics[0].code == "analysis_failed"
    assert "SECRET" not in a.diagnostics[0].message


# --------------------------------------------------------------------------- #
# Resource safety
# --------------------------------------------------------------------------- #
def test_per_file_output_is_capped_and_the_cap_is_reported():
    from app.services.static_analysis import MAX_SYMBOLS_PER_FILE  # noqa: PLC0415

    a = py("def f(x):\n    return x\n\n" * (MAX_SYMBOLS_PER_FILE + 500))
    assert len(a.symbols) == MAX_SYMBOLS_PER_FILE
    assert [d.code for d in a.diagnostics] == ["symbols_truncated"]


@pytest.mark.parametrize(
    "text",
    [
        "(" * 50_000,
        "{" * 50_000,
        "`${" * 20_000,
        "'" * 50_000,
        "/" * 50_000,
        "function a(){" * 10_000,
        "class A {" * 10_000,
        "const a = 1,\n" * 10_000,
        "import {" + ", ".join(f"n{i}" for i in range(20_000)) + "} from './x';\n",
    ],
    ids=["open-parens", "open-braces", "nested-templates", "quotes", "slashes", "nested-functions", "unterminated-classes", "comma-declarations", "long-import-list"],
)
def test_pathological_javascript_is_scanned_in_bounded_time(text):
    import time  # noqa: PLC0415

    started = time.perf_counter()
    js(text)
    assert time.perf_counter() - started < 10


def test_random_javascript_soup_never_raises():
    import random  # noqa: PLC0415

    rng = random.Random(5)
    pieces = list("{}()[]`'\"/\\*$=<>;,.\n ") +["import ", "export ", "function ", "class ", "require(", "const ", "=> ", "${", "/*", "//", "async ", "from "]
    for _ in range(300):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 300)))
        assert js(text).parser == "js-scanner"
