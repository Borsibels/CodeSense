"""File discovery: supported types, ignored folders, exclusion reasons and deterministic order."""

from __future__ import annotations

import random

import pytest

from app.config import ProjectSettings
from app.services.project_discovery import classify_path, decode_source, discover_files
from app.services.project_ingestion import open_archive
from project_fixtures import IGNORED_PROJECT, MIXED_PROJECT, make_zip

LIMITS = ProjectSettings()


def discover(files, limits: ProjectSettings = LIMITS):
    archive = open_archive(make_zip(files), limits)
    try:
        return discover_files(archive, limits), archive
    finally:
        archive.close()


def reasons(result) -> dict[str, str]:
    return {e.path: e.reason for e in result.excluded}


@pytest.mark.parametrize(
    "name, language",
    [
        ("a.py", "python"),
        ("a.js", "javascript"),
        ("a.jsx", "javascript"),
        ("a.mjs", "javascript"),
        ("a.cjs", "javascript"),
        ("a.html", "html"),
        ("a.htm", "html"),
        ("a.css", "css"),
        ("UPPER.PY", "python"),
    ],
)
def test_supported_extensions(name, language):
    result, _ = discover({name: "x = 1\n"})
    assert [(f.path, f.language) for f in result.files] == [(name, language)]
    assert result.excluded == ()


@pytest.mark.parametrize("name", ["notes.txt", "data.json", "app.ts", "main.go", "Makefile", "README.md", "img.png"])
def test_unsupported_files_are_listed_not_dropped(name):
    result, _ = discover({name: "content"})
    assert result.files == ()
    assert reasons(result) == {name: "unsupported_extension"}


@pytest.mark.parametrize(
    "directory",
    ["node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next", "coverage"],
)
def test_ignored_directories(directory):
    result, _ = discover({f"{directory}/pkg/x.js": "var a;\n", "keep.js": "var b;\n"})
    assert [f.path for f in result.files] == ["keep.js"]
    assert reasons(result) == {f"{directory}/pkg/x.js": "ignored_directory"}


def test_ignored_directories_match_at_any_depth_and_any_case():
    result, _ = discover({"src/node_modules/x.js": "a", "web/Dist/y.js": "b", "src/main.js": "c"})
    assert [f.path for f in result.files] == ["src/main.js"]
    assert set(reasons(result).values()) == {"ignored_directory"}


def test_a_file_that_merely_starts_with_an_ignored_name_is_kept():
    result, _ = discover({"build.py": "x = 1\n", "distance/a.py": "y = 2\n"})
    assert {f.path for f in result.files} == {"build.py", "distance/a.py"}


def test_every_archive_file_is_accounted_for_exactly_once():
    result, archive = discover(IGNORED_PROJECT)
    seen = [f.path for f in result.files] + [e.path for e in result.excluded]
    assert sorted(seen) == sorted(IGNORED_PROJECT)
    assert len(seen) == len(set(seen))


def test_exclusion_reasons_for_the_ignored_fixture_are_accurate():
    result, _ = discover(IGNORED_PROJECT)
    assert [f.path for f in result.files] == ["main.py"]
    assert reasons(result) == {
        ".git/config": "ignored_directory",
        "__pycache__/main.cpython-312.pyc": "ignored_directory",
        "build/out.js": "ignored_directory",
        "data.bin": "unsupported_extension",
        "dist/bundle.js": "ignored_directory",
        "node_modules/lib/index.js": "ignored_directory",
        "notes.txt": "unsupported_extension",
        "static/app.min.js": "minified",
        "venv/lib/site.py": "ignored_directory",
    }


def test_binary_content_is_excluded():
    result, _ = discover({"image.py": b"\x00\x01\x02binary", "ok.py": "x = 1\n"})
    assert reasons(result) == {"image.py": "binary_content"}


def test_control_character_heavy_files_are_binary():
    result, _ = discover({"weird.py": bytes([1, 2, 3, 4, 5, 6, 7, 8] * 50)})
    assert reasons(result) == {"weird.py": "binary_content"}


def test_invalid_utf8_is_excluded_with_a_reason():
    result, _ = discover({"latin.py": "caf\xe9 = 1\n".encode("latin-1"), "ok.py": "x = 1\n"})
    assert reasons(result) == {"latin.py": "invalid_encoding"}


