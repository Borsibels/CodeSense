"""Deterministic source-file discovery over a validated archive.

Every archive file ends up in exactly one of two lists: ``files`` (supported,
readable source) or ``excluded`` (with a machine-readable reason and a human
detail). Nothing is dropped silently. Paths are portable (``/``-separated,
relative to the archive root) and both lists are sorted by path.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from app.config import ProjectSettings
from app.services.project_ingestion import ArchiveMember, ProjectArchive
from app.services.project_models import Deadline, ExcludedFile, Language, ProjectFile

SUPPORTED_EXTENSIONS: dict[str, Language] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
}

# Compared case-insensitively against every directory component of a path.
IGNORED_DIRECTORIES = frozenset(
    {
        "node_modules",
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".next",
        "coverage",
        # Not required by the brief; same kind of generated/tooling noise.
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        ".idea",
        ".vscode",
        "__macosx",
    }
)

# Machine-written dependency/lock files and bundler artefacts, by file name.
GENERATED_NAMES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pipfile.lock",
        "composer.lock",
    }
)
_GENERATED_SUFFIXES = (".bundle.js", ".chunk.js", ".min.js.map", ".js.map", ".css.map")
_MINIFIED_SUFFIXES = (".min.js", ".min.css", ".min.mjs")

_MIN_SIZE_FOR_AVERAGE_CHECK = 2000
_MAX_LINE_CHARS = 1000
_MAX_AVERAGE_LINE_CHARS = 200
_TEXT_CONTROL_OK = {9, 10, 12, 13}  # tab, LF, FF, CR


@dataclass(frozen=True)
class DiscoveryResult:
    files: tuple[ProjectFile, ...]
    excluded: tuple[ExcludedFile, ...]


def extension_of(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def classify_path(path: str, size: int, limits: ProjectSettings) -> tuple[str, str] | None:
    """Reason to exclude ``path`` by name/size alone, or None if it is a candidate."""
    parts = path.split("/")
    for directory in parts[:-1]:
        if directory.lower() in IGNORED_DIRECTORIES:
            return "ignored_directory", f"Inside the ignored directory '{directory}'."
    name = parts[-1].lower()
    if name in GENERATED_NAMES or name.endswith(_GENERATED_SUFFIXES):
        return "generated_file", "Machine-generated dependency or build artefact."
    if name.endswith(_MINIFIED_SUFFIXES):
        return "minified", "The file name marks it as minified."
    extension = extension_of(path)
    if extension not in SUPPORTED_EXTENSIONS:
        shown = extension or "no extension"
        return "unsupported_extension", f"Unsupported file type ({shown})."
    if size > limits.max_file_bytes:
        return (
            "too_large",
            f"{size} bytes exceeds the {limits.max_file_bytes}-byte source file limit.",
        )
    return None


def decode_source(data: bytes) -> tuple[str | None, str | None, str | None]:
    """Decode and normalise a source file -> ``(text, reason, detail)``.

    ``reason`` is set (and ``text`` is None) for binary or non-UTF-8 content.
    Line endings are normalised to ``\n`` so line numbers mean the same thing
    to every analyser.
    """
    if b"\x00" in data:
        return None, "binary_content", "Contains NUL bytes; not a text file."
    sample = data[:8192]
    if sample and sum(1 for b in sample if b < 32 and b not in _TEXT_CONTROL_OK) > len(sample) // 10:
        return None, "binary_content", "Contains many control characters; not a text file."
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "invalid_encoding", "Not valid UTF-8 (only UTF-8 source is supported)."
    return text.replace("\r\n", "\n").replace("\r", "\n"), None, None


def looks_minified(text: str, language: Language, size: int) -> bool:
    if language not in ("javascript", "css"):
        return False
    lines = text.split("\n")
    longest = max((len(line) for line in lines), default=0)
    if longest > _MAX_LINE_CHARS:
        return True
    non_empty = [line for line in lines if line.strip()]
    return (
        size >= _MIN_SIZE_FOR_AVERAGE_CHECK
        and bool(non_empty)
        and sum(len(line) for line in non_empty) / len(non_empty) > _MAX_AVERAGE_LINE_CHARS
    )


def count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def discover_files(
    archive: ProjectArchive, limits: ProjectSettings, deadline: Deadline | None = None
) -> DiscoveryResult:
    files: list[ProjectFile] = []
    excluded: list[ExcludedFile] = []

    def exclude(member: ArchiveMember, reason: str, detail: str) -> None:
        excluded.append(ExcludedFile(member.path, reason, detail, member.size))

    for member in archive.members:  # already sorted by path
        if deadline:
            deadline.check()
        verdict = classify_path(member.path, member.size, limits)
        if verdict:
            exclude(member, *verdict)
            continue
        data = archive.read(member, limits.max_file_bytes)
        text, reason, detail = decode_source(data)
        if text is None:
            exclude(member, reason or "binary_content", detail or "")
            continue
        language = SUPPORTED_EXTENSIONS[extension_of(member.path)]
        if looks_minified(text, language, member.size):
            exclude(member, "minified", "Lines are too long for hand-written source (looks minified).")
            continue
        files.append(ProjectFile(member.path, language, text, member.size, count_lines(text)))

    return DiscoveryResult(files=tuple(files), excluded=tuple(excluded))
