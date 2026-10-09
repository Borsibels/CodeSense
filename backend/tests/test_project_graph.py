"""Dependency graph: resolution, statuses, external packages, cycles and determinism."""

from __future__ import annotations

import random

from app.config import ProjectSettings
from app.services.project_pipeline import analyze_project
from project_fixtures import CYCLE_PROJECT, MIXED_PROJECT, PYTHON_PROJECT, WEB_PROJECT, make_zip

LIMITS = ProjectSettings()


def graph_of(files):
    return analyze_project(make_zip(files), LIMITS).graph


def edges(graph, source=None):
    return [e for e in graph.edges if source is None or e.source == source]


def edge(graph, source, specifier):
    (match,) = [e for e in graph.edges if e.source == source and e.specifier == specifier]
    return match


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #
def test_python_absolute_and_relative_imports_resolve_inside_the_project():
    g = graph_of(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from . import b\nfrom .c import thing\nfrom .. import top\nimport pkg.b as bb\n",
            "pkg/b.py": "",
            "pkg/c.py": "thing = 1\n",
            "top.py": "import pkg.a\nfrom pkg import b\n",
        }
    )
    resolved = {(e.source, e.specifier, e.target) for e in g.edges if e.status == "resolved"}
    assert ("pkg/a.py", ".", None) not in resolved
    assert ("pkg/a.py", ".b", "pkg/b.py") in resolved  # `from . import b` is the submodule pkg/b.py
    assert ("pkg/a.py", ".c", "pkg/c.py") in resolved
    assert ("pkg/a.py", "..top", "top.py") in resolved
    assert ("pkg/a.py", "pkg.b", "pkg/b.py") in resolved
    assert ("top.py", "pkg.a", "pkg/a.py") in resolved
    assert ("top.py", "pkg.b", "pkg/b.py") in resolved  # `from pkg import b`
    assert all(e.status == "resolved" for e in g.edges)


def test_from_package_import_name_that_is_not_a_module_resolves_to_the_package():
    g = graph_of({"pkg/__init__.py": "name = 1\n", "main.py": "from pkg import name\n"})
    assert edge(g, "main.py", "pkg").target == "pkg/__init__.py"


def test_python_external_vs_standard_library():
    g = graph_of({"m.py": "import os\nimport requests\nfrom collections import abc\nimport numpy.linalg\n"})
    status = {e.specifier: (e.status, e.external_kind) for e in g.edges}
    assert status == {
        "os": ("external", "stdlib"),
        "requests": ("external", "package"),
        "collections": ("external", "stdlib"),
        "numpy.linalg": ("external", "package"),
    }
    assert [(d.name, d.kind) for d in g.external_dependencies] == [
        ("collections", "stdlib"),
        ("numpy", "package"),
        ("os", "stdlib"),
        ("requests", "package"),
    ]


def test_python_missing_relative_import_is_a_confirmed_missing_file():
    g = graph_of({"pkg/__init__.py": "", "pkg/a.py": "from .nothing import x\nfrom ... import y\n"})
    a = edge(g, "pkg/a.py", ".nothing")
    assert (a.status, a.target) == ("missing", None)
    assert edge(g, "pkg/a.py", "...").status == "missing"


def test_python_missing_submodule_of_a_local_package_is_missing_not_external():
    g = graph_of({"utils/__init__.py": "", "main.py": "import utils.nope\nimport notlocal\n"})
    assert edge(g, "main.py", "utils.nope").status == "missing"
    assert edge(g, "main.py", "notlocal").status == "external"


def test_python_namespace_package_is_unresolved():
    g = graph_of({"ns/mod.py": "", "main.py": "import ns\n"})
    e = edge(g, "main.py", "ns")
    assert e.status == "unresolved" and "Namespace" in e.reason


def test_python_imports_resolve_against_enclosing_folders_like_a_zipped_parent_directory():
    """A zip of the parent folder (``backend/app/...``) still resolves ``import app.x``."""
    g = graph_of(
        {
            "backend/app/__init__.py": "",
            "backend/app/main.py": "from app.services import tools\nimport app.config\n",
            "backend/app/config.py": "",
            "backend/app/services/__init__.py": "",
            "backend/app/services/tools.py": "",
        }
    )
    targets = {e.target for e in g.edges if e.source == "backend/app/main.py"}
    # `from app.services import tools` imports the package (its __init__) and the submodule
    assert targets == {"backend/app/services/__init__.py", "backend/app/services/tools.py", "backend/app/config.py"}


def test_python_script_mode_sibling_imports():
    g = graph_of({"scripts/run.py": "import helpers\n", "scripts/helpers.py": ""})
    assert edge(g, "scripts/run.py", "helpers").target == "scripts/helpers.py"


