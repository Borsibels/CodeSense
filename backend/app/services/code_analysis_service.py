"""Phase 4 orchestration: project -> context -> prompt -> model -> validation -> response.

No FastAPI imports. The HTTP layer hands over an already-analysed :class:`ProjectAnalysis`
(the ZIP is ingested *before* any inference slot is taken) and an ``inference_slot`` factory;
everything that talks to the model happens inside one slot.

Request flow
------------
1. ``select_context`` + ``assemble_context`` (worker thread). The instruction reserve is sized
   from the *actual* template for this intent/depth plus the retry allowance, so the context
   gets everything the instructions do not need.
2. ``build_prompt`` (neutralised, nonce-delimited) and ``budget.ensure_fits`` on the WHOLE prompt.
   If concatenation pushed it over, the context is rebuilt once with a larger reserve; if it
   still does not fit that is a server bug (:class:`AnalysisBudgetError`, HTTP 500), never a
   silent overflow.
3. One inference slot. Standard attempt with at most one validation retry (and none if even the
   retry prompt would not fit). If the answer was cut off at the output limit, ONE compact
   attempt (smaller schema, "be brief") follows, only if time allows. At most 3 generations.
4. :class:`EvidenceValidator` checks every reference. Coverage and limitations are computed
   here from the real project; nothing the model says about them is used.

Nothing in the log contains file names, source text or model output.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from app.services.analysis_models import (
    DEFAULT_DEPTH,
    AnalysisResponse,
    ConceptOut,
    CoverageOut,
    Depth,
    ExcludedRefOut,
    GenerationOut,
    GlossaryEntry,
    LineRange,
    PartialFileOut,
    draft_model,
)
from app.services.analysis_prompts import (
    NONCE_LENGTH,
    PROMPT_VERSION,
    RETRY_ALLOWANCE_TOKENS,
    BuiltPrompt,
    MarkerCollisionError,
    build_prompt,
    looks_like_instructions,
    measure_templates,
    worst_case_retry_prompt,
)
from app.services.context_assembly import AssembledContext, assemble_context
from app.services.context_selection import INTENTS, ContextRequest, Intent, select_context
from app.services.ollama_service import OllamaGeneration
from app.services.project_models import ProjectAnalysis
from app.services.structured import StructuredGenerator, StructuredOutputTruncatedError
from app.services.token_budget import BudgetCheck, PromptTooLargeError, TokenBudget
from app.services.analysis_models import PatternCheckOut, RelationshipOut
from app.services.bug_patterns import run_pattern_checks
from app.services.evidence import EvidenceValidator
from app.services.finding_triage import debug_outcome, outcome_text, pattern_checks_out, triage_findings
from app.services.glossary import find_terms

logger = logging.getLogger(__name__)

NOTICE = (
    "AI-generated analysis: a helpful starting point, not proof. Nothing was run. A finding marked "
    "'source_verified' only means the code it points at is real and was shown to the AI; it does not "
    "prove a bug exists. Check every finding yourself."
)

# Added to every debug answer. Measured on the real model: on code with no defect it still reported a
# problem in every run, so the user must not read a finding as a fact.
DEBUG_CAUTION = (
    "A small AI model running on this computer wrote these findings. It can flag code that is actually fine and "
    "miss real problems, so treat each one as something to check yourself, not as a fact."
)
# Phase 4.5: added to every debug answer next to DEBUG_CAUTION.
TRIAGE_CAUTION = (
    "Findings are listed 'possible_problem' first, then 'worth_checking'. That order is a rough guess made to save you time, "
    "not a verdict: the second group can contain real problems, and the first can contain mistakes."
)
PATTERN_CAUTION = (
    "The automatic pattern checks only look for a few specific kinds of mistake (for example, a loop that reads one item past "
    "the end). Finding nothing with them does not mean the code is correct."
)

# Dependency-graph edges returned as ``relationships`` (the rest are counted in ``coverage``).
MAX_RELATIONSHIPS = 20
_LOAD_KINDS = frozenset({"script", "stylesheet", "css_import", "asset", "link"})
_INTERNAL_STATUSES = frozenset({"resolved", "missing", "unresolved", "excluded"})


def build_relationships(project: ProjectAnalysis, target_path: str | None) -> list[RelationshipOut]:
    """Dependency edges around the selected file (or across the project for an overview), straight from the parsers.

    Deterministic: the AI contributes nothing here, so it cannot misstate which file uses which. External
    packages and the standard library are not listed (they are not files of this upload).
    """
    graph = project.graph

    def edge_out(edge) -> RelationshipOut:
        resolved = edge.status == "resolved"
        return RelationshipOut(
            from_=edge.source,
            to=edge.target if resolved and edge.target else edge.specifier,
            kind="loads" if edge.kind in _LOAD_KINDS else "uses",
            resolved=resolved,
        )

    entry_paths = [e.path for e in graph.entry_points]
    out: list[RelationshipOut] = []
    if target_path:
        if target_path in entry_paths:
            out.append(RelationshipOut(from_=target_path, to=target_path, kind="entry_point", resolved=True))
        outgoing = sorted((e for e in graph.edges if e.source == target_path and e.status in _INTERNAL_STATUSES), key=lambda e: (e.line, e.specifier))
        incoming = sorted((e for e in graph.edges if e.target == target_path and e.status == "resolved"), key=lambda e: (e.source, e.line))
        edges = [*outgoing, *incoming]
    else:
        out.extend(RelationshipOut(from_=p, to=p, kind="entry_point", resolved=True) for p in entry_paths)
        dependents = {n.path: n.dependent_count for n in graph.nodes}
        edges = sorted(
            (e for e in graph.edges if e.status in _INTERNAL_STATUSES),
            key=lambda e: (e.status != "missing", -dependents.get(e.target or "", 0), e.source, e.line),
        )
    seen = {(r.from_, r.to, r.kind) for r in out}
    for edge in edges:
        rel = edge_out(edge)
        key = (rel.from_, rel.to, rel.kind)
        if key not in seen:
            seen.add(key)
            out.append(rel)
    return out[:MAX_RELATIONSHIPS]

# At most one validation retry in the standard attempt (the generator's own default); the compact
# fallback never retries. => at most 3 model calls per request.
MAX_STANDARD_RETRIES = 1
# Start the compact fallback only if the standard attempt(s) left at least this much headroom
# under the 120 s per-call read timeout.
COMPACT_FALLBACK_DEADLINE_SECONDS = 150.0
# A larger reserve for the one rebuild after a concatenation overshoot.
RESERVE_BUMP_TOKENS = 64
# Smallest context (estimated tokens) worth analysing; the app refuses to start below this.
MIN_CONTEXT_TOKENS = 1000
MAX_LISTED_FILES = 50
_SLACK_TOKENS = 16

InferenceSlot = Callable[[], AbstractAsyncContextManager[None]]
GenerationHook = Callable[[int, OllamaGeneration], None]


@dataclass(frozen=True)
class AnalysisRequest:
    intent: Intent
    depth: Depth = DEFAULT_DEPTH
    file_path: str | None = None
    symbol: str | None = None


class NoAnalyzableSourceError(Exception):
    """There is nothing the model could be shown (no supported source, or nothing fit)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AnalysisUnavailableError(Exception):
    """The server's token budget is too small for code analysis (a configuration problem)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AnalysisBudgetError(RuntimeError):
    """The assembled prompt exceeds the token budget even after one rebuild: a server-side bug."""


def _nonce() -> str:
    return secrets.token_hex(NONCE_LENGTH // 2)


def check_instruction_budget(budget: TokenBudget, reserve: int) -> tuple[dict[tuple[str, str, str], int], str | None]:
    """Measure every template and say whether enough room is left for source code.

    Returns ``(sizes, problem)``: the estimated token size of each ``(intent, depth, mode)``
    template, and ``None`` or an actionable message when the biggest template plus the retry
    allowance would leave fewer than :data:`MIN_CONTEXT_TOKENS` for code. The message is NOT raised
    here: an unusually small ``AI_MAX_INPUT_TOKENS`` must not stop the other (Phase 1-3) endpoints
    from working, so only ``/api/ai/analyze`` refuses, explicitly, with this message.
    """
    sizes = {(m.intent, m.depth, m.mode): m.tokens for m in measure_templates(budget, "0" * NONCE_LENGTH)}
    worst = max(sizes.values())
    room = budget.input_limit - max(reserve, worst + RETRY_ALLOWANCE_TOKENS + _SLACK_TOKENS)
    if room < MIN_CONTEXT_TOKENS:
        return sizes, (
            f"AI code analysis needs about {worst + RETRY_ALLOWANCE_TOKENS} estimated tokens for its instructions, "
            f"which leaves {max(room, 0)} of the {budget.input_limit}-token input budget for source code "
            f"(minimum {MIN_CONTEXT_TOKENS}). Raise AI_MAX_INPUT_TOKENS (or the model context window) to use it."
        )
    return sizes, None


class CodeAnalysisService:
    def __init__(
        self,
        structured: StructuredGenerator,
        budget: TokenBudget,
        *,
        reserve: int,
        inference_slot: InferenceSlot,
        nonce_factory: Callable[[], str] = _nonce,
        clock: Callable[[], float] = time.perf_counter,
        on_generation: GenerationHook | None = None,
        compact_fallback: bool = True,
        fallback_deadline_seconds: float = COMPACT_FALLBACK_DEADLINE_SECONDS,
    ) -> None:
        self.structured = structured
        self.budget = budget
        self.reserve = reserve
        self.inference_slot = inference_slot
        self.nonce_factory = nonce_factory
        self.clock = clock
        self.on_generation = on_generation
        self.compact_fallback = compact_fallback
        self.fallback_deadline_seconds = fallback_deadline_seconds

        self._template_tokens, self.unavailable_reason = check_instruction_budget(budget, reserve)

    # ----- budget ------------------------------------------------------------------------ #
    def reserve_for(self, intent: str, depth: str) -> int:
        """Instruction reserve for a request: its real template, the retry sentence and a little slack."""
        needed = self._template_tokens[intent, depth, "standard"] + RETRY_ALLOWANCE_TOKENS + _SLACK_TOKENS
        return max(self.reserve, needed)

    def _build_context(self, project: ProjectAnalysis, request: ContextRequest, reserve: int) -> AssembledContext:
        selection = select_context(project, request)
        return assemble_context(project, selection, request, self.budget, reserve)

    def _prompt(self, request: AnalysisRequest, mode: str, context_text: str) -> BuiltPrompt:
        for _ in range(3):
            try:
                return build_prompt(request.intent, request.depth, mode, context_text, self.nonce_factory())
            except MarkerCollisionError:
                continue
        raise AnalysisBudgetError("could not choose a source delimiter that the source text does not contain")

    # ----- main entry point --------------------------------------------------------------- #
    async def analyze(
        self, project: ProjectAnalysis, request: AnalysisRequest, *, project_ms: float | None = None
    ) -> AnalysisResponse:
        started = self.clock()
        if self.unavailable_reason:
            raise AnalysisUnavailableError(self.unavailable_reason)
        if request.intent not in INTENTS:
            raise ValueError(f"unsupported intent {request.intent!r}")

        # 1. Context. The request was validated by select_context (SelectionError -> HTTP 422).
        context_request = ContextRequest(intent=request.intent, path=request.file_path, symbol=request.symbol)
        reserve = self.reserve_for(request.intent, request.depth)
        context = await asyncio.to_thread(self._build_context, project, context_request, reserve)
        if not (context.summary.files_full + context.summary.files_partial + context.summary.files_outline_only):
            if not project.files:
                raise NoAnalyzableSourceError("The archive contains no supported source files to analyse.")
            raise NoAnalyzableSourceError("None of the project's code fit into the AI's input window.")

        # 2. Prompt, checked as a whole.
        prompt = self._prompt(request, "standard", context.text)
        try:
            check = self.budget.ensure_fits(prompt.text)
        except PromptTooLargeError:
            logger.warning("Prompt overshot the budget after concatenation; rebuilding with a larger reserve")
            context = await asyncio.to_thread(
                self._build_context, project, context_request, reserve + RESERVE_BUMP_TOKENS
            )
            prompt = self._prompt(request, "standard", context.text)
            try:
                check = self.budget.ensure_fits(prompt.text)
            except PromptTooLargeError as exc:
                raise AnalysisBudgetError(f"analysis prompt exceeds the input budget: {exc}") from exc
        context_ms = (self.clock() - started) * 1000

        # A retry resends the prompt plus one sentence; only allow it if that provably fits.
        retries = min(self.structured.max_retries, MAX_STANDARD_RETRIES)
        if retries and not self.budget.check(worst_case_retry_prompt(prompt.text)).fits:
            logger.info("Retry disabled for this request: the retry prompt would not fit the budget")
            retries = 0

        # 3. Inference: everything under ONE slot.
        inference_started = self.clock()
        mode = "standard"
        attempts = 0
        async with self.inference_slot():
            generation_started = self.clock()
            try:
                result = await self.structured.generate(
                    prompt.text, draft_model(request.intent, request.depth), max_retries=retries
                )
                attempts += result.attempts
            except StructuredOutputTruncatedError as exc:
                attempts += exc.attempts
                elapsed = self.clock() - generation_started
                if not self.compact_fallback or elapsed > self.fallback_deadline_seconds:
                    raise
                logger.warning("Output truncated after %d attempt(s); trying one compact answer", attempts)
                compact = self._prompt(request, "compact", context.text)
                self.budget.ensure_fits(compact.text)
                mode = "compact"
                result = await self.structured.generate(
                    compact.text, draft_model(request.intent, request.depth, compact=True), max_retries=0
                )
                attempts += result.attempts
        inference_ms = (self.clock() - inference_started) * 1000
        if self.on_generation is not None:
            self.on_generation(check.tokens, result.generation)

        # 4. Evidence validation (pure, outside the slot).
        validation_started = self.clock()
        validator = EvidenceValidator(project, context)
        draft: Any = result.value
        debug_outcome_value = None
        pattern_checks: list[PatternCheckOut] = []
        relationships: list[RelationshipOut] = []
        summary: str = draft.summary
        if request.intent == "debug":
            findings = validator.validate_findings(draft.findings)
            explanations = []
            # Deterministic Phase 4.5 layers. The model's draft is never changed; findings are only ordered and labelled.
            hits = await asyncio.to_thread(run_pattern_checks, project, context.target_path, self._target_ranges(project, context)) \
                if context.target_path else []
            triage = triage_findings(project, findings, hits)
            findings = triage.findings
            pattern_checks = pattern_checks_out(project, hits, triage.corroborated_by, context, request.depth)
            debug_outcome_value = debug_outcome(findings, hits)
            # The AI's own debug summary ("found 2 issues") contradicted its tiered findings and measurably added nothing.
            summary = outcome_text(debug_outcome_value, findings, pattern_checks, context, project)
        else:
            explanations = validator.validate_sections(draft.sections)
            findings = []
            relationships = build_relationships(project, context.target_path)
        concept = None
        concept_name = getattr(draft, "concept_name", "")
        if concept_name and getattr(draft, "concept_explanation", ""):
            concept = ConceptOut(name=concept_name, explanation=draft.concept_explanation)
        glossary: list[GlossaryEntry] = []
        if request.depth == "beginner":
            # AI text, plus the rule explanations (CodeSense's own wording, but still read by the beginner).
            texts = [draft.summary if request.intent != "debug" else "", getattr(draft, "analogy", ""),
                     getattr(draft, "role_in_app", ""), concept_name, getattr(draft, "concept_explanation", "")]
            texts += [f"{e.title} {e.description}" for e in explanations]
            texts += [f"{f.title} {f.problem} {f.what_could_happen} {f.likely_cause} {f.suggestion}" for f in findings]
            texts += [c.explanation for c in pattern_checks]
            texts += list(getattr(draft, "assumptions", []))
            glossary = [GlossaryEntry(term=term, meaning=meaning) for term, meaning in find_terms(texts)]
        validation_ms = (self.clock() - validation_started) * 1000
        total_ms = (self.clock() - started) * 1000 + (project_ms or 0.0)

        counts = {v: sum(1 for f in findings if f.verification == v) for v in ("source_verified", "hypothesis", "unsupported")}
        logger.info(
            "Analysis done: intent=%s depth=%s mode=%s attempts=%d prompt_tokens~%d context_tokens~%d "
            "findings=%d verified=%d hypothesis=%d unsupported=%d sections=%d inference_ms=%.0f total_ms=%.0f",
            request.intent, request.depth, mode, attempts, check.tokens, context.budget.estimated_tokens,
            len(findings), counts["source_verified"], counts["hypothesis"], counts["unsupported"],
            len(explanations), inference_ms, total_ms,
        )

        timings = {
            "context": round(context_ms, 1),
            "inference": round(inference_ms, 1),
            "validation": round(validation_ms, 1),
            "total": round(total_ms, 1),
        }
        if project_ms is not None:
            timings = {"project": round(project_ms, 1), **timings}
        return AnalysisResponse(
            notice=NOTICE,
            intent=request.intent,
            depth=request.depth,
            target_file=context.target_path,
            target_symbols=list(context.target_symbols),
            summary=summary,
            debug_outcome=debug_outcome_value,
            pattern_checks=pattern_checks,
            relationships=relationships,
            analogy=(getattr(draft, "analogy", "") or None),
            explanations=explanations,
            role_in_app=(getattr(draft, "role_in_app", "") or None),
            concept_to_learn=concept,
            findings=findings,
            assumptions=[a for a in getattr(draft, "assumptions", []) if a.strip()],
            glossary=glossary,
            limitations=self._limitations(project, context, prompt, mode, request.intent),
            coverage=self._coverage(project, context, check),
            generation=GenerationOut(
                model=self.structured.ollama.settings.model,
                prompt_version=PROMPT_VERSION,
                attempts=attempts,
                mode=mode,  # type: ignore[arg-type]
                prompt_eval_count=result.generation.prompt_eval_count,
                eval_count=result.generation.eval_count,
            ),
            timings_ms=timings,
        )

    # ----- deterministic metadata ---------------------------------------------------------- #
    @staticmethod
    def _target_ranges(project: ProjectAnalysis, context: AssembledContext) -> list[tuple[int, int]] | None:
        """Line ranges of the selected symbol(s), so pattern checks stay inside them; None = the whole file."""
        if not context.target_path or not context.target_symbols:
            return None
        wanted = set(context.target_symbols)
        ranges = [(s.start_line, s.end_line) for s in project.analyses[context.target_path].symbols if s.qualified_name in wanted]
        return ranges or None

    @staticmethod
    def _coverage(project: ProjectAnalysis, context: AssembledContext, check: BudgetCheck) -> CoverageOut:
        by_status: dict[str, list] = {"full": [], "partial": [], "outline_only": [], "not_included": []}
        for item in context.coverage:
            by_status[item.status].append(item)
        not_included = [c.path for c in by_status["not_included"]]
        excluded = [ExcludedRefOut(path=e.path, reason=e.reason) for e in project.excluded]
        return CoverageOut(
            files_total=context.summary.files_total,
            full_files=[c.path for c in by_status["full"]],
            partial_files=[
                PartialFileOut(
                    path=c.path,
                    included_ranges=[LineRange(start_line=a, end_line=b) for a, b in c.included_ranges],
                    omitted_ranges=[LineRange(start_line=a, end_line=b) for a, b in c.omitted_ranges],
                )
                for c in by_status["partial"]
            ],
            outline_only_files=[c.path for c in by_status["outline_only"]],
            not_included_files=not_included[:MAX_LISTED_FILES],
            not_included_total=len(not_included),
            excluded_files=excluded[:MAX_LISTED_FILES],
            excluded_total=len(excluded),
            context_estimated_tokens=context.budget.estimated_tokens,
            instruction_estimated_tokens=max(check.tokens - context.budget.estimated_tokens, 0),
            prompt_estimated_tokens=check.tokens,
            input_limit=check.limit,
        )

    @staticmethod
    def _limitations(
        project: ProjectAnalysis, context: AssembledContext, prompt: BuiltPrompt, mode: str, intent: str
    ) -> list[str]:
        out: list[str] = []
        if intent == "debug":
            out.append(DEBUG_CAUTION)
            out.append(TRIAGE_CAUTION)
            out.append(PATTERN_CAUTION)
        partial = [c.path for c in context.coverage if c.status == "partial"]
        outline = [c.path for c in context.coverage if c.status == "outline_only"]
        skipped = [c.path for c in context.coverage if c.status == "not_included"]

        def names(paths: list[str]) -> str:
            shown = ", ".join(paths[:5])
            return shown + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")

        if partial:
            out.append(
                f"Only part of {len(partial)} file(s) was shown to the AI ({names(partial)}); code outside the shown "
                "lines was not analysed."
            )
        if outline:
            out.append(
                f"{len(outline)} file(s) were shown only as a list of signatures, without their code ({names(outline)})."
            )
        if skipped:
            out.append(f"{len(skipped)} source file(s) were not shown to the AI at all ({names(skipped)}).")
        if project.excluded:
            out.append(f"{len(project.excluded)} file(s) in the archive were skipped (unsupported type, too large or ignored).")
        for warning in context.warnings:
            if "only partially included" in warning or "signature outlines only" in warning:
                continue  # said above, with file names
            out.append(warning)
        if prompt.neutralized_sequences:
            out.append(
                f"{prompt.neutralized_sequences} special chat-control sequence(s) in the source were defused before the AI saw them."
            )
        if looks_like_instructions(context.text):
            out.append(
                "The source contains text that looks like instructions to an AI. It was treated as code, not as "
                "instructions, but read this analysis with extra care."
            )
        if mode == "compact":
            out.append("The AI's first answer was cut off at its length limit, so a shorter answer was requested; some detail may be missing.")
        return out
