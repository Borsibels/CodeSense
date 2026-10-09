"""Archive security: hostile ZIPs are refused, valid ones are only inspected, nothing is executed."""

from __future__ import annotations

import re
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from app.config import ConfigError, ProjectSettings
from app.services.project_ingestion import ArchiveError, normalize_member_path, open_archive
from app.services.project_pipeline import analyze_project
from project_fixtures import (
    PYTHON_PROJECT,
    make_duplicate_zip,
    make_encrypted_zip,
    make_special_zip,
    make_symlink_zip,
    make_zip,
    make_zip_bomb,
    patch_names,
)

APP_DIR = Path(__file__).resolve().parent.parent / "app"
LIMITS = ProjectSettings()


def rejected(data: bytes, limits: ProjectSettings = LIMITS) -> ArchiveError:
    with pytest.raises(ArchiveError) as info:
        open_archive(data, limits).close()
    return info.value


# --------------------------------------------------------------------------- #
# Valid archives
# --------------------------------------------------------------------------- #
def test_a_valid_archive_is_accepted_and_members_are_sorted():
    archive = open_archive(make_zip({"b.py": "x = 1\n", "a/c.py": "y = 2\n", "a/": ""}), LIMITS)
    try:
        assert [m.path for m in archive.members] == ["a/c.py", "b.py"]
        assert archive.entry_count == 3  # two files + one directory entry
        assert archive.uncompressed_bytes == len("x = 1\n") + len("y = 2\n")
        assert archive.decompressed_bytes == 0  # nothing is decompressed until a file is read
    finally:
        archive.close()


def test_read_returns_exact_content_and_counts_bytes():
    archive = open_archive(make_zip({"a.py": "print(1)\n"}), LIMITS)
    try:
        assert archive.read(archive.members[0], 1000) == b"print(1)\n"
        assert archive.decompressed_bytes == 9
    finally:
        archive.close()


def test_read_refuses_to_exceed_the_byte_limit():
    archive = open_archive(make_zip({"a.py": "x" * 100}), LIMITS)
    try:
        with pytest.raises(ArchiveError) as info:
            archive.read(archive.members[0], 50)
        assert info.value.code == "ENTRY_TOO_LARGE"
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# Not a usable archive
# --------------------------------------------------------------------------- #
def test_empty_upload():
    assert rejected(b"").code == "INVALID_ARCHIVE"


def test_random_bytes_are_not_a_zip():
    assert rejected(b"this is definitely not a zip file").code == "INVALID_ARCHIVE"


def test_a_zip_with_a_prefix_is_refused():
    assert rejected(b"MZ" + make_zip({"a.py": "x=1\n"})).code == "INVALID_ARCHIVE"


def test_a_truncated_archive_is_corrupt():
    data = make_zip({"a.py": "x = 1\n" * 100})
    assert rejected(data[: len(data) // 2]).code == "INVALID_ARCHIVE"


def test_garbage_after_a_valid_signature():
    assert rejected(b"PK\x03\x04" + b"\xff" * 200).code == "INVALID_ARCHIVE"


def test_a_zip_with_no_entries():
    assert rejected(b"PK\x05\x06" + b"\x00" * 18).code == "EMPTY_ARCHIVE"


def test_corrupt_entry_data_is_reported_when_read():
    data = bytearray(make_zip({"a.py": "abcdefgh" * 20}, compression=zipfile.ZIP_STORED))
    data[data.index(b"abcdefgh") + 3] ^= 0xFF  # flip one payload byte: the CRC no longer matches
    archive = open_archive(bytes(data), LIMITS)
    try:
        with pytest.raises(ArchiveError) as info:
            archive.read(archive.members[0], 1000)
        assert info.value.code == "INVALID_ARCHIVE"
    finally:
        archive.close()


def test_an_entry_whose_header_understates_its_size_is_not_silently_truncated():
    data = bytearray(make_zip({"a.py": "x" * 1000}, compression=zipfile.ZIP_STORED))
    central = data.rindex(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 24, 10)  # declared uncompressed size
    archive = open_archive(bytes(data), LIMITS)
    try:
        with pytest.raises(ArchiveError):
            archive.read(archive.members[0], 1000)
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "../evil.py",
        "a/../../evil.py",
        "a/../b.py",  # needs "fixing" to be safe: refused, not repaired
        "./a.py",
        "a//b.py",
        "/etc/cron.py",
        "C:/evil.py",
        "c:evil.py",
        "a.py:stream",
        "evil.py.",
        "dir /a.py",
        "a\nb.py",
        "a\u202eb.py",  # right-to-left override
    ],
)
def test_unsafe_paths_are_refused(name):
    assert rejected(make_zip({name: "x = 1\n"})).code == "UNSAFE_PATH"