def test_star_imports_still_link_the_module():
    g = graph_of({"a.py": "from b import *\n", "b.py": ""})
    assert edge(g, "a.py", "b").target == "b.py"


def test_import_of_an_oversized_local_module_is_reported_as_excluded():
    limits = ProjectSettings(max_file_bytes=200, max_entry_bytes=5000, max_uncompressed_bytes=50000)
    files = {"main.py": "import big\n", "big.py": "x = 1\n" * 100}
    g = analyze_project(make_zip(files), limits).graph
    e = edge(g, "main.py", "big")
    assert e.status == "excluded" and "too_large" in e.reason and e.target is None


# --------------------------------------------------------------------------- #
# JavaScript
# --------------------------------------------------------------------------- #
def test_js_extensions_and_directory_index_resolution():
    g = graph_of(
        {
            "main.js": "import a from './a';\nimport b from './b.js';\nimport c from './lib';\n"
            "import d from './esm';\nimport e from './comp';\nimport f from '../root';\n",
            "a.js": "",
            "b.js": "",
            "lib/index.js": "",
            "esm.mjs": "",
            "comp.jsx": "",
            "root.js": "",
            "sub/inner.js": "import r from '../root';\nimport s from './../main';\n",
        }
    )
    got = {e.specifier: e.target for e in g.edges if e.source == "main.js"}
    assert got == {
        "./a": "a.js",
        "./b.js": "b.js",
        "./lib": "lib/index.js",
        "./esm": "esm.mjs",
        "./comp": "comp.jsx",
        "../root": None,  # outside the project
    }
    assert edge(g, "main.js", "../root").status == "unresolved"
    assert {e.target for e in g.edges if e.source == "sub/inner.js"} == {"root.js", "main.js"}


def test_js_exact_file_wins_over_directory():
    g = graph_of({"main.js": "import x from './x';\n", "x.js": "", "x/index.js": ""})
    assert edge(g, "main.js", "./x").target == "x.js"


def test_js_external_packages_builtins_and_urls():
    g = graph_of(
        {
            "m.js": "import React from 'react';\nimport _ from 'lodash/fp';\nimport x from '@scope/pkg/sub';\n"
            "const fs = require('fs');\nconst p = require('node:path');\nimport u from 'https://cdn.example.com/x.js';\n"
        }
    )
    kinds = {e.specifier: e.external_kind for e in g.edges}
    assert kinds == {
        "react": "package",
        "lodash/fp": "package",
        "@scope/pkg/sub": "package",
        "fs": "builtin",
        "node:path": "builtin",
        "https://cdn.example.com/x.js": "url",
    }
    assert [(d.name, d.kind) for d in g.external_dependencies] == [
        ("@scope/pkg", "package"),
        ("fs", "builtin"),
        ("https://cdn.example.com/x.js", "url"),
        ("lodash", "package"),
        ("path", "builtin"),  # `node:path` is listed under its module name
        ("react", "package"),
    ]


def test_js_missing_relative_import_vs_unresolvable_alias():
    g = graph_of({"src/m.js": "import a from './gone';\nimport b from '@/components/b';\nimport c from 'src/utils';\n", "src/utils.js": ""})
    assert edge(g, "src/m.js", "./gone").status == "missing"
    assert edge(g, "src/m.js", "@/components/b").status == "unresolved"
    # bare specifier that names a local folder: could be a base-URL alias, so not claimed external
    assert edge(g, "src/m.js", "src/utils").status == "unresolved"


def test_js_import_of_a_typescript_file_is_excluded_not_missing():
    g = graph_of({"m.js": "import a from './a';\n", "a.ts": "export const a = 1;\n"})
    e = edge(g, "m.js", "./a")
    assert (e.status, e.target) == ("excluded", None)


def test_js_reexports_and_dynamic_imports_create_edges():
    g = graph_of({"i.js": "export * from './a';\nexport { b } from './b';\nconst l = () => import('./c');\n", "a.js": "", "b.js": "", "c.js": ""})
    assert {(e.kind, e.target) for e in g.edges} == {("reexport", "a.js"), ("reexport", "b.js"), ("dynamic_import", "c.js")}


def test_js_edges_are_marked_heuristic():
    g = graph_of({"a.js": "import './b.js';\n", "b.js": ""})
    assert edge(g, "a.js", "./b.js").confidence == "heuristic"


