"""Schemas for Phase 4 AI analysis: what the model must produce, and what the API returns.

Two families, kept apart on purpose
-----------------------------------
* **Draft models** (``*Draft``) are sent to Ollama as the ``format`` JSON schema and validate
  the model's reply. They are small and shallow because the model is a 3B network with a
  1024-token output limit. "No location" is ``""`` / ``0`` instead of ``null`` (``anyOf``
  grammars are unreliable on small models).
* **Public models** (``AnalysisResponse`` and friends) are what the HTTP client receives. Every
  field says in its description whether it is *AI-generated* or *deterministic* (computed by
  the backend from the real project). Nothing the model says about coverage, file lists or
  verification is ever copied through: those fields are built in Python.

Strict vs. clipped
------------------
* Structural fields (enums, line numbers, list sizes) are strict: a violation is rejected and
  the structured generator retries.
* AI **prose** is clipped with an ellipsis instead of rejected: a rejection costs a full
  15-40 s regeneration for no gain. Ollama's grammar already enforces ``maxLength``, which
  cuts text *mid-word* at exactly the cap, so a string that reaches the cap is treated as cut
  off and trimmed back to a word boundary.
* A quoted code excerpt (``evidence``) is never clipped: a clipped quote is not the model's
  quote. Over-long evidence is simply treated as absent.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.services.context_selection import Intent

Depth = Literal["beginner", "intermediate", "advanced"]
DEPTHS: tuple[str, ...] = ("beginner", "intermediate", "advanced")
DEFAULT_DEPTH: Depth = "beginner"

Level = Literal["low", "medium", "high"]
Category = Literal[
    "logic",
    "condition",
    "off_by_one",
    "variable_usage",
    "edge_case",
    "null_access",
    "api_misuse",
    "type_assumption",
    "integration",
    "other",
]
Verification = Literal["source_verified", "hypothesis", "unsupported"]
LocationStatus = Literal["in_context", "outline_only", "file_only", "none", "rejected"]
EvidenceIssue = Literal[
    "NO_LOCATION",
    "FILE_NOT_IN_PROJECT",
    "FILE_EXCLUDED",
    "LINES_OUT_OF_RANGE",
    "LINES_NOT_IN_CONTEXT",
    "LINES_OUTLINE_ONLY",
    "EXCERPT_MISMATCH",
    "NO_EXCERPT",
]

# ---- Phase 4.5 (presentation only; none of these says a bug is real) ------------------------------ #
Tier = Literal["possible_problem", "worth_checking"]
TierReason = Literal[
    "QUOTE_NOT_MATCHED",  # demoting: the location is real but the AI's quoted code does not match it
    "LOCATION_NOT_VERIFIED",  # demoting: the AI's location is invalid, so there is no evidence to look at
    "IN_DEMO_CODE",  # demoting: the cited lines are an example block that only runs when the file is run directly
    "INPUT_ASSUMPTION",  # demoting: the claim is about inputs the code was never shown receiving
    "CORROBORATED_BY_RULE",  # promoting: an independent deterministic rule fired on the same lines
]
DebugOutcome = Literal["no_clear_problem", "possible_problems"]
PatternStrength = Literal["problem_if_assumptions_hold", "worth_checking"]
RelationshipKind = Literal["uses", "loads", "entry_point"]

MAX_FINDINGS = 3
MAX_SECTIONS = 4
MAX_EVIDENCE_CHARS = 140
MAX_LINE_NUMBER = 1_000_000
MAX_PATH_CHARS = 300

ELLIPSIS = "…"
CUT_MARGIN = 4  # see clip_prose


# --------------------------------------------------------------------------- #
# Field types
# --------------------------------------------------------------------------- #
def clip_prose(value: Any, limit: int) -> Any:
    """Collapse whitespace; if the text reached ``limit`` it was cut off, so trim and add ``…``.

    Non-strings are returned unchanged so that Pydantic rejects them (structure stays strict).
    The result is never longer than ``limit``.
    """
    if not isinstance(value, str):
        return value
    text = " ".join(value.split())
    if len(text) < limit:
        # Measured: the output grammar's cut can land a character or two under the cap (after
        # whitespace is collapsed), mid-sentence. Text that nearly fills the cap without a
        # sentence ending was almost certainly cut off, so say so.
        if len(text) >= limit - CUT_MARGIN and text[-1] not in ".!?)`\"'":
            return text.rstrip(" ,;:-") + ELLIPSIS
        return text
    cut = text[: limit - 1]
    boundary = cut.rfind(" ")  # back up to a word boundary unless that discards most of the text
    if boundary >= limit // 2:
        cut = cut[:boundary]
    cut = cut.rstrip(" ,;:-")
    return cut + ELLIPSIS


def Prose(limit: int):  # noqa: N802 - used like a type in annotations
    """AI-written text: whitespace-normalised and clipped with an ellipsis at ``limit`` characters."""
    return Annotated[str, BeforeValidator(partial(clip_prose, limit=limit)), Field(max_length=limit)]


def _drop_overlong_quote(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= MAX_EVIDENCE_CHARS:
        # Never clip a quote: a clipped quote is not what the model quoted. A quote that reaches the
        # cap was cut off by the output grammar (a runaway copy), so it counts as absent.
        return ""
    return value


Quote = Annotated[str, BeforeValidator(_drop_overlong_quote), Field(max_length=MAX_EVIDENCE_CHARS)]
Line = Annotated[int, Field(ge=0, le=MAX_LINE_NUMBER)]
ModelPath = Annotated[str, Field(max_length=MAX_PATH_CHARS)]


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Draft models: standard size
# --------------------------------------------------------------------------- #
class SectionDraft(_Draft):
    """One part of an explanation. Location first: it anchors a small model on real code."""

    file_path: ModelPath  # "" = not about one place
    start_line: Line  # 0 = no location
    end_line: Line
    title: Prose(70)  # type: ignore[valid-type]
    description: Prose(300)  # type: ignore[valid-type]


class BeginnerExplainDraft(_Draft):
    """Plain-English explanation (depth ``beginner``). Field order = generation order."""

    summary: Prose(400)  # type: ignore[valid-type]
    analogy: Prose(220)  # type: ignore[valid-type]  # "" when no honest analogy exists
    sections: list[SectionDraft] = Field(max_length=MAX_SECTIONS)
    role_in_app: Prose(240)  # type: ignore[valid-type]
    concept_name: Prose(60)  # type: ignore[valid-type]
    concept_explanation: Prose(240)  # type: ignore[valid-type]
    assumptions: list[Prose(120)] = Field(max_length=2)  # type: ignore[valid-type]


class TechnicalExplainDraft(_Draft):
    """Developer / technical explanation (depths ``intermediate`` and ``advanced``)."""

    summary: Prose(450)  # type: ignore[valid-type]
    sections: list[SectionDraft] = Field(max_length=MAX_SECTIONS)
    assumptions: list[Prose(120)] = Field(max_length=2)  # type: ignore[valid-type]


class FindingDraft(_Draft):
    """A possible problem. Location and quote come BEFORE the prose that depends on them."""

    file_path: ModelPath
    evidence: Quote  # one line copied as shown, WITH its margin number; "" if none
    start_line: Line
    end_line: Line
    title: Prose(70)  # type: ignore[valid-type]
    category: Category
    severity: Level
    confidence: Level
    problem: Prose(200)  # type: ignore[valid-type]  # what may be wrong, plain words
    what_could_happen: Prose(160)  # type: ignore[valid-type]
    likely_cause: Prose(160)  # type: ignore[valid-type]
    suggestion: Prose(160)  # type: ignore[valid-type]


class DebugDraft(_Draft):
    summary: Prose(280)  # type: ignore[valid-type]
    findings: list[FindingDraft] = Field(max_length=MAX_FINDINGS)


# --------------------------------------------------------------------------- #
# Draft models: compact fallback (used once, after an output-truncation)
# --------------------------------------------------------------------------- #
class CompactSectionDraft(_Draft):
    file_path: ModelPath
    start_line: Line
    end_line: Line
    title: Prose(50)  # type: ignore[valid-type]
    description: Prose(160)  # type: ignore[valid-type]


class CompactBeginnerExplainDraft(_Draft):
    summary: Prose(240)  # type: ignore[valid-type]
    sections: list[CompactSectionDraft] = Field(max_length=2)
    role_in_app: Prose(140)  # type: ignore[valid-type]
    concept_name: Prose(50)  # type: ignore[valid-type]
    concept_explanation: Prose(140)  # type: ignore[valid-type]


class CompactTechnicalExplainDraft(_Draft):
    summary: Prose(260)  # type: ignore[valid-type]
    sections: list[CompactSectionDraft] = Field(max_length=2)


class CompactFindingDraft(_Draft):
    file_path: ModelPath
    evidence: Quote
    start_line: Line
    end_line: Line
    title: Prose(60)  # type: ignore[valid-type]
    category: Category
    severity: Level
    confidence: Level
    problem: Prose(130)  # type: ignore[valid-type]
    what_could_happen: Prose(110)  # type: ignore[valid-type]
    likely_cause: Prose(110)  # type: ignore[valid-type]
    suggestion: Prose(110)  # type: ignore[valid-type]


class CompactDebugDraft(_Draft):
    summary: Prose(160)  # type: ignore[valid-type]
    findings: list[CompactFindingDraft] = Field(max_length=1)


ExplainDraft = BeginnerExplainDraft | TechnicalExplainDraft | CompactBeginnerExplainDraft | CompactTechnicalExplainDraft
AnyDraft = ExplainDraft | DebugDraft | CompactDebugDraft


def draft_model(intent: str, depth: str, *, compact: bool = False) -> type[BaseModel]:
    """The schema the model must satisfy for this request."""
    if intent == "debug":
        return CompactDebugDraft if compact else DebugDraft
    if depth == "beginner":
        return CompactBeginnerExplainDraft if compact else BeginnerExplainDraft
    return CompactTechnicalExplainDraft if compact else TechnicalExplainDraft


# --------------------------------------------------------------------------- #
# Public response
# --------------------------------------------------------------------------- #
class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineRange(_Out):
    start_line: int
    end_line: int


class ExplanationOut(_Out):
    title: str = Field(description="AI-generated: short heading of this step or part.")
    description: str = Field(description="AI-generated explanation of this step or part.")
    file_path: str | None = Field(None, description="Validated against the project; null if the AI gave none or an invalid one.")
    start_line: int | None = Field(None, description="First line, validated against what the AI was shown; null otherwise.")
    end_line: int | None = Field(None, description="Last line, validated against what the AI was shown; null otherwise.")
    location_status: LocationStatus = Field(
        description=(
            "Deterministic. `in_context`: the lines exist and were shown to the AI. `outline_only`: the range matches a "
            "symbol the AI saw only as a signature outline. `file_only`: a real file the AI was shown, no lines. "
            "`none`: no location was given. `rejected`: a location was given but is invalid (fields are null)."
        )
    )
    location_issue: EvidenceIssue | None = Field(None, description="Deterministic. Why a location was rejected.")


class ConceptOut(_Out):
    name: str = Field(description="AI-generated: the programming concept's name.")
    explanation: str = Field(description="AI-generated: what the concept means, using this code as the example.")


class GlossaryEntry(_Out):
    term: str
    meaning: str


class EvidenceOut(_Out):
    source_excerpt: str = Field(
        description=(
            "Deterministic: lines copied by the BACKEND from the uploaded file (never the AI's own quote), "
            "at most 15 lines starting at `excerpt_start_line`."
        )
    )
    excerpt_start_line: int
    excerpt_end_line: int
    excerpt_matched: bool = Field(
        description="Deterministic: whether the code the AI quoted really appears in the cited lines."
    )


class FindingOut(_Out):
    id: str = Field(description="Deterministic: F1, F2, ... assigned after validation and triage, in display order.")
    title: str = Field(description="AI-generated.")
    category: Category = Field(description="AI-generated.")
    problem: str = Field(description="AI-generated: what may be wrong, in the requested depth's language. A suspicion, not a proven bug.")
    what_could_happen: str = Field(description="AI-generated: the effect if the problem is real.")
    likely_cause: str = Field(description="AI-generated: why it may happen.")
    suggestion: str = Field(description="AI-generated: a direction for fixing it.")
    severity: Level = Field(description="AI-generated qualitative rating of impact if the problem is real.")
    confidence: Level = Field(
        description=(
            "AI-generated qualitative rating, NOT a probability. Forced to `low` for `unsupported` findings."
        )
    )
    verification: Verification = Field(
        description=(
            "Deterministic. `source_verified`: the cited file and lines exist, were shown to the AI and the quoted code "
            "matches - this does NOT prove the bug is real. `hypothesis`: the location is valid but the quote is "
            "missing or does not match. `unsupported`: the location is invalid; it is removed."
        )
    )
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    evidence: EvidenceOut | None = None
    evidence_issue: EvidenceIssue | None = Field(None, description="Deterministic. Why the finding is not `source_verified`.")
    tier: Tier = Field(
        "worth_checking",
        description=(
            "Deterministic presentation heuristic, NOT a correctness guarantee. `possible_problem`: shown first; the cited code "
            "was verified and nothing marked it as demo code or as a claim about unseen input, or an independent rule "
            "corroborated it. `worth_checking`: a note, shown after the possible problems; it is demoted, never hidden, and may "
            "still be a real bug (for example a genuine missing input check)."
        ),
    )
    tier_reasons: list[TierReason] = Field(
        default_factory=list,
        description=(
            "Deterministic. Why the tier was chosen. Demoting reasons: QUOTE_NOT_MATCHED, LOCATION_NOT_VERIFIED, IN_DEMO_CODE, "
            "INPUT_ASSUMPTION. Promoting: CORROBORATED_BY_RULE. May be listed even when the finding stayed a possible problem "
            "(corroboration overrides the demoting reasons)."
        ),
    )


class PatternCheckOut(_Out):
    """A deterministic rule hit (``bug_patterns.py``). Not AI-generated, and not proof of a bug."""

    id: str = Field(description="Deterministic: P1, P2, ...")
    rule: str = Field(description="Deterministic: stable rule identifier, e.g. PY_INDEX_PAST_END.")
    strength: PatternStrength = Field(
        description=(
            "Deterministic. `problem_if_assumptions_hold`: this is a mistake PROVIDED every assumption listed below is true. "
            "`worth_checking`: suspicious, but it depends on what the author intended."
        )
    )
    title: str
    explanation: str = Field(description="Deterministic, written for the requested depth.")
    assumptions: list[str] = Field(description="Deterministic: what must be true for this to be a real problem. Always shown with the hit.")
    parser: Literal["confirmed", "heuristic"] = Field(
        description="`confirmed`: found with Python's own parser. `heuristic`: found with Sift's lightweight JavaScript scanner."
    )
    file_path: str
    start_line: int
    end_line: int
    source_excerpt: str = Field(description="Deterministic: lines copied by the backend from the uploaded file (at most 15).")
    excerpt_start_line: int
    shown_to_ai: bool = Field(description="Deterministic: whether these lines were part of what the AI was shown.")
    corroborates: list[str] = Field(default_factory=list, description="Deterministic: ids of AI findings on the same lines.")


class RelationshipOut(BaseModel):
    """A dependency-graph edge (mirrors the frontend's ``Relationship``). Deterministic, from the parsers: never the AI."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from", description="The file that refers to `to`.")
    to: str = Field(description="The project file it refers to, or the unresolved reference as written.")
    kind: RelationshipKind = Field(
        description=(
            "`uses`: imports or requires. `loads`: a script, stylesheet or asset reference. `entry_point`: marks `from` as a "
            "heuristic entry point (`to` equals `from`). A file's users are the edges whose `to` is that file."
        )
    )
    resolved: bool = Field(description="False when the reference does not match a file in the upload.")


class PartialFileOut(_Out):
    path: str
    included_ranges: list[LineRange]
    omitted_ranges: list[LineRange]


class ExcludedRefOut(_Out):
    path: str
    reason: str


class CoverageOut(_Out):
    """Deterministic. Built from the real project and the real prompt; the AI contributes nothing here."""

    files_total: int
    full_files: list[str] = Field(description="Files whose whole content the AI was shown.")
    partial_files: list[PartialFileOut]
    outline_only_files: list[str] = Field(description="Files the AI saw only as signatures.")
    not_included_files: list[str] = Field(description="Files the AI did not see (list is bounded; see the total).")
    not_included_total: int
    excluded_files: list[ExcludedRefOut] = Field(description="Archive files that were not analysed (list is bounded).")
    excluded_total: int
    context_estimated_tokens: int
    instruction_estimated_tokens: int
    prompt_estimated_tokens: int
    input_limit: int
    is_estimate: Literal[True] = True


class GenerationOut(_Out):
    model: str
    prompt_version: str
    attempts: int = Field(description="Model calls made for this request (the first try, retries and the compact fallback).")
    mode: Literal["standard", "compact"] = Field(
        description="`compact` means the first answer was cut off at the output limit, so a shorter one was requested."
    )
    prompt_eval_count: int | None = None
    eval_count: int | None = None


class AnalysisResponse(_Out):
    status: Literal["completed"] = "completed"
    notice: str = Field(description="Deterministic reminder that AI output is advice, not proof.")
    intent: Intent
    depth: Depth
    target_file: str | None = None
    target_symbols: list[str] = []
    summary: str = Field(
        description=(
            "`explain`/`overview`: AI-generated overview in the requested depth's language. `debug`: DETERMINISTIC (built from "
            "`debug_outcome` and `coverage`); the AI's own debug summary is discarded because it contradicted its findings."
        )
    )
    debug_outcome: DebugOutcome | None = Field(
        None,
        description=(
            "Deterministic, `debug` only. `no_clear_problem`: no finding reached the `possible_problem` tier and no rule found a "
            "problem in the code examined. This is NOT proof that the code is bug-free. `possible_problems`: at least one "
            "`possible_problem` finding or one `problem_if_assumptions_hold` pattern check exists."
        ),
    )
    pattern_checks: list[PatternCheckOut] = Field(
        default_factory=list,
        description="Deterministic, `debug` only: hits from a few hand-written rules, each with its assumptions. Not AI output.",
    )
    relationships: list[RelationshipOut] = Field(
        default_factory=list,
        description="Deterministic, `explain`/`overview` only: dependency edges around the selected file, from the project's parsers.",
    )
    analogy: str | None = Field(None, description="AI-generated, `beginner` explain/overview only: an everyday comparison. Optional and imperfect.")
    explanations: list[ExplanationOut] = Field(description="AI-generated, in order (step by step). Empty for `debug`.")
    role_in_app: str | None = Field(None, description="AI-generated, `beginner` explain/overview only.")
    concept_to_learn: ConceptOut | None = Field(None, description="AI-generated, `beginner` explain/overview only.")
    findings: list[FindingOut] = Field(description="AI-generated suspicions with validated locations. Only for `debug`.")
    assumptions: list[str] = Field(description="AI-generated: things the AI says it could not confirm.")
    glossary: list[GlossaryEntry] = Field(
        default_factory=list,
        description=(
            "Deterministic, `beginner` depth only: plain-language meanings of common programming words found in the "
            "AI's text. Written by Sift, not by the AI, and bounded to a few entries."
        ),
    )
    limitations: list[str] = Field(description="Deterministic: what this analysis could not see or check.")
    coverage: CoverageOut
    generation: GenerationOut
    timings_ms: dict[str, float] = Field(description="Deterministic: project, context, inference, validation, total.")