@pytest.mark.parametrize(
    "placeholder, name",
    [
        (b"..|..|evil.py", "..\\..\\evil.py"),
        (b"C:|evil.py", "C:\\evil.py"),
        (b"|share|x.py", "\\share\\x.py"),
        (b"a|..|..|b.py", "a\\..\\..\\b.py"),
    ],
)
def test_windows_style_paths_are_refused(placeholder, name):
    """Backslash names are what Compress-Archive/Windows tools write; they must not slip through."""
    data = patch_names(make_zip({placeholder.decode(): "x = 1\n"}), {b"|": b"\\"})
    error = rejected(data)
    assert error.code == "UNSAFE_PATH"
    assert name.split("\\")[0] in error.message or "path" in error.message.lower() or "drive" in error.message


def test_backslash_separators_are_normalised_not_rejected():
    data = patch_names(make_zip({"src|app.py": "x = 1\n"}), {b"|": b"\\"})
    archive = open_archive(data, LIMITS)
    try:
        assert [m.path for m in archive.members] == ["src/app.py"]
    finally:
        archive.close()


def test_error_messages_escape_hostile_names():
    error = rejected(make_zip({"a\x1b[31m/../x.py": "x"}))
    assert "\x1b" not in error.message
    assert len(error.message) < 400


def test_path_depth_and_length_limits():
    limits = ProjectSettings(max_path_depth=3, max_path_chars=30)
    assert rejected(make_zip({"a/b/c/d.py": "x"}), limits).code == "UNSAFE_PATH"
    assert rejected(make_zip({"a" * 40 + ".py": "x"}), limits).code == "UNSAFE_PATH"


@pytest.mark.parametrize("name", ["a.py", "src/app.py", "a/b/c/d.py", "name with spaces.py", "ünïcode.py", ".hidden/x.py"])
def test_ordinary_paths_normalise_to_themselves(name):
    assert normalize_member_path(name, LIMITS) == (name, False)


def test_directory_entries_are_recognised():
    assert normalize_member_path("src/", LIMITS) == ("src", True)


# --------------------------------------------------------------------------- #
# Symlinks, special files, encryption, methods
# --------------------------------------------------------------------------- #
def test_symlinks_are_refused():
    assert rejected(make_symlink_zip()).code == "UNSUPPORTED_ENTRY"


@pytest.mark.parametrize("kind", [stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK])
def test_special_files_are_refused(kind):
    assert rejected(make_special_zip("dev.py", kind | 0o644)).code == "UNSUPPORTED_ENTRY"


def test_regular_files_with_unix_modes_are_fine():
    archive = open_archive(make_special_zip("ok.py", stat.S_IFREG | 0o644, b"x = 1\n"), LIMITS)
    archive.close()


def test_encrypted_entries_are_refused():
    assert rejected(make_encrypted_zip()).code == "ENCRYPTED_ARCHIVE"


def test_only_stored_and_deflate_are_accepted():
    data = make_zip({"a.py": "x = 1\n" * 50}, compression=zipfile.ZIP_BZIP2)
    assert rejected(data).code == "UNSUPPORTED_ENTRY"


