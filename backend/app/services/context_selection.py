"""Deterministic context selection: which files and code regions matter, in what order.

No inference, embeddings or scoring models. The order is a fixed list of tiers
driven by the dependency graph and static symbols:

======  ================================================================
tier    what
======  ================================================================
0       the selected file / symbol
1       header (imports) of the selected file; its direct dependencies
2       files that import the selected file (callers / users)
3       entry points; other symbols in the selected file
4       neighbouring symbols; everything else, most-imported first
======  ================================================================

``intent`` changes *how much* of a related file is requested, not the order:

* ``overview`` - structure first: signature outlines of the focus file, entry
  points and the most-imported files; code only if budget remains.
* ``explain``  - the target in full, then *outlines* of what it depends on and
  small usage windows in files that import it.
* ``debug``    - the target in full, then the *implementations* of the
  dependency symbols the target actually mentions, then the places that use it.

The result is a list of :class:`Candidate`; :mod:`app.services.context_assembly`
decides how much of it fits the token budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.services.project_models import FileAnalysis, ProjectAnalysis, ProjectFile, Symbol

Intent = Literal["overview", "explain", "debug"]
INTENTS: tuple[str, ...] = ("overview", "explain", "debug")

_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_MAX_TARGET_NAMES = 30
_MAX_DEP_SYMBOLS_PER_FILE = 12
_HEADER_MAX_LINES = 30
_MAX_AVAILABLE_NAMES = 15


class SelectionError(Exception):
    """The request names something that is not in the project."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ContextRequest:
    intent: Intent = "overview"
    path: str | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class Candidate:
    kind: Literal["code", "outline"]
    path: str
    start_line: int
    end_line: int
    tier: int
    reason: str
    symbol: str | None = None
    # The thing the user asked about (whole selected file). If it cannot fit comfortably the
    # assembler shows an outline of it first and caps the share of the budget its code may use.
    primary: bool = False


@dataclass(frozen=True)
class Selection:
    candidates: tuple[Candidate, ...]
    target_path: str | None
    target_symbols: tuple[str, ...]  # qualified names actually matched


def normalize_request_path(raw: str) -> str:
    path = raw.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _identifiers(text: str) -> set[str]:
    return set(_IDENT.findall(text))


def _lines(file: ProjectFile, start: int, end: int) -> str:
    return "\n".join(file.lines[start - 1 : end])


def _find_symbols(analysis: FileAnalysis, wanted: str) -> list[Symbol]:
    exact = [s for s in analysis.symbols if s.qualified_name == wanted]
    if exact:
        return exact
    return [s for s in analysis.symbols if s.name == wanted][:5]


_MAX_INNERMOST_INPUT = 200


def _innermost(symbols: list[Symbol]) -> list[Symbol]:
    """Drop symbols that contain another symbol in the list (keep the most specific ones)."""
    symbols = symbols[:_MAX_INNERMOST_INPUT]  # bounded: this comparison is quadratic
    kept = []
    for s in symbols:
        contains_other = any(
            o is not s and s.start_line <= o.start_line and o.end_line <= s.end_line and (o.start_line, o.end_line) != (s.start_line, s.end_line)
            for o in symbols
        )
        if not contains_other:
            kept.append(s)
    return kept