def test_utf8_bom_is_stripped_and_line_endings_normalised():
    result, _ = discover({"a.py": b"\xef\xbb\xbfx = 1\r\ny = 2\rz = 3\n"})
    (file,) = result.files
    assert file.text == "x = 1\ny = 2\nz = 3\n"
    assert file.line_count == 3


def test_line_count_and_lines():
    result, _ = discover({"a.py": "one\ntwo\nthree", "b.py": "", "c.py": "x\n"})
    counts = {f.path: f.line_count for f in result.files}
    assert counts == {"a.py": 3, "b.py": 0, "c.py": 1}
    assert result.files[0].lines == ["one", "two", "three"]


def test_minified_by_name_and_by_content():
    long_line = "var a=" + "1+" * 800 + "1;"
    dense = "function f(a){return a;}" * 200  # one 4800-char line
    result, _ = discover({"lib.min.js": "var a=1;", "bundle.js": long_line, "dense.js": dense, "normal.js": "var a = 1;\n"})
    assert [f.path for f in result.files] == ["normal.js"]
    assert set(reasons(result).values()) == {"minified"}


def test_wide_but_hand_written_files_with_long_average_lines_are_minified():
    text = ("var value_" + "x" * 250 + " = 1;\n") * 30
    result, _ = discover({"wide.js": text})
    assert reasons(result) == {"wide.js": "minified"}


def test_long_lines_in_python_are_not_a_minification_signal():
    result, _ = discover({"data.py": "X = '" + "a" * 3000 + "'\n"})
    assert [f.path for f in result.files] == ["data.py"]


def test_oversized_source_files_are_excluded_without_being_read(monkeypatch):
    limits = ProjectSettings(max_file_bytes=1000, max_entry_bytes=5000, max_uncompressed_bytes=50000)
    archive = open_archive(make_zip({"big.py": "x = 1\n" * 500, "small.py": "x = 1\n"}), limits)
    try:
        read = []
        original = archive.read
        monkeypatch.setattr(archive, "read", lambda member, n: read.append(member.path) or original(member, n))
        result = discover_files(archive, limits)
    finally:
        archive.close()
    assert reasons(result) == {"big.py": "too_large"}
    assert read == ["small.py"]


def test_generated_dependency_files():
    result, _ = discover({"package-lock.json": "{}", "yarn.lock": "x", "app.bundle.js": "var a;\n", "ok.js": "var b;\n"})
    assert [f.path for f in result.files] == ["ok.js"]
    assert set(reasons(result).values()) == {"generated_file", "generated_file", "generated_file"}


def test_output_order_is_independent_of_archive_order():
    names = [f"dir{i % 3}/file{i}.py" for i in range(30)] + ["a.js", "z.css", "m.html"]
    files = {n: f"x = {i}\n" for i, n in enumerate(names)}
    shuffled = dict(random.Random(7).sample(sorted(files.items()), len(files)))
    first, _ = discover(files)
    second, _ = discover(shuffled)
    assert [f.path for f in first.files] == sorted(files)
    assert first == second


def test_mixed_project_discovery():
    result, _ = discover(MIXED_PROJECT)
    assert [f.path for f in result.files] == sorted(
        p for p in MIXED_PROJECT if p.endswith((".py", ".js", ".html", ".css"))
    )
    assert reasons(result) == {"README.md": "unsupported_extension", "web/img/logo.png": "unsupported_extension"}


def test_classify_path_is_pure_and_precise():
    assert classify_path("src/a.py", 10, LIMITS) is None
    assert classify_path("src/a.py", LIMITS.max_file_bytes + 1, LIMITS)[0] == "too_large"
    assert classify_path("node_modules/a.py", 10, LIMITS)[0] == "ignored_directory"
    assert classify_path("a.txt", 10, LIMITS)[0] == "unsupported_extension"


def test_decode_source_reports_why_not():
    assert decode_source(b"ok")[0] == "ok"
    assert decode_source(b"a\x00b")[1] == "binary_content"
    assert decode_source(b"\xff\xfe\xfd")[1] == "invalid_encoding"