# --------------------------------------------------------------------------- #
# Duplicates and collisions
# --------------------------------------------------------------------------- #
def test_duplicate_paths_are_refused():
    assert rejected(make_duplicate_zip("a.py", "a.py")).code == "DUPLICATE_PATH"


def test_case_insensitive_collisions_are_refused():
    error = rejected(make_zip({"README.py": "x", "readme.py": "y"}))
    assert error.code == "DUPLICATE_PATH"


def test_unicode_normalisation_collisions_are_refused():
    composed, decomposed = "caf\u00e9.py", "cafe\u0301.py"
    assert rejected(make_zip({composed: "x", decomposed: "y"})).code == "DUPLICATE_PATH"


def test_a_name_used_as_both_file_and_directory_is_refused():
    assert rejected(make_zip({"a": "x", "a/b.py": "y"})).code == "DUPLICATE_PATH"


# --------------------------------------------------------------------------- #
# Size limits and bombs
# --------------------------------------------------------------------------- #
def test_oversized_archive_is_413():
    error = rejected(make_zip({"a.py": bytes(range(256)) * 8}, compression=zipfile.ZIP_STORED), ProjectSettings(max_zip_bytes=1000))
    assert (error.code, error.status) == ("ARCHIVE_TOO_LARGE", 413)


def test_oversized_individual_entry():
    limits = ProjectSettings(max_entry_bytes=1000, max_file_bytes=500, max_uncompressed_bytes=100000)
    assert rejected(make_zip({"data.bin": b"x" * 2000}), limits).code == "ENTRY_TOO_LARGE"


def test_oversized_project_when_uncompressed():
    limits = ProjectSettings(max_uncompressed_bytes=3000, max_entry_bytes=2000, max_file_bytes=1000)
    files = {f"f{i}.py": "x = 1\n" * 100 for i in range(10)}  # 600 bytes each
    assert rejected(make_zip(files), limits).code == "UNCOMPRESSED_TOO_LARGE"


def test_excessive_compression_ratio_is_refused_as_a_zip_bomb():
    data = make_zip_bomb(4)
    assert len(data) < 20_000  # 4 MiB of zeros in a few KiB
    assert rejected(data).code == "SUSPICIOUS_COMPRESSION"


def test_the_overall_ratio_is_checked_too():
    # Many files that are each under the per-file floor but together compress >100:1.
    files = {f"z{i}.py": b"\x00" * 60_000 for i in range(5)}
    limits = ProjectSettings(ratio_floor_bytes=100_000, max_compression_ratio=50)
    assert rejected(make_zip(files), limits).code == "SUSPICIOUS_COMPRESSION"


def test_small_highly_compressible_files_are_not_bombs():
    archive = open_archive(make_zip({"a.py": "#" * 5000}), LIMITS)  # ratio > 100 but tiny
    archive.close()


def test_too_many_entries():
    files = {f"f{i}.py": "x" for i in range(501)}
    error = rejected(make_zip(files))
    assert error.code == "TOO_MANY_ENTRIES"


def test_entry_count_is_checked_before_the_directory_is_parsed(monkeypatch):
    data = make_zip({f"f{i}.py": "" for i in range(30)})

    def explode(*args, **kwargs):
        raise AssertionError("the central directory must not be parsed for an over-limit archive")

    monkeypatch.setattr(zipfile, "ZipFile", explode)
    assert rejected(data, ProjectSettings(max_entries=10)).code == "TOO_MANY_ENTRIES"


def test_entries_claiming_more_compressed_data_than_exists_are_refused():
    data = bytearray(make_zip({"a.py": "x" * 100}, compression=zipfile.ZIP_STORED))
    central = data.rindex(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 20, 50_000_000)  # compressed size far beyond the file
    assert rejected(bytes(data)).code == "SUSPICIOUS_COMPRESSION"


def test_rejected_archives_do_not_leak_file_handles():
    for _ in range(50):
        with pytest.raises(ArchiveError):
            open_archive(make_symlink_zip(), LIMITS)  # would exhaust handles/warn if left open