# --------------------------------------------------------------------------- #
# HTML / CSS
# --------------------------------------------------------------------------- #
def test_web_project_relationships():
    g = graph_of(WEB_PROJECT)
    assert edge(g, "index.html", "css/styles.css").target == "css/styles.css"
    assert edge(g, "index.html", "js/app.js").target == "js/app.js"
    assert edge(g, "css/styles.css", "base.css").target == "css/base.css"
    assert edge(g, "js/app.js", "./util.js").target == "js/util.js"
    assert edge(g, "index.html", "js/missing.js").status == "missing"
    assert edge(g, "js/app.js", "./config").status == "missing"
    logo = edge(g, "index.html", "img/logo.png")
    assert logo.status == "excluded" and "unsupported_extension" in logo.reason
    assert edge(g, "css/styles.css", "../img/logo.png").status == "excluded"


def test_html_query_strings_fragments_and_special_urls():
    g = graph_of(
        {
            "index.html": '<link rel="stylesheet" href="s.css?v=3#x"><script src="//cdn.x/y.js"></script>'
            '<img src="data:image/png;base64,AAA"><a href="#top"><script src="/app.js"></script><script src="../out.js"></script>',
            "s.css": "",
            "app.js": "",
        }
    )
    got = {e.specifier: (e.status, e.target) for e in g.edges}
    assert got["s.css?v=3#x"] == ("resolved", "s.css")
    assert got["//cdn.x/y.js"] == ("external", None)
    assert got["/app.js"] == ("resolved", "app.js")
    assert got["../out.js"][0] == "unresolved"
    assert not any(e.specifier.startswith("data:") for e in g.edges)


def test_css_import_bundler_alias_is_unresolved():
    g = graph_of({"a.css": '@import "~pkg/x.css";\n'})
    assert edge(g, "a.css", "~pkg/x.css").status == "unresolved"


# --------------------------------------------------------------------------- #
# Cycles, entry points, determinism
# --------------------------------------------------------------------------- #
def test_cycles_are_detected_in_every_language():
    g = graph_of(CYCLE_PROJECT)
    assert [c.files for c in g.cycles] == [("a.py", "b.py", "c.py"), ("js/x.js", "js/y.js"), ("self_ref.py",)]
    assert g.cycles[0].example == ("a.py", "b.py", "c.py", "a.py")
    assert g.cycles[2].example == ("self_ref.py", "self_ref.py")


def test_an_acyclic_project_has_no_cycles():
    assert graph_of(MIXED_PROJECT).cycles == ()


def test_cycle_detection_survives_a_very_long_chain_without_recursion():
    files = {f"m{i}.py": f"import m{i + 1}\n" for i in range(1500)}
    files["m1500.py"] = "import m0\n"
    g = analyze_project(make_zip(files), ProjectSettings(max_entries=2000)).graph
    assert len(g.cycles) == 1 and len(g.cycles[0].files) == 1501


def test_node_dependency_counts():
    g = graph_of(PYTHON_PROJECT)
    counts = {n.path: (n.dependency_count, n.dependent_count) for n in g.nodes}
    assert counts["app/main.py"] == (2, 0)
    assert counts["app/utils.py"] == (0, 2)
    assert g.dependencies["app/models.py"] == ("app/utils.py",)
    assert g.dependents["app/utils.py"] == ("app/main.py", "app/models.py")


def test_entry_points():
    g = graph_of(MIXED_PROJECT)
    assert [(e.path) for e in g.entry_points] == ["app/main.py", "web/index.html"]
    assert all(e.confidence == "heuristic" for e in g.entry_points)


def test_conventional_entry_names_only_count_if_nothing_imports_them():
    g = graph_of({"main.py": "x = 1\n", "app.py": "x = 1\n", "other.py": "import app\n"})
    assert [e.path for e in g.entry_points] == ["main.py"]


def test_graph_output_is_deterministic_and_independent_of_zip_order():
    items = sorted(MIXED_PROJECT.items())
    shuffled = dict(random.Random(3).sample(items, len(items)))
    first = analyze_project(make_zip(dict(items)), LIMITS)
    second = analyze_project(make_zip(shuffled), LIMITS)
    assert first.graph.edges == second.graph.edges
    assert first.graph.nodes == second.graph.nodes
    assert first.graph.cycles == second.graph.cycles
    assert first.graph.external_dependencies == second.graph.external_dependencies
    assert list(first.analyses) == list(second.analyses) == sorted(first.analyses)


def test_every_edge_keeps_its_source_line():
    g = graph_of(PYTHON_PROJECT)
    assert edge(g, "app/main.py", "app.utils").line == 4
    assert edge(g, "app/main.py", "requests").line == 6


def test_resolution_scales_linearly_enough():
    files = {f"pkg/m{i}.py": "".join(f"import pkg.m{(i + k) % 400}\n" for k in range(1, 6)) for i in range(400)}
    files["pkg/__init__.py"] = ""
    project = analyze_project(make_zip(files), ProjectSettings(max_entries=500))
    assert len(project.graph.edges) == 2000