class _Planner:
    def __init__(self, project: ProjectAnalysis, request: ContextRequest) -> None:
        self.project = project
        self.request = request
        self.files = project.by_path
        self.graph = project.graph
        self.out: list[Candidate] = []

    # ----- candidate helpers --------------------------------------------- #
    def code(
        self, path: str, start: int, end: int, tier: int, reason: str, symbol: str | None = None, primary: bool = False
    ) -> None:
        if end >= start >= 1:
            self.out.append(Candidate("code", path, start, end, tier, reason, symbol, primary))

    def outline(self, path: str, tier: int, reason: str) -> None:
        self.out.append(Candidate("outline", path, 0, 0, tier, reason))

    def whole_file(self, path: str, tier: int, reason: str, primary: bool = False) -> None:
        count = self.files[path].line_count
        self.code(path, 1, count, tier, reason, primary=primary)

    # ----- planning ------------------------------------------------------ #
    def plan(self) -> Selection:
        request = self.request
        path = request.path
        matched: list[Symbol] = []

        if path is not None:
            analysis = self.project.analyses[path]
            if request.symbol:
                matched = _find_symbols(analysis, request.symbol)
                if not matched:
                    available = [s.qualified_name for s in analysis.symbols if s.kind != "variable"][:_MAX_AVAILABLE_NAMES]
                    hint = f" Symbols found in this file: {', '.join(available)}." if available else " No symbols were detected in this file."
                    raise SelectionError("SYMBOL_NOT_FOUND", f"No symbol named '{request.symbol}' in {path}.{hint}")

        if request.intent == "overview":
            self.plan_overview(path)
        else:
            assert path is not None  # validated by select_context
            self.plan_focused(path, matched)
        return Selection(
            candidates=tuple(self.out),
            target_path=path,
            target_symbols=tuple(s.qualified_name for s in matched),
        )

    def plan_overview(self, focus: str | None) -> None:
        if focus:
            self.outline(focus, 0, "focus file")
        for entry in self.graph.entry_points:
            self.outline(entry.path, 1, f"entry point: {entry.reason}")
        ranked = sorted(self.graph.nodes, key=lambda n: (-n.dependent_count, n.path))
        for node in ranked:
            if node.dependent_count:
                self.outline(node.path, 2, f"imported by {node.dependent_count} file(s)")
        for node in sorted(self.graph.nodes, key=lambda n: n.path):
            self.outline(node.path, 3, "project file")
        # Code last: only if the structure above left room.
        if focus:
            self.whole_file(focus, 4, "focus file")
        for entry in self.graph.entry_points:
            self.whole_file(entry.path, 5, "entry point")

    def plan_focused(self, path: str, matched: list[Symbol]) -> None:
        debug = self.request.intent == "debug"
        file = self.files[path]
        analysis = self.project.analyses[path]

        # Tier 0: the target itself.
        if matched:
            for symbol in matched:
                self.code(path, symbol.start_line, symbol.end_line, 0, "selected symbol", symbol.qualified_name)
            target_text = "\n".join(_lines(file, s.start_line, s.end_line) for s in matched)
            first_symbol_line = min((s.start_line for s in analysis.symbols), default=file.line_count + 1)
            header_end = min(first_symbol_line - 1, _HEADER_MAX_LINES)
            if header_end >= 1:
                self.code(path, 1, header_end, 1, "header (imports) of the selected file")
        else:
            self.whole_file(path, 0, "selected file", primary=True)
            target_text = file.text

        names_used = _identifiers(target_text)

        # Tier 1: what the target depends on.
        for dep in self.graph.dependencies.get(path, ()):
            if debug:
                self.dependency_symbols(dep, names_used, 1)
            else:
                self.outline(dep, 1, "direct dependency")
        if debug:
            for dep in self.graph.dependencies.get(path, ()):
                self.outline(dep, 3, "direct dependency (outline)")

        # Tier 2: who uses the target.
        if matched:
            target_names = {matched[0].name} | {s.name for s in matched}
        else:
            target_names = {
                s.name
                for s in analysis.symbols
                if s.parent is None and s.kind in ("function", "async_function", "class")
            }
            target_names = set(sorted(target_names)[:_MAX_TARGET_NAMES])
        for importer in self.graph.dependents.get(path, ()):
            self.importer_usage(importer, target_names)

        # Tier 3/4: entry points and neighbours, then everything else.
        for entry in self.graph.entry_points:
            if entry.path != path:
                self.outline(entry.path, 3, f"entry point: {entry.reason}")
        if matched:
            self.outline(path, 3, "other symbols in the selected file")
            if debug:
                self.neighbours(path, matched)
        ranked = sorted(self.graph.nodes, key=lambda n: (-n.dependent_count, n.path))
        for node in ranked:
            if node.path != path:
                self.outline(node.path, 4, "project file")

    def dependency_symbols(self, dep: str, names_used: set[str], tier: int) -> None:
        analysis = self.project.analyses[dep]
        hits = [
            s
            for s in analysis.symbols
            if s.parent is None and s.kind != "variable" and s.name in names_used
        ]
        for symbol in hits[:_MAX_DEP_SYMBOLS_PER_FILE]:
            self.code(dep, symbol.start_line, symbol.end_line, tier, "implementation used by the selected code", symbol.qualified_name)

    def importer_usage(self, importer: str, target_names: set[str]) -> None:
        file = self.files[importer]
        analysis = self.project.analyses[importer]
        hits = []
        for symbol in analysis.symbols:
            if symbol.kind == "variable":
                continue
            if target_names & _identifiers(_lines(file, symbol.start_line, symbol.end_line)):
                hits.append(symbol)
        hits = _innermost(hits)
        if hits and self.request.intent != "overview":
            for symbol in hits[:_MAX_DEP_SYMBOLS_PER_FILE]:
                self.code(importer, symbol.start_line, symbol.end_line, 2, "uses the selected code", symbol.qualified_name)
        else:
            self.outline(importer, 2, "imports the selected file")

    def neighbours(self, path: str, matched: list[Symbol]) -> None:
        siblings = [s for s in self.project.analyses[path].symbols if s.kind != "variable"]
        chosen = {(s.start_line, s.end_line) for s in matched}
        for symbol in matched:
            before = [s for s in siblings if s.end_line < symbol.start_line and s.parent == symbol.parent]
            after = [s for s in siblings if s.start_line > symbol.end_line and s.parent == symbol.parent]
            for neighbour in (before[-1:] + after[:1]):
                if (neighbour.start_line, neighbour.end_line) not in chosen:
                    self.code(path, neighbour.start_line, neighbour.end_line, 4, "neighbouring symbol", neighbour.qualified_name)


def select_context(project: ProjectAnalysis, request: ContextRequest) -> Selection:
    """Validate the request against the project and return the ordered candidates."""
    path = normalize_request_path(request.path) if request.path else None
    symbol = request.symbol.strip() if request.symbol and request.symbol.strip() else None

    if symbol and not path:
        raise SelectionError("FILE_REQUIRED", "A symbol can only be selected together with the file that contains it.")
    if request.intent != "overview" and not path:
        raise SelectionError("FILE_REQUIRED", f"The '{request.intent}' intent needs a selected file.")
    if path and path not in project.by_path:
        excluded = next((e for e in project.excluded if e.path == path), None)
        if excluded:
            raise SelectionError("FILE_NOT_ANALYZED", f"'{path}' is in the archive but was excluded ({excluded.reason}).")
        raise SelectionError("FILE_NOT_FOUND", f"'{path}' is not in the uploaded project.")

    return _Planner(project, ContextRequest(request.intent, path, symbol)).plan()