# --------------------------------------------------------------------------- #
# Nothing is written to disk, nothing is executed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module", ["project_ingestion.py", "project_discovery.py", "project_pipeline.py"])
def test_ingestion_code_never_touches_the_filesystem(module):
    source = (APP_DIR / "services" / module).read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    assert not re.search(r"\bextract(?:all)?\(|\btempfile\b|\bos\.|\bPath\(|\bshutil\b|(?<![\w.])open\(|\bpathlib\b", code)


def test_the_analysis_modules_never_execute_or_import_code():
    forbidden = re.compile(r"(?<![\w.])(?:eval|exec|compile|__import__)\(|\bimportlib\b|\bsubprocess\b|\bruntime\b|\bpickle\b")
    for module in (
        "project_ingestion",
        "project_discovery",
        "python_analysis",
        "js_analysis",
        "web_analysis",
        "static_analysis",
        "dependency_graph",
        "context_selection",
        "context_assembly",
        "project_pipeline",
    ):
        source = (APP_DIR / "services" / f"{module}.py").read_text(encoding="utf-8")
        code = "\n".join(
            line.split("#", 1)[0] for line in source.splitlines() if not line.strip().startswith(('"', "'", "``"))
        )
        assert not forbidden.search(code), module


def test_uploaded_python_is_parsed_but_never_run(tmp_path):
    marker = tmp_path / "executed.txt"
    hostile = f'''
import os, sys
open({str(marker)!r}, "w").write("pwned")
os.system("echo pwned")
__import__("subprocess").run(["echo", "pwned"])
sys.exit(3)
raise RuntimeError("boom")
while True:
    pass
'''
    project = analyze_project(make_zip({"evil.py": hostile}), LIMITS)
    assert project.analyses["evil.py"].has_syntax_error is False
    assert not marker.exists()


def test_uploaded_javascript_and_html_are_scanned_but_never_run(tmp_path):
    marker = tmp_path / "js_executed.txt"
    files = {
        "evil.js": f"require('fs').writeFileSync({str(marker)!r}, 'x'); process.exit(1);\n",
        "evil.html": f"<script>fetch('http://evil.invalid/x');</script><img src=http://evil.invalid/a.png>\n",
    }
    project = analyze_project(make_zip(files), LIMITS)
    assert not marker.exists()
    assert {e.specifier for e in project.graph.edges if e.external_kind == "url"} == {"http://evil.invalid/a.png"}


def test_analysis_leaves_the_working_directory_untouched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    analyze_project(make_zip(PYTHON_PROJECT), LIMITS)
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_project_settings_defaults_match_the_documented_limits():
    s = ProjectSettings()
    assert s.max_zip_bytes == 10 * 1024 * 1024
    assert s.max_uncompressed_bytes == 25 * 1024 * 1024
    assert s.max_file_bytes == 512 * 1024
    assert s.max_entries == 500
    assert s.max_compression_ratio == 100


def test_project_settings_read_the_environment():
    s = ProjectSettings.from_env({"PROJECT_MAX_ENTRIES": "50", "PROJECT_MAX_ZIP_BYTES": "2048", "PROJECT_MAX_ENTRY_BYTES": "1024", "PROJECT_MAX_FILE_BYTES": "512", "PROJECT_MAX_UNCOMPRESSED_BYTES": "4096"})
    assert (s.max_entries, s.max_zip_bytes) == (50, 2048)


@pytest.mark.parametrize(
    "env",
    [
        {"PROJECT_MAX_ENTRIES": "0"},
        {"PROJECT_MAX_ENTRIES": "many"},
        {"PROJECT_PROCESSING_TIMEOUT": "nan"},
        {"PROJECT_MAX_FILE_BYTES": str(10**9)},  # larger than the entry limit
        {"PROJECT_INSTRUCTION_RESERVE_TOKENS": "-1"},
    ],
)
def test_bad_project_settings_fail_with_a_clear_error(env):
    with pytest.raises(ConfigError):
        ProjectSettings.from_env(env)
