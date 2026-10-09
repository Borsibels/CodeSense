"""Token-budgeted context assembly.

Turns the ordered :class:`~app.services.context_selection.Candidate` list into
one text block that fits the model's input budget, plus an exact report of what
made it in and what did not.

Budget
------
``context_limit = TokenBudget.input_limit - instruction_reserve``. The reserve is
space kept free for the future system prompt, task instructions, JSON-schema
guidance and chat formatting; source code never gets to use it. All counts come
from the Phase 2.5 :class:`~app.services.token_budget.TokenBudget` (estimates,
never exact) and the *final* text is re-counted as a whole before it is returned.

Honesty rules
-------------
* Nothing is cut silently. Every omitted stretch of a file is marked in the text
  (``[... lines 43-79 not included ...]``) and listed in the report.
* A file is ``full`` only if every non-blank line is present; otherwise it is
  ``partial`` (some code) or ``outline_only`` (signatures, no bodies).
* Line numbers are the real source line numbers.
* If a region does not fit, it is split at symbol boundaries (a class into its
  methods, a file into its functions); a single oversized symbol contributes a
  leading block of lines. Arbitrary character truncation is never used, except
  that a single line over 400 characters is clipped with an explicit marker.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from app.services.context_selection import Candidate, ContextRequest, Selection
from app.services.project_models import FileAnalysis, GraphEdge, ProjectAnalysis, Symbol
from app.services.token_budget import TokenBudget

MAX_LINE_CHARS = 400
GAP_ALLOWANCE_TOKENS = 18  # one "[... lines a-b not included ...]" marker
MIN_USEFUL_TOKENS = 40
OUTLINE_MAX_ENTRIES = 40
DEPENDENCY_LINE_ITEMS = 10
MAX_REPORTED_OMISSIONS = 200
MAX_REPORTED_RANGES = 25
# Phase 4.5 CODE MAP (explain / overview only): a hard cap, counted inside the context budget.
CODE_MAP_MAX_TOKENS = 120
CODE_MAP_MAX_IMPORTS = 4
CODE_MAP_INTENTS = ("explain", "overview")
_MAP_KINDS = frozenset({"class", "function", "async_function", "method", "async_method"})
_KIND_WORDS = {
    "class": "class", "function": "function", "async_function": "async function",
    "method": "method", "async_method": "async method",
}

Range = tuple[int, int]


# --------------------------------------------------------------------------- #
# Report types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FileCoverage:
    path: str
    # full | partial | outline_only | not_included
    status: str
    line_count: int
    included_ranges: tuple[Range, ...] = ()
    omitted_ranges: tuple[Range, ...] = ()
    outline_entries_listed: int | None = None
    outline_entries_total: int | None = None
    reasons: tuple[str, ...] = ()
    estimated_tokens: int = 0


@dataclass(frozen=True)
class OmittedRegion:
    path: str
    kind: str  # code | outline
    start_line: int | None
    end_line: int | None
    symbol: str | None
    reason: str
    requested_for: str


@dataclass(frozen=True)
class BudgetReport:
    input_limit: int
    instruction_reserve: int
    context_limit: int
    estimated_tokens: int
    remaining_tokens: int
    is_estimate: bool = True


@dataclass(frozen=True)
class CoverageSummary:
    files_total: int
    files_full: int
    files_partial: int
    files_outline_only: int
    files_not_included: int


@dataclass(frozen=True)
class AssembledContext:
    text: str
    intent: str
    target_path: str | None
    target_symbols: tuple[str, ...]
    budget: BudgetReport
    summary: CoverageSummary
    coverage: tuple[FileCoverage, ...]
    omitted_regions: tuple[OmittedRegion, ...]
    omitted_region_total: int
    warnings: tuple[str, ...] = ()
    # Uncapped, merged line ranges of CODE that appear in ``text`` (``coverage`` caps its lists at
    # MAX_REPORTED_RANGES). Source-reference validation needs the complete set.
    shown_ranges: dict[str, tuple[Range, ...]] = field(default_factory=dict)
    # Files that appear in ``text`` as a signature OUTLINE (their bodies are not shown by it).
    outlined_paths: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- #
# Range helpers
# --------------------------------------------------------------------------- #
def merge_ranges(ranges: list[Range]) -> list[Range]:
    merged: list[Range] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_ranges(wanted: Range, covered: list[Range]) -> list[Range]:
    """Parts of ``wanted`` not inside the merged, sorted ``covered`` ranges."""
    result: list[Range] = []
    cursor = wanted[0]
    for start, end in covered:
        if end < cursor:
            continue
        if start > wanted[1]:
            break
        if start > cursor:
            result.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= wanted[1]:
        result.append((cursor, wanted[1]))
    return result


# --------------------------------------------------------------------------- #
# Assembler
# --------------------------------------------------------------------------- #
@dataclass
class _Segment:
    start: int
    end: int
    symbol: str | None
    reason: str
    cost: int


@dataclass
class _Outline:
    lines: list[str]
    listed: int
    total: int
    reason: str
    cost: int


@dataclass
class _Block:
    path: str
    kind: str  # code | outline
    segment: _Segment | None = None


@dataclass
class _State:
    segments: dict[str, list[_Segment]] = field(default_factory=dict)
    outlines: dict[str, _Outline] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)  # files by first inclusion
    log: list[_Block] = field(default_factory=list)
    header_cost: dict[str, int] = field(default_factory=dict)
    redundant_outlines: set[str] = field(default_factory=set)  # skipped on purpose: nothing to add
    used: int = 0
    floor: int = 0  # tokens temporarily held back while placing a primary candidate


class ContextAssembler:
    def __init__(self, budget: TokenBudget, *, line_numbers: bool = True) -> None:
        self.budget = budget
        self.line_numbers = line_numbers

    # ----- public ---------------------------------------------------------- #
    def assemble(
        self,
        project: ProjectAnalysis,
        selection: Selection,
        request: ContextRequest,
        instruction_reserve: int,
    ) -> AssembledContext:
        self.project = project
        self.request = request
        self.selection = selection
        self.state = _State()
        self.files = project.by_path
        self.lines = {path: file.lines for path, file in self.files.items()}
        self.symbol_starts: dict[str, list[int]] = {}
        self.limit = max(self.budget.input_limit - instruction_reserve, 0)
        self.reserve = instruction_reserve
        self.warnings: list[str] = []

        preface = self.preface()
        base_cost = self.count(preface)
        if base_cost > self.limit:
            self.warnings.append("The token budget is too small for any context; nothing was included.")
            return self.finish("", selection, omit_everything=True)
        self.state.used = base_cost

        if not project.files:
            self.warnings.append("The archive contains no supported source files.")
            return self.finish(preface, selection)

        for candidate in selection.candidates:
            if candidate.kind == "outline":
                self.place_outline(candidate)
            elif candidate.primary:
                self.place_primary(candidate)
            else:
                self.place_code(candidate)
        # The primary region may have been capped; give it whatever the rest left over.
        for candidate in selection.candidates:
            if candidate.primary and candidate.kind == "code":
                self.place_code(candidate)
        # An outline of a file that ended up fully included as code only repeats it.
        for path in [p for p in self.state.outlines if self.status_of(p) == "full"]:
            self.remove_outline(path)

        text = self.render(preface)
        # Final authority: the whole text, counted once. Drop the lowest-priority blocks if the
        # per-block sums under-estimated (they are normally an over-estimate).
        while self.count(text) > self.limit and self.state.log:
            self.drop_last_block()
            text = self.render(preface)
        return self.finish(text, selection)

    # ----- counting ---------------------------------------------------------#
    def count(self, text: str) -> int:
        return self.budget.count(text).tokens

    @property
    def remaining(self) -> int:
        return self.limit - self.state.floor - self.state.used

    # ----- text pieces ------------------------------------------------------#
    def preface(self) -> str:
        request = self.request
        lines = [
            "CODESENSE PROJECT CONTEXT (static analysis only; no code was executed)",
            f"Intent: {request.intent}",
        ]
        if self.selection.target_path:
            target = f"Selected file: {self.selection.target_path}"
            if self.selection.target_symbols:
                target += f" | selected symbol: {', '.join(self.selection.target_symbols)}"
            lines.append(target)
        lines.append(
            "Reading guide: numbers on the left are real source line numbers. Sections marked PARTIAL "
            "or OUTLINE are not complete; do not assume anything about code that is not shown."
        )
        lines.extend(self.dependency_lines())
        lines.extend(self.code_map_lines())
        return "\n".join(lines) + "\n"

    def code_map_lines(self) -> list[str]:
        """Phase 4.5: the parser's own list of the selected file's classes, functions and imports.

        When the selected file is shown in full its outline is (rightly) dropped, so a 3B model had to infer
        which methods belong to which class from indentation, and measured on the real model it sometimes
        credited a function to the wrong class or file. This map states membership exactly, from the Phase 3
        parser, in at most :data:`CODE_MAP_MAX_TOKENS` estimated tokens. ``explain`` and ``overview`` only:
        the debug context (and so the measured debug prompt) is untouched.
        """
        path = self.selection.target_path
        if self.request.intent not in CODE_MAP_INTENTS or not path:
            return []
        analysis = self.project.analyses[path]
        file = self.files[path]
        if file.language not in ("python", "javascript"):
            return []
        symbols = [s for s in analysis.symbols if s.kind in _MAP_KINDS]
        header = f"CODE MAP of {path} ({'Python parser, exact' if analysis.confidence == 'confirmed' else 'JavaScript scanner, heuristic'}):"
        selected = set(self.selection.target_symbols)
        family = selected | {s.parent for s in symbols if s.qualified_name in selected and s.parent}
        family |= {s.qualified_name for s in symbols if s.parent in selected}

        def rank(symbol: Symbol) -> int:
            if symbol.qualified_name in selected:
                return 0
            return 1 if (symbol.qualified_name in family or symbol.parent in family) else 2

        imports = self.map_imports(path)
        budget = min(CODE_MAP_MAX_TOKENS, self.limit // 6)
        chosen: list[Symbol] = []
        for symbol in sorted(symbols, key=lambda s: (rank(s), s.start_line)):
            trial = sorted([*chosen, symbol], key=lambda s: s.start_line)
            if self.count(self.render_code_map(header, trial, len(symbols) - len(trial), imports) + "\n") > budget:
                break
            chosen = trial
        if not chosen and not imports:
            return []
        text = self.render_code_map(header, chosen, len(symbols) - len(chosen), imports)
        if self.count(text + "\n") > budget:  # even the header + imports alone do not fit: leave the map out
            text = self.render_code_map(header, chosen, len(symbols) - len(chosen), [])
            if self.count(text + "\n") > budget or not chosen:
                return []
        return text.split("\n")

    @staticmethod
    def render_code_map(header: str, symbols: list[Symbol], hidden: int, imports: list[str]) -> str:
        parts = [f"L{s.start_line}-{s.end_line} {_KIND_WORDS[s.kind]} {s.qualified_name}" for s in symbols]
        rows = [header]
        if parts:
            rows.append("  " + " | ".join(parts) + (f" | (+{hidden} more)" if hidden else ""))
        if imports:
            rows.append("  imported: " + "; ".join(imports))
        return "\n".join(rows)

    def map_imports(self, path: str) -> list[str]:
        """``name from other/file.py (function, L4-5)`` for names this file imports from another project file."""
        analysis = self.project.analyses[path]
        edges = {(e.line, e.target) for e in self.project.graph.edges if e.source == path and e.status == "resolved" and e.target}
        out: list[str] = []
        for ref in analysis.references:
            for line, target in sorted(e for e in edges if e[0] == ref.line):
                for name in ref.names:
                    symbol = next(
                        (s for s in self.project.analyses[target].symbols if s.name == name and s.parent is None and s.kind in _MAP_KINDS),
                        None,
                    )
                    if symbol:
                        entry = f"{name} from {target} ({_KIND_WORDS[symbol.kind]}, L{symbol.start_line}-{symbol.end_line})"
                        if entry not in out:
                            out.append(entry)
        return out[:CODE_MAP_MAX_IMPORTS]

    def dependency_lines(self) -> list[str]:
        project = self.project
        graph = project.graph
        out: list[str] = []
        languages: dict[str, int] = {}
        for f in project.files:
            languages[f.language] = languages.get(f.language, 0) + 1
        summary = ", ".join(f"{count} {name}" for name, count in sorted(languages.items())) or "none"
        out.append(f"Project: {len(project.files)} source files ({summary}); {len(project.excluded)} other files excluded.")
        target = self.selection.target_path

        def clip(items: list[str], limit: int = DEPENDENCY_LINE_ITEMS) -> str:
            shown = ", ".join(items[:limit])
            return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")

        def describe(edge: GraphEdge) -> str:
            where = f" (line {edge.line})"
            if edge.status == "resolved":
                return f"{edge.target}{where}"
            if edge.status == "external":
                return f"{edge.specifier} [external]"
            if edge.status == "missing":
                return f"{edge.specifier}{where} [MISSING from the project]"
            if edge.status == "excluded":
                return f"{edge.specifier}{where} [not analysed]"
            return f"{edge.specifier}{where} [unresolved]"

        if target:
            outgoing = [describe(e) for e in graph.edges if e.source == target]
            if outgoing:
                out.append(f"{target} depends on: {clip(list(dict.fromkeys(outgoing)))}")
            incoming = [f"{e.source} (line {e.line})" for e in graph.edges if e.target == target and e.status == "resolved"]
            if incoming:
                out.append(f"{target} is used by: {clip(list(dict.fromkeys(incoming)))}")
            for cycle in graph.cycles:
                if target in cycle.files:
                    out.append("Import cycle involving this file: " + " -> ".join(cycle.example))
                    break
        else:
            if graph.entry_points:
                out.append("Entry points (heuristic): " + clip([e.path for e in graph.entry_points]))
            busiest = sorted((n for n in graph.nodes if n.dependent_count), key=lambda n: (-n.dependent_count, n.path))
            if busiest:
                out.append("Most imported: " + clip([f"{n.path} ({n.dependent_count})" for n in busiest], 6))
            if graph.cycles:
                out.append("Import cycles: " + clip([" -> ".join(c.example) for c in graph.cycles], 3))
            missing = [f"{e.source}:{e.line} -> {e.specifier}" for e in graph.edges if e.status == "missing"]
            if missing:
                out.append("References to files missing from the project: " + clip(missing, 5))
        return out

    # ----- placement --------------------------------------------------------#
    def covered(self, path: str) -> list[Range]:
        return merge_ranges([(s.start, s.end) for s in self.state.segments.get(path, [])])

    def is_blank(self, path: str, start: int, end: int) -> bool:
        return all(not line.strip() for line in self.lines[path][start - 1 : end])

    def file_header_estimate(self, path: str) -> int:
        count = self.files[path].line_count
        header = f"FILE {path} [PARTIAL: lines {count}-{count}, {count}-{count} of {count}]\n"
        trailing_gap = GAP_ALLOWANCE_TOKENS
        return self.count(header) + trailing_gap + self.diagnostic_cost(path)

    def diagnostic_text(self, path: str) -> str:
        analysis = self.project.analyses[path]
        notes = [
            f"  NOTE: static analysis reports {d.message}" + (f" at line {d.line}" if d.line else "")
            for d in analysis.diagnostics
            if d.severity == "error"
        ]
        return "\n".join(notes[:2])

    def diagnostic_cost(self, path: str) -> int:
        text = self.diagnostic_text(path)
        return self.count(text + "\n") if text else 0

    def render_lines(self, path: str, start: int, end: int) -> tuple[str, int]:
        width = len(str(self.files[path].line_count))
        out = []
        clipped = 0
        for number in range(start, end + 1):
            text = self.lines[path][number - 1].rstrip()
            if len(text) > MAX_LINE_CHARS:
                text = f"{text[:MAX_LINE_CHARS]} [... +{len(text) - MAX_LINE_CHARS} characters not shown]"
                clipped += 1
            out.append(f"{number:>{width}} | {text}".rstrip() if self.line_numbers else text)
        return "\n".join(out) + "\n", clipped

    def segment_cost(self, path: str, start: int, end: int) -> int:
        """Estimated tokens to add lines ``start..end`` of ``path`` (incl. its file header if new)."""
        # Cheap guard: every character costs at least 0.15 tokens under the estimator.
        if sum(len(line) for line in self.lines[path][start - 1 : end]) * 0.15 > self.limit:
            return self.limit + 1
        text, _ = self.render_lines(path, start, end)
        cost = self.count(text) + GAP_ALLOWANCE_TOKENS
        if path not in self.state.header_cost:
            cost += self.file_header_estimate(path)
        return cost

    def add_segment(self, candidate: Candidate, start: int, end: int, cost: int) -> None:
        state = self.state
        path = candidate.path
        if path not in state.header_cost:
            header = self.file_header_estimate(path)
            state.header_cost[path] = header
            if path not in state.order:
                state.order.append(path)
        segment = _Segment(start, end, candidate.symbol, candidate.reason, cost)
        state.segments.setdefault(path, []).append(segment)
        state.log.append(_Block(path, "code", segment))
        state.used += cost

    def place_code(self, candidate: Candidate) -> None:
        wanted: Range = (candidate.start_line, candidate.end_line)
        for start, end in subtract_ranges(wanted, self.covered(candidate.path)):
            self.place_range(candidate, start, end)

    def place_primary(self, candidate: Candidate) -> None:
        """The selected file: whole if it fits comfortably, else outline first + a capped share."""
        cost = self.segment_cost(candidate.path, candidate.start_line, candidate.end_line)
        if cost <= 0.6 * self.limit - self.state.used:
            self.place_code(candidate)
            return
        self.place_outline(
            Candidate("outline", candidate.path, 0, 0, candidate.tier, "outline of the oversized selected file"),
            max_tokens=int(0.3 * self.limit),
        )
        self.state.floor = max(self.remaining // 2, 0)
        try:
            self.place_code(candidate)
        finally:
            self.state.floor = 0

    def place_range(self, candidate: Candidate, start: int, end: int) -> None:
        path = candidate.path
        if self.is_blank(path, start, end):
            return
        if self.remaining < MIN_USEFUL_TOKENS:
            return
        cost = self.segment_cost(path, start, end)
        if cost <= self.remaining:
            self.add_segment(candidate, start, end, cost)
            return
        units = self.decompose(path, start, end)
        if len(units) == 1 and units[0] == (start, end):
            # An unsplittable unit that does not fit. A leading block of lines is only worth showing
            # when it is the selected target and nothing else of this file is present yet.
            if candidate.tier == 0 and path not in self.state.segments:
                self.place_prefix(candidate, start, end)
            return
        for unit_start, unit_end in units:
            if self.remaining < MIN_USEFUL_TOKENS:
                break
            self.place_range(candidate, unit_start, unit_end)

    def place_prefix(self, candidate: Candidate, start: int, end: int) -> None:
        """A single oversized unit: include as many leading lines as fit (at least 3)."""
        path = candidate.path
        overhead = GAP_ALLOWANCE_TOKENS + (0 if path in self.state.header_cost else self.file_header_estimate(path))
        budget_left = self.remaining - overhead
        used = 0
        last = start - 1
        for number in range(start, end + 1):
            line_cost = self.count(self.render_lines(path, number, number)[0])
            if used + line_cost > budget_left:
                break
            used += line_cost
            last = number
        if last - start + 1 >= 3:
            self.add_segment(candidate, start, last, used + overhead)

    def decompose(self, path: str, start: int, end: int) -> list[Range]:
        analysis: FileAnalysis = self.project.analyses[path]
        starts = self.symbol_starts.get(path)
        if starts is None:  # symbols are sorted by start line; index them once per file
            starts = self.symbol_starts[path] = [s.start_line for s in analysis.symbols]
        low = bisect.bisect_left(starts, start)
        high = bisect.bisect_right(starts, end)
        inside = [
            s
            for s in analysis.symbols[low:high]
            if s.end_line <= end and (s.start_line, s.end_line) != (start, end)
        ]
        inside.sort(key=lambda s: (s.start_line, -s.end_line))
        top: list[Symbol] = []
        last_end = start - 1
        for symbol in inside:
            if symbol.start_line > last_end:
                top.append(symbol)
                last_end = symbol.end_line
        units: list[Range] = []
        cursor = start
        for symbol in top:
            if symbol.start_line > cursor:
                units.append((cursor, symbol.start_line - 1))
            units.append((symbol.start_line, symbol.end_line))
            cursor = symbol.end_line + 1
        if cursor <= end:
            units.append((cursor, end))
        units = [u for u in units if not self.is_blank(path, *u)]
        return units or [(start, end)]

    # ----- outlines -----------------------------------------------------------#
    def outline_entries(self, path: str) -> list[str]:
        analysis = self.project.analyses[path]
        file = self.files[path]
        entries: list[str] = []
        if file.language == "html":
            if analysis.title:
                entries.append(f"title: {analysis.title}")
            scripts = [r.specifier for r in analysis.references if r.kind == "script"]
            styles = [r.specifier for r in analysis.references if r.kind == "stylesheet"]
            if scripts:
                entries.append("scripts: " + ", ".join(scripts))
            if styles:
                entries.append("stylesheets: " + ", ".join(styles))
            if analysis.inline_script_count or analysis.inline_style_count:
                entries.append(
                    f"inline scripts: {analysis.inline_script_count}, inline styles: {analysis.inline_style_count}"
                )
            if analysis.element_ids:
                entries.append("element ids: " + ", ".join(e.id for e in analysis.element_ids[:20]))
            return entries
        for d in analysis.diagnostics:
            if d.severity == "error":
                entries.append(f"NOTE: {d.message}" + (f" at line {d.line}" if d.line else ""))
        covered = self.covered(path)
        symbols = [
            s
            for s in analysis.symbols
            if not any(a <= s.start_line and s.end_line <= b for a, b in covered)  # code already shown
        ]
        symbols = [s for s in symbols if s.kind != "variable"] + [s for s in symbols if s.kind == "variable"]
        for symbol in sorted(symbols[: OUTLINE_MAX_ENTRIES * 4], key=lambda s: (s.start_line, s.qualified_name)):
            label = symbol.signature or symbol.qualified_name
            if symbol.kind != "rule" and symbol.parent and symbol.signature:
                label = f"{symbol.signature}  [in {symbol.parent}]"
            entries.append(f"L{symbol.start_line}-{symbol.end_line} {label}")
        return entries

    def place_outline(self, candidate: Candidate, max_tokens: int | None = None) -> None:
        path = candidate.path
        state = self.state
        if path in state.outlines or path in state.redundant_outlines:
            return
        if self.remaining < MIN_USEFUL_TOKENS:
            return
        if self.status_of(path) == "full":
            state.redundant_outlines.add(path)
            return  # complete code already shown; an outline would only repeat it
        file = self.files[path]
        if file.line_count == 0:
            state.redundant_outlines.add(path)  # an empty file has nothing to outline
            return
        entries = self.outline_entries(path)
        if not entries and self.covered(path):
            state.redundant_outlines.add(path)  # every symbol is already shown as code
            return
        header = f"OUTLINE {path} ({file.language}, {file.line_count} lines) - signatures only; bodies are NOT included\n"
        if not entries:
            entries = ["(no top-level symbols detected)"]
        total = len(entries)
        entries = entries[:OUTLINE_MAX_ENTRIES]
        cap = self.remaining if max_tokens is None else min(self.remaining, max_tokens)
        header_cost = self.count(header)
        used = header_cost
        listed: list[str] = []
        more_cost = self.count(f"  (+{total} more entries not listed)\n")
        for entry in entries:
            cost = self.count(f"  {entry}\n")
            reserve = more_cost if len(listed) + 1 < total else 0
            if used + cost + reserve > cap:
                break
            used += cost
            listed.append(entry)
        if not listed:
            return
        if len(listed) < total:
            used += more_cost
        state.outlines[path] = _Outline([f"  {e}" for e in listed], len(listed), total, candidate.reason, used)
        if path not in state.order:
            state.order.append(path)
        state.log.append(_Block(path, "outline"))
        state.used += used

    # ----- rendering ------------------------------------------------------------#
    def status_of(self, path: str) -> str:
        if self.files[path].line_count == 0:
            return "full"  # an empty file has nothing that could be missing
        segments = self.state.segments.get(path)
        if segments:
            missing = [
                r
                for r in subtract_ranges((1, self.files[path].line_count), self.covered(path))
                if not self.is_blank(path, *r)
            ]
            return "partial" if missing else "full"
        return "outline_only" if path in self.state.outlines else "not_included"

    def render(self, preface: str) -> str:
        parts = [preface]
        for path in self.state.order:
            segments = sorted(self.state.segments.get(path, []), key=lambda s: (s.start, s.end))
            if segments:
                parts.append(self.render_file(path, segments))
            outline = self.state.outlines.get(path)
            if outline:
                file = self.files[path]
                parts.append(
                    f"OUTLINE {path} ({file.language}, {file.line_count} lines) - signatures only; bodies are NOT included\n"
                    + "\n".join(outline.lines)
                    + "\n"
                    + (f"  (+{outline.total - outline.listed} more entries not listed)\n" if outline.listed < outline.total else "")
                )
        return "\n".join(parts)

    def render_file(self, path: str, segments: list[_Segment]) -> str:
        count = self.files[path].line_count
        ranges = self.covered(path)
        missing = [r for r in subtract_ranges((1, count), ranges) if not self.is_blank(path, *r)]
        if missing:
            shown = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in ranges)
            noun = "line" if len(ranges) == 1 and ranges[0][0] == ranges[0][1] else "lines"
            status = f"PARTIAL: {noun} {shown} of {count}"
        else:
            status = f"FULL: all {count} lines"
        out = [f"FILE {path} [{status}]"]
        note = self.diagnostic_text(path)
        if note:
            out.append(note)
        cursor = 1
        for start, end in ranges:
            if start > cursor and not self.is_blank(path, cursor, start - 1):
                out.append(f"  [... lines {cursor}-{start - 1} not included ...]")
            text, _ = self.render_lines(path, start, end)
            out.append(text.rstrip("\n"))
            cursor = end + 1
        if cursor <= count and not self.is_blank(path, cursor, count):
            out.append(f"  [... lines {cursor}-{count} not included ...]")
        return "\n".join(out) + "\n"

    def remove_outline(self, path: str) -> None:
        state = self.state
        outline = state.outlines.pop(path)
        state.used -= outline.cost
        state.redundant_outlines.add(path)
        state.log = [b for b in state.log if not (b.kind == "outline" and b.path == path)]

    def drop_last_block(self) -> None:
        block = self.state.log.pop()
        state = self.state
        if block.kind == "code" and block.segment is not None:
            state.segments[block.path].remove(block.segment)
            state.used -= block.segment.cost
            if not state.segments[block.path]:
                del state.segments[block.path]
        else:
            outline = state.outlines.pop(block.path)
            state.used -= outline.cost
        if block.path not in state.segments and block.path not in state.outlines and block.path in state.order:
            state.order.remove(block.path)
            state.used -= state.header_cost.pop(block.path, 0)

    # ----- report ----------------------------------------------------------------#
    def finish(self, text: str, selection: Selection, *, omit_everything: bool = False) -> AssembledContext:
        state = self.state
        coverage: list[FileCoverage] = []
        reasons: dict[str, list[str]] = {}
        for candidate in selection.candidates:
            bucket = reasons.setdefault(candidate.path, [])
            if candidate.reason not in bucket and len(bucket) < 4:
                bucket.append(candidate.reason)
        for path in sorted(self.files):
            file = self.files[path]
            status = "not_included" if omit_everything else self.status_of(path)
            ranges = self.covered(path)
            missing = [r for r in subtract_ranges((1, file.line_count), ranges) if not self.is_blank(path, *r)] if file.line_count else []
            outline = state.outlines.get(path)
            tokens = sum(s.cost for s in state.segments.get(path, [])) + (outline.cost if outline else 0)
            coverage.append(
                FileCoverage(
                    path=path,
                    status=status,
                    line_count=file.line_count,
                    included_ranges=tuple(ranges[:MAX_REPORTED_RANGES]),
                    omitted_ranges=tuple(missing[:MAX_REPORTED_RANGES]) if status in ("partial",) else (),
                    outline_entries_listed=outline.listed if outline else None,
                    outline_entries_total=outline.total if outline else None,
                    reasons=tuple(reasons.get(path, ())),
                    estimated_tokens=tokens,
                )
            )

        # Everything requested that is not in the final text; dedupe, keep the first reason.
        omitted: dict[tuple, OmittedRegion] = {}
        for candidate in selection.candidates:
            if omit_everything or not text:
                break
            if candidate.kind == "outline":
                if (
                    candidate.path not in state.outlines
                    and candidate.path not in state.redundant_outlines
                    and self.status_of(candidate.path) != "full"
                ):
                    omitted.setdefault((candidate.path, "outline", None, None),
                                       OmittedRegion(candidate.path, "outline", None, None, None, "token_budget", candidate.reason))
            else:
                for start, end in subtract_ranges((candidate.start_line, candidate.end_line), self.covered(candidate.path)):
                    if not self.is_blank(candidate.path, start, end):
                        omitted.setdefault((candidate.path, "code", start, end),
                                           OmittedRegion(candidate.path, "code", start, end, candidate.symbol, "token_budget", candidate.reason))
        omitted_list = sorted(omitted.values(), key=lambda r: (r.path, r.kind, r.start_line or 0, r.end_line or 0))

        counts = {s: sum(1 for c in coverage if c.status == s) for s in ("full", "partial", "outline_only", "not_included")}
        if counts["partial"]:
            self.warnings.append(
                f"{counts['partial']} file(s) are only partially included; do not treat them as fully analysed."
            )
        if counts["outline_only"]:
            self.warnings.append(
                f"{counts['outline_only']} file(s) are included as signature outlines only (no function bodies)."
            )
        clipped = sum(
            1
            for path, segments in state.segments.items()
            for number in {n for s in segments for n in range(s.start, s.end + 1)}
            if len(self.lines[path][number - 1].rstrip()) > MAX_LINE_CHARS
        )
        if clipped:
            self.warnings.append(f"{clipped} very long line(s) were clipped (marked in the text).")
        if any(
            self.project.analyses[c.path].confidence == "heuristic" and c.status != "not_included" for c in coverage
        ):
            self.warnings.append(
                "JavaScript and CSS structure comes from a heuristic scanner, not a full parser; "
                "symbol boundaries may be imprecise."
            )
        for path in sorted(self.files):
            if self.project.analyses[path].has_syntax_error and self.status_of(path) != "not_included":
                self.warnings.append(f"{path} has a syntax error reported by the Python parser; see the NOTE in its section.")
                break

        estimated = self.count(text) if text else 0
        return AssembledContext(
            text=text,
            intent=self.request.intent,
            target_path=selection.target_path,
            target_symbols=selection.target_symbols,
            budget=BudgetReport(
                input_limit=self.budget.input_limit,
                instruction_reserve=self.reserve,
                context_limit=self.limit,
                estimated_tokens=estimated,
                remaining_tokens=self.limit - estimated,
            ),
            summary=CoverageSummary(
                files_total=len(coverage),
                files_full=counts["full"],
                files_partial=counts["partial"],
                files_outline_only=counts["outline_only"],
                files_not_included=counts["not_included"],
            ),
            coverage=tuple(coverage),
            omitted_regions=tuple(omitted_list[:MAX_REPORTED_OMISSIONS]),
            omitted_region_total=len(omitted_list),
            warnings=tuple(self.warnings),
            shown_ranges={} if omit_everything else {p: tuple(self.covered(p)) for p in sorted(state.segments)},
            outlined_paths=frozenset() if omit_everything else frozenset(state.outlines),
        )


def assemble_context(
    project: ProjectAnalysis,
    selection: Selection,
    request: ContextRequest,
    budget: TokenBudget,
    instruction_reserve: int,
    *,
    line_numbers: bool = True,
) -> AssembledContext:
    return ContextAssembler(budget, line_numbers=line_numbers).assemble(project, selection, request, instruction_reserve)
