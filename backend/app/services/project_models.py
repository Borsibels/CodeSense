"""Plain data types shared by the project-analysis services (no I/O, no framework)."""

from __future__ import annotations

import time
from functools import cached_property
from dataclasses import dataclass, field
from typing import Literal

Language = Literal["python", "javascript", "html", "css"]
# "confirmed": produced by a real parser (Python ast, html.parser) - a static fact.
# "heuristic": produced by a lightweight scanner - likely right, never guaranteed.
Confidence = Literal["confirmed", "heuristic"]


class ProcessingTimeout(Exception):
    """The per-request processing deadline passed."""


class Deadline:
    """Cooperative deadline: long loops call :meth:`check` between units of work."""

    def __init__(self, seconds: float | None) -> None:
        self._end = None if seconds is None else time.perf_counter() + seconds

    def check(self) -> None:
        if self._end is not None and time.perf_counter() > self._end:
            raise ProcessingTimeout("project processing exceeded its time limit")


@dataclass(frozen=True)
class ProjectFile:
    """A supported, readable source file. ``text`` uses ``\n`` line endings."""

    path: str
    language: Language
    text: str
    size_bytes: int
    line_count: int

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n")[: self.line_count]


@dataclass(frozen=True)
class ExcludedFile:
    path: str
    reason: str
    detail: str
    size_bytes: int


@dataclass(frozen=True)
class Symbol:
    name: str
    qualified_name: str
    # function | async_function | class | method | async_method | variable | rule
    kind: str
    start_line: int
    end_line: int
    signature: str | None = None
    parent: str | None = None
    doc: str | None = None
    exported: bool | None = None
    confidence: Confidence = "confirmed"


@dataclass(frozen=True)
class Reference:
    """An import / script / stylesheet / @import / url() reference found in a file."""

    # import | require | dynamic_import | reexport | script | stylesheet | css_import | asset | link
    kind: str
    specifier: str
    line: int
    names: tuple[str, ...] = ()
    level: int = 0  # Python relative-import dots
    top_level: bool = True
    confidence: Confidence = "confirmed"


@dataclass(frozen=True)
class ExportItem:
    name: str
    kind: str  # named | default | reexport | star | commonjs
    line: int
    source: str | None = None
    confidence: Confidence = "heuristic"


@dataclass(frozen=True)
class Diagnostic:
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    line: int | None = None
    column: int | None = None
    confidence: Confidence = "confirmed"


@dataclass(frozen=True)
class ElementId:
    id: str
    tag: str
    line: int


@dataclass(frozen=True)
class FileAnalysis:
    path: str
    language: Language
    parser: str
    confidence: Confidence
    line_count: int
    symbols: tuple[Symbol, ...] = ()
    references: tuple[Reference, ...] = ()
    exports: tuple[ExportItem, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    doc: str | None = None  # first line of the module docstring (Python)
    has_main_guard: bool = False  # Python: ``if __name__ == "__main__":`` at top level
    title: str | None = None  # HTML <title>
    inline_script_count: int = 0
    inline_style_count: int = 0
    element_ids: tuple[ElementId, ...] = ()
    css_classes: tuple[str, ...] = ()
    css_ids: tuple[str, ...] = ()
    has_syntax_error: bool = False


@dataclass(frozen=True)
class GraphEdge:
    source: str
    specifier: str
    kind: str
    line: int
    # resolved | missing | excluded | external | unresolved
    status: str
    target: str | None = None  # project path when status == resolved
    external_kind: str | None = None  # stdlib | package | url | builtin
    reason: str | None = None
    confidence: Confidence = "confirmed"


@dataclass(frozen=True)
class GraphNode:
    path: str
    language: Language
    dependency_count: int
    dependent_count: int


@dataclass(frozen=True)
class Cycle:
    files: tuple[str, ...]  # every file in the strongly connected component, sorted
    example: tuple[str, ...]  # one concrete loop, first == last


@dataclass(frozen=True)
class EntryPoint:
    path: str
    reason: str
    confidence: Confidence = "heuristic"


@dataclass(frozen=True)
class ExternalDependency:
    name: str
    kind: str  # package | stdlib | builtin | url
    files: tuple[str, ...]  # project files that reference it, sorted


@dataclass(frozen=True)
class DependencyGraph:
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    cycles: tuple[Cycle, ...] = ()
    entry_points: tuple[EntryPoint, ...] = ()
    external_dependencies: tuple[ExternalDependency, ...] = ()
    # forward / reverse adjacency over *resolved* edges, derived once
    dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False, compare=False)
    dependents: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class ProjectStats:
    archive_bytes: int
    entry_count: int
    uncompressed_bytes: int
    source_bytes: int


@dataclass(frozen=True)
class ProjectAnalysis:
    stats: ProjectStats
    files: tuple[ProjectFile, ...]
    excluded: tuple[ExcludedFile, ...]
    analyses: dict[str, FileAnalysis]
    graph: DependencyGraph

    @cached_property
    def by_path(self) -> dict[str, ProjectFile]:
        return {f.path: f for f in self.files}
