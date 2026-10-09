"""Deterministic validation of the model's source references.

The model is a 3B network: it can cite a file that does not exist, lines that are out of range,
code it was never shown, or a "quote" it made up. Every reference is therefore checked against
the real project and against the exact context that was put in the prompt. Pure and synchronous:
no I/O, no model calls.

What each outcome means
-----------------------
For a **finding**:

=================  ==========================================================================
``source_verified``  file exists, lines are in range, the lines were *shown to the model*, and
                     the quoted code really appears in them. This proves the *citation* is
                     real. It does NOT prove the bug is real.
``hypothesis``       location is valid and was shown, but the quote is missing, trivial or does
                     not match. A plausible concern that needs a human look.
``unsupported``      the location itself is invalid (unknown/excluded file, bad lines, lines
                     the model never saw). The location is removed (never replaced by a
                     "corrected" one), confidence is forced to ``low``.
=================  ==========================================================================

Rules that never bend
---------------------
* Line numbers are never clamped, shifted or searched for ("fixed"): wrong stays wrong and is
  reported as wrong.
* The excerpt returned to the client is copied from the real file by the backend; the model's
  own quote is only ever compared, never returned.
* A line that exists in the project but was not part of the prompt is *not* inspected evidence
  (``LINES_NOT_IN_CONTEXT``); a file the model saw only as a signature outline is
  ``LINES_OUTLINE_ONLY``; an excluded file is ``FILE_EXCLUDED``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.services.analysis_models import (
    MAX_FINDINGS,
    MAX_SECTIONS,
    CompactFindingDraft,
    CompactSectionDraft,
    EvidenceIssue,
    EvidenceOut,
    ExplanationOut,
    FindingDraft,
    FindingOut,
    LocationStatus,
    SectionDraft,
)
from app.services.analysis_prompts import neutralize_control_tokens
from app.services.context_assembly import MAX_LINE_CHARS, AssembledContext, subtract_ranges
from app.services.context_selection import normalize_request_path
from app.services.project_models import ProjectAnalysis

MAX_EXCERPT_LINES = 15
# A quote must carry at least this many letters/digits, so ")" or "{" can never count as evidence.
MIN_QUOTE_ALNUM = 8

_LINE_PREFIX = re.compile(r"^\s*\d+\s*\|\s?")
_FENCE = re.compile(r"^\s*```\w*\s*$")
_WS = re.compile(r"\s+")

Kind = Literal["explanation", "finding"]


@dataclass(frozen=True)
class LocationCheck:
    status: LocationStatus
    issue: EvidenceIssue | None = None
    path: str | None = None
    start: int | None = None
    end: int | None = None


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


class EvidenceValidator:
    def __init__(self, project: ProjectAnalysis, context: AssembledContext) -> None:
        self.project = project
        self.context = context
        self._excluded = {e.path for e in project.excluded}

    # ----- locations ------------------------------------------------------------------ #
    def check_location(self, raw_path: str, start: int, end: int, *, kind: Kind) -> LocationCheck:
        text = raw_path.strip()
        if not text:
            if start == 0 and end == 0:
                return LocationCheck("none", "NO_LOCATION" if kind == "finding" else None)
            return LocationCheck("rejected", "FILE_NOT_IN_PROJECT")  # line numbers with no file
        path = normalize_request_path(text)
        files = self.project.by_path
        if path not in files:
            return LocationCheck("rejected", "FILE_EXCLUDED" if path in self._excluded else "FILE_NOT_IN_PROJECT")
        shown = self.context.shown_ranges.get(path, ())
        outlined = path in self.context.outlined_paths

        if start == 0 and end == 0:
            if kind == "finding":
                return LocationCheck("rejected", "NO_LOCATION")
            if shown or outlined:
                return LocationCheck("file_only", None, path)
            return LocationCheck("rejected", "LINES_NOT_IN_CONTEXT")

        file = files[path]
        if not 1 <= start <= end <= file.line_count:
            return LocationCheck("rejected", "LINES_OUT_OF_RANGE")

        lines = file.lines
        missing = [
            r
            for r in subtract_ranges((start, end), list(shown))
            if any(line.strip() for line in lines[r[0] - 1 : r[1]])
        ]
        if not missing:
            return LocationCheck("in_context", None, path, start, end)

        touches_shown = any(a <= end and start <= b for a, b in shown)
        if outlined and not touches_shown:
            if kind == "explanation" and self._inside_symbol(path, start, end):
                return LocationCheck("outline_only", None, path, start, end)
            return LocationCheck("rejected", "LINES_OUTLINE_ONLY")
        return LocationCheck("rejected", "LINES_NOT_IN_CONTEXT")

    def _inside_symbol(self, path: str, start: int, end: int) -> bool:
        return any(s.start_line <= start and end <= s.end_line for s in self.project.analyses[path].symbols)

    # ----- quotes ----------------------------------------------------------------------- #
    @staticmethod
    def _clean_quote(quote: str) -> str:
        kept = []
        for line in quote.splitlines():
            if _FENCE.match(line):
                continue
            kept.append(_LINE_PREFIX.sub("", line))
        text, _ = neutralize_control_tokens(" ".join(kept))
        return _collapse(text).strip("`")

    def _cited_text(self, path: str, start: int, end: int) -> str:
        """The cited lines exactly as the model saw them (clipped at MAX_LINE_CHARS, neutralised)."""
        lines = self.project.by_path[path].lines[start - 1 : end]
        shown = [neutralize_control_tokens(line.rstrip()[:MAX_LINE_CHARS])[0] for line in lines]
        return _collapse(" ".join(shown))

    def _quote_check(self, path: str, start: int, end: int, quote: str) -> tuple[bool, EvidenceIssue | None]:
        cleaned = self._clean_quote(quote)
        if sum(ch.isalnum() for ch in cleaned) < MIN_QUOTE_ALNUM:
            return False, "NO_EXCERPT"
        if cleaned in self._cited_text(path, start, end):
            return True, None
        return False, "EXCERPT_MISMATCH"

    def _excerpt(self, path: str, start: int, end: int, matched: bool) -> EvidenceOut:
        last = min(end, start + MAX_EXCERPT_LINES - 1)
        rendered = []
        for line in self.project.by_path[path].lines[start - 1 : last]:
            line = line.rstrip()
            rendered.append(line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " [...]")
        return EvidenceOut(
            source_excerpt="\n".join(rendered),
            excerpt_start_line=start,
            excerpt_end_line=last,
            excerpt_matched=matched,
        )

    # ----- explanation sections ----------------------------------------------------------- #
    def validate_sections(self, sections: Sequence[SectionDraft | CompactSectionDraft]) -> list[ExplanationOut]:
        out: list[ExplanationOut] = []
        useful = [s for s in sections if s.title.strip() or s.description.strip()]
        for section in useful[:MAX_SECTIONS]:
            check = self.check_location(section.file_path, section.start_line, section.end_line, kind="explanation")
            keeps_path = check.status in ("in_context", "outline_only", "file_only")
            keeps_lines = check.status in ("in_context", "outline_only")
            out.append(
                ExplanationOut(
                    title=section.title,
                    description=section.description,
                    file_path=check.path if keeps_path else None,
                    start_line=check.start if keeps_lines else None,
                    end_line=check.end if keeps_lines else None,
                    location_status=check.status,
                    location_issue=check.issue,
                )
            )
        return out

    # ----- findings ----------------------------------------------------------------------- #
    def validate_findings(self, findings: Sequence[FindingDraft | CompactFindingDraft]) -> list[FindingOut]:
        out: list[FindingOut] = []
        seen: set[tuple] = set()
        for draft in findings[:MAX_FINDINGS]:
            check = self.check_location(draft.file_path, draft.start_line, draft.end_line, kind="finding")
            located = check.status == "in_context"
            evidence: EvidenceOut | None = None
            issue = check.issue
            confidence = "low"
            if located:
                assert check.path is not None and check.start is not None and check.end is not None
                matched, issue = self._quote_check(check.path, check.start, check.end, draft.evidence)
                evidence = self._excerpt(check.path, check.start, check.end, matched)
                verification = "source_verified" if matched else "hypothesis"
                # An unmatched quote means the model's own account of the code is unreliable, so it
                # may not claim high confidence.
                confidence = draft.confidence if matched or draft.confidence != "high" else "medium"
                key: tuple = (check.path, check.start, check.end, draft.category)
            else:
                verification = "unsupported"
                issue = issue or "NO_LOCATION"
                key = ("unlocated", " ".join(draft.title.lower().split()))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                FindingOut(
                    id=f"F{len(out) + 1}",
                    title=draft.title,
                    category=draft.category,
                    problem=draft.problem,
                    what_could_happen=draft.what_could_happen,
                    likely_cause=draft.likely_cause,
                    suggestion=draft.suggestion,
                    severity=draft.severity,
                    confidence=confidence,  # type: ignore[arg-type]
                    verification=verification,
                    file_path=check.path if located else None,
                    start_line=check.start if located else None,
                    end_line=check.end if located else None,
                    evidence=evidence,
                    evidence_issue=issue,
                )
            )
        return out
