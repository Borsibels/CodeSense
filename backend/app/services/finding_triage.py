"""Presentation triage for debug findings, and the deterministic debug outcome and summary.

Why this exists
---------------
``evidence.py`` proves one thing: the cited file and lines are real and were shown to the model. It
deliberately does NOT judge whether the AI's claim is true, and measured on the real model it cannot:
on code with no defect the model produced findings in every run, and every one that quoted a real line
came back ``source_verified``. Mixing plausibility into the evidence validator would blur that guarantee,
so this separate module sorts findings for PRESENTATION, after validation.

What a tier is (and is not)
---------------------------
* ``possible_problem``: shown first. The citation was verified, the cited lines are not an example block,
  and the claim is not about inputs the code was never shown receiving; OR an independent deterministic
  rule fired on the same lines.
* ``worth_checking``: everything else. Demoted, NEVER hidden: a genuine missing input check is a real kind
  of bug, so a demoted finding can still be right, and its reason codes say exactly why it was demoted.

A tier is a prioritisation heuristic, not a verdict. Tiers never remove or edit a finding, and the model's
call count, prompt and recall are untouched: the debug prompt is byte-for-byte the one that was measured.

The outcome ``no_clear_problem`` therefore means "nothing reached the top tier in the code examined", and
the summary says in plain words that this is not proof of correct code.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.services.analysis_models import (
    DebugOutcome,
    FindingOut,
    PatternCheckOut,
    Tier,
    TierReason,
)
from app.services.bug_patterns import PatternHit
from app.services.context_assembly import MAX_LINE_CHARS, AssembledContext
from app.services.project_models import ProjectAnalysis
from app.services.python_analysis import _is_main_guard, _walk_top_blocks

Range = tuple[int, int]
MAX_PATTERN_EXCERPT_LINES = 15
PROBLEM_TEXT_CHARS = 90  # how much of ``problem`` is read for the input-assumption test

# Human wording for each reason code (documentation, tests and the README; the API returns the codes).
TIER_REASON_TEXT: dict[str, str] = {
    "QUOTE_NOT_MATCHED": "The place is real, but the code the AI quoted does not match it, so the AI's description of that code may be wrong.",
    "LOCATION_NOT_VERIFIED": "The AI pointed at a place that does not exist or that it was not shown, so there is no code to check this against.",
    "IN_DEMO_CODE": "The lines are an example block that only runs when the file is run directly; findings there are rarely about the real logic.",
    "INPUT_ASSUMPTION": "The claim is about input the code was never shown receiving (empty, missing or unexpected values), which cannot be judged from the code alone.",
    "CORROBORATED_BY_RULE": "An independent automatic rule flagged the same lines, which makes this more credible than the AI's say-so alone.",
}


# --------------------------------------------------------------------------- #
# Demo code (Python ``if __name__ == "__main__":`` blocks, from the AST)
# --------------------------------------------------------------------------- #
def demo_ranges(project: ProjectAnalysis, path: str) -> list[Range]:
    """Line ranges of module-level ``if __name__ == "__main__":`` blocks in a Python file."""
    file = project.by_path.get(path)
    if file is None or file.language != "python" or project.analyses[path].has_syntax_error:
        return []
    try:
        tree = ast.parse(file.text)
        return [(node.lineno, node.end_lineno or node.lineno) for node in _walk_top_blocks(tree.body) if _is_main_guard(node)]
    except (SyntaxError, RecursionError, ValueError, MemoryError):
        return []


# --------------------------------------------------------------------------- #
# "The claim is about unseen input"
# --------------------------------------------------------------------------- #
# Categories of claim, not words from any fixture. All of them are statements whose truth depends on a value
# the code was never shown receiving, so they cannot be confirmed from the code alone:
#   1. absent / empty values        empty, blank, null, undefined, NaN, None
#   2. validation talk              validation, sanitise, input/error handling
#   3. unusual input                invalid / unexpected / malformed / negative / non-numeric  input|value|...
#   4. a missing check              missing / no / lack of  check|handling|validation|guard
#   5. edge cases                   edge case, corner case, boundary
#   6. division by zero             depends on the divisor's value
# (Case-insensitive except ``None``, which is the Python value; the English "none" is ordinary prose.)
_INPUT_WORDS = re.compile(
    r"""\b(?:
          empty | blank | null | undefined | nan
        | (?:un)?validat\w* | (?:un)?sanitiz\w* | (?:un)?sanitis\w*
        | (?:error|input|exception)\s+handling
        | (?:invalid|unexpected|unusual|malformed|improper|illegal|negative|non-numeric|non-integer|incorrect\s+type|wrong\s+type)
              \s+(?:\w+\s+){0,2}?(?:input|value|argument|parameter|data|type|format|number|quantit\w+|price)s?
        | (?:missing|lack\s+of|lacks|lacking|without|no|insufficient|absent)\s+(?:\w+\s+){0,3}?(?:checks?|checking|handling|validation|guards?|verification)
        | (?:missing|absent)\s+(?:input|value|argument|parameter|data|field|key|element|item)s?
        | edge[\s-]cases? | corner[\s-]cases? | boundary\s+(?:case|condition|check)s?
        | divi(?:sion|de|ded|ding)\s+by\s+(?:zero|0) | by\s+zero | zerodivisionerror
        | unhandled\s+(?:input|value|case)s?
        )\b""",
    re.IGNORECASE | re.VERBOSE,
)
_NONE_VALUE = re.compile(r"\bNone\b(?!\s+of\b)")  # case-sensitive on purpose; "None of the branches" is just prose


def is_input_assumption(title: str, problem: str) -> bool:
    """Is the finding a claim about inputs the code was never shown receiving?

    Reads the title and the first :data:`PROBLEM_TEXT_CHARS` characters of the problem text: that is where a
    3B model states WHAT it thinks is wrong; the rest is hedging and elaboration.
    """
    text = f"{title}. {problem[:PROBLEM_TEXT_CHARS]}"
    return bool(_INPUT_WORDS.search(text) or _NONE_VALUE.search(text))


# --------------------------------------------------------------------------- #
# Tiering
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TriageResult:
    findings: list[FindingOut]  # ordered possible_problem first, ids renumbered in display order
    corroborated_by: dict[int, list[str]]  # index into the ``hits`` passed in -> ids of the findings it corroborates


def triage_findings(project: ProjectAnalysis, findings: Sequence[FindingOut], hits: Sequence[PatternHit]) -> TriageResult:
    demos: dict[str, list[Range]] = {}

    def demo(path: str) -> list[Range]:
        if path not in demos:
            demos[path] = demo_ranges(project, path)
        return demos[path]

    staged: list[tuple[FindingOut, list[TierReason], Tier, list[int]]] = []
    for finding in findings:
        reasons: list[TierReason] = []
        if finding.verification == "unsupported":
            reasons.append("LOCATION_NOT_VERIFIED")
        elif finding.verification == "hypothesis":
            reasons.append("QUOTE_NOT_MATCHED")
        located = bool(finding.file_path and finding.start_line and finding.end_line)
        if located and any(a <= finding.start_line and finding.end_line <= b for a, b in demo(finding.file_path)):  # type: ignore[arg-type]
            reasons.append("IN_DEMO_CODE")
        if is_input_assumption(finding.title, finding.problem):
            reasons.append("INPUT_ASSUMPTION")
        matching = (
            [
                i
                for i, hit in enumerate(hits)
                if hit.path == finding.file_path and any(finding.start_line <= n <= finding.end_line for n in hit.focus_lines)  # type: ignore[operator]
            ]
            if located
            else []
        )
        if matching:
            reasons.append("CORROBORATED_BY_RULE")
        tier: Tier = "possible_problem" if (matching or not reasons) else "worth_checking"
        staged.append((finding, reasons, tier, matching))

    ordered = [s for s in staged if s[2] == "possible_problem"] + [s for s in staged if s[2] == "worth_checking"]
    result: list[FindingOut] = []
    corroborated: dict[int, list[str]] = {}
    for number, (finding, reasons, tier, matching) in enumerate(ordered, start=1):
        new_id = f"F{number}"
        result.append(finding.model_copy(update={"id": new_id, "tier": tier, "tier_reasons": list(reasons)}))
        for index in matching:
            corroborated.setdefault(index, []).append(new_id)
    return TriageResult(findings=result, corroborated_by=corroborated)


# --------------------------------------------------------------------------- #
# Outcome
# --------------------------------------------------------------------------- #
def problem_hits(hits: Sequence[PatternHit]) -> list[PatternHit]:
    return [h for h in hits if h.strength == "problem_if_assumptions_hold"]


def debug_outcome(findings: Sequence[FindingOut], hits: Sequence[PatternHit]) -> DebugOutcome:
    """``possible_problems`` only if a top-tier finding or a problem-strength rule hit exists."""
    if any(f.tier == "possible_problem" for f in findings) or problem_hits(hits):
        return "possible_problems"
    return "no_clear_problem"


# --------------------------------------------------------------------------- #
# Pattern checks as the API returns them
# --------------------------------------------------------------------------- #
def pattern_checks_out(
    project: ProjectAnalysis,
    hits: Sequence[PatternHit],
    corroborated_by: dict[int, list[str]],
    context: AssembledContext,
    depth: str,
) -> list[PatternCheckOut]:
    ordered = sorted(enumerate(hits), key=lambda item: (item[1].strength != "problem_if_assumptions_hold", item[1].start_line, item[1].rule))
    out: list[PatternCheckOut] = []
    for number, (index, hit) in enumerate(ordered, start=1):
        lines = project.by_path[hit.path].lines
        last = min(hit.end_line, hit.start_line + MAX_PATTERN_EXCERPT_LINES - 1)
        rendered = []
        for line in lines[hit.start_line - 1 : last]:
            line = line.rstrip()
            rendered.append(line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " [...]")
        shown = context.shown_ranges.get(hit.path, ())
        out.append(
            PatternCheckOut(
                id=f"P{number}",
                rule=hit.rule,
                strength=hit.strength,
                title=hit.title,
                explanation=hit.explanation(depth),
                assumptions=list(hit.assumptions),
                parser=hit.parser,
                file_path=hit.path,
                start_line=hit.start_line,
                end_line=hit.end_line,
                source_excerpt="\n".join(rendered),
                excerpt_start_line=hit.start_line,
                shown_to_ai=all(any(a <= n <= b for a, b in shown) for n in hit.focus_lines),
                corroborates=corroborated_by.get(index, []),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Summary text (deterministic; never says or implies "no bugs")
# --------------------------------------------------------------------------- #
def _span_text(ranges: Sequence[Range]) -> str:
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in ranges)


def examined_description(context: AssembledContext, project: ProjectAnalysis) -> str:
    """What the AI was actually shown of the selected file, in words (from the real coverage report)."""
    path = context.target_path
    if path is None:
        return "the project overview"
    cover = next((c for c in context.coverage if c.path == path), None)
    scope = f"{', '.join(context.target_symbols)} in {path}" if context.target_symbols else path
    if cover is None or cover.status == "not_included":
        return f"none of {path}: it did not fit the AI's window"
    if cover.status == "outline_only":
        return f"only the signatures of {path}, not its code"
    if cover.status == "partial":
        ranges = cover.included_ranges
        shown = _span_text(ranges)
        return f"{scope}, lines {shown} of {cover.line_count} (the rest was not shown)"
    if context.target_symbols:
        return scope
    count = cover.line_count
    return f"all {count} line{'s' if count != 1 else ''} of {path}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def outcome_text(
    outcome: DebugOutcome,
    findings: Sequence[FindingOut],
    pattern_checks: Sequence[PatternCheckOut],
    context: AssembledContext,
    project: ProjectAnalysis,
) -> str:
    """The debug ``summary``: outcome + coverage + honest caveats, built only from deterministic facts."""
    examined = examined_description(context, project)
    top = [f for f in findings if f.tier == "possible_problem"]
    corroborating = {c.id for c in pattern_checks if c.corroborates}  # already counted as the finding they corroborate
    rule_problems = [c for c in pattern_checks if c.strength == "problem_if_assumptions_hold" and c.id not in corroborating]
    notes = [f for f in findings if f.tier == "worth_checking"] + [
        c for c in pattern_checks if c.strength == "worth_checking" and c.id not in corroborating
    ]
    not_shown = context.summary.files_not_included
    unseen = (
        f" {_plural(not_shown, 'other source file was', 'other source files were')} not shown to the AI at all."
        if not_shown
        else ""
    )
    note_text = (
        f" {_plural(len(notes), 'note', 'notes')} worth a second look {'is' if len(notes) == 1 else 'are'} listed below, after the main list."
        if notes
        else ""
    )
    if outcome == "no_clear_problem":
        return (
            f"No clear problem was identified in the code examined ({examined}). That is not proof the code is correct: it has not been "
            "proven bug-free. A small AI model reviewed it, nothing was run, and only a few kinds of mistakes are checked automatically."
            f"{unseen}{note_text}"
        ).strip()
    count = len(top) + len(rule_problems)
    unseen_hits = [c for c in pattern_checks if c.strength == "problem_if_assumptions_hold" and not c.shown_to_ai]
    outside = (
        f" {_plural(len(unseen_hits), 'of them is', 'of them are')} in lines the AI was not shown, found by the automatic checks alone."
        if unseen_hits
        else ""
    )
    return (
        f"{_plural(count, 'possible problem', 'possible problems')} found in the code examined ({examined}), most likely first. "
        "These are suspicions, not confirmed bugs: a small AI model and a few automatic checks produced them and nothing was run, so "
        f"check each one yourself.{outside}{unseen}{note_text}"
    ).strip()
