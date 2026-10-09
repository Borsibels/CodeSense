"""Project analysis routes: ``POST /api/projects/inspect`` and ``POST /api/projects/context``.

The request body is the raw ZIP archive (``Content-Type: application/zip``), not
JSON or multipart: browsers can ``fetch(url, {method: "POST", body: file})`` it
directly, no multipart parser is needed, and the middleware can enforce the size
limit while streaming. Selection options travel in the query string.

Nothing is stored: the archive is analysed in a worker thread, the response is
built from derived data, and everything is garbage once the request ends.
Neither route needs, calls or waits for Ollama.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request

from app.api.errors import ApiError
from app.api.project_schemas import (
    BudgetOut,
    ContextResponse,
    CoverageSummaryOut,
    DiagnosticCounts,
    ExcludedFileOut,
    FileCoverageOut,
    GraphOut,
    InspectResponse,
    LineRangeOut,
    OmittedRegionOut,
    ProjectSummary,
    SourceFileOut,
)
from app.api.schemas import ErrorResponse
from app.config import ProjectSettings
from app.services import TokenBudget
from app.services.context_assembly import AssembledContext, assemble_context
from app.services.context_selection import ContextRequest, select_context
from app.services.project_models import ProjectAnalysis
from app.services.project_pipeline import analyze_project

logger = logging.getLogger(__name__)

# Smallest context (estimated tokens) worth building; a bigger reserve is refused as a client error.
MIN_CONTEXT_TOKENS = 200
PREFIX = "/api/projects"
UPLOAD_PATHS = (f"{PREFIX}/inspect", f"{PREFIX}/context")
# Content types accepted for the archive body. The bytes are validated as a ZIP regardless.
ZIP_CONTENT_TYPES = frozenset({"application/zip", "application/x-zip-compressed", "application/octet-stream"})

NOTICE = (
    "Deterministic static analysis of the uploaded files. Nothing was executed, so this is not a "
    "substitute for running or fully understanding the project. Items marked 'heuristic' come from "
    "a lightweight scanner and may be incomplete."
)

router = APIRouter(prefix=PREFIX, tags=["projects"])

_ZIP_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "description": "The project folder as a ZIP archive (raw bytes, Content-Type: application/zip).",
        "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
    }
}
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (413, 415, 422, 503, 504)
}


class ProjectLimiter:
    """Caps simultaneous project analyses (each holds up to ~35 MiB in memory)."""

    def __init__(self, max_concurrent: int, queue_wait_timeout: float) -> None:
        self.queue_wait_timeout = queue_wait_timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if not self._semaphore.locked():
            await self._semaphore.acquire()
        else:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), self.queue_wait_timeout)
            except asyncio.TimeoutError:
                raise ApiError(
                    503,
                    "PROJECT_BUSY",
                    "Another project is being analysed. Wait a moment and try again.",
                    headers={"Retry-After": "5"},
                ) from None
        try:
            yield
        finally:
            self._semaphore.release()


async def analyse_upload(request: Request) -> ProjectAnalysis:
    settings: ProjectSettings = request.app.state.project_settings
    limiter: ProjectLimiter = request.app.state.project_limiter
    # The middleware already enforces the size cap (declared and streamed).
    data = await request.body()
    async with limiter.slot():
        # CPU-bound and bounded: keep it off the event loop. The pipeline enforces its own deadline.
        return await asyncio.to_thread(analyze_project, data, settings)


def _build_context(
    project: ProjectAnalysis, request: ContextRequest, budget: TokenBudget, reserve: int
) -> AssembledContext:
    selection = select_context(project, request)
    return assemble_context(project, selection, request, budget, reserve)


def _summary(project: ProjectAnalysis) -> ProjectSummary:
    languages: dict[str, int] = {}
    counts = {"error": 0, "warning": 0, "info": 0}
    symbols = references = 0
    for file in project.files:
        languages[file.language] = languages.get(file.language, 0) + 1
        analysis = project.analyses[file.path]
        symbols += len(analysis.symbols)
        references += len(analysis.references)
        for diagnostic in analysis.diagnostics:
            counts[diagnostic.severity] += 1
    return ProjectSummary(
        archive_bytes=project.stats.archive_bytes,
        entry_count=project.stats.entry_count,
        uncompressed_bytes=project.stats.uncompressed_bytes,
        source_files=len(project.files),
        source_bytes=project.stats.source_bytes,
        excluded_files=len(project.excluded),
        languages=dict(sorted(languages.items())),
        symbol_count=symbols,
        reference_count=references,
        diagnostics=DiagnosticCounts(**counts),
    )


def _source_file(project: ProjectAnalysis, path: str) -> SourceFileOut:
    file = project.by_path[path]
    analysis = project.analyses[path]
    return SourceFileOut(
        path=path,
        language=file.language,
        size_bytes=file.size_bytes,
        line_count=file.line_count,
        parser=analysis.parser,
        confidence=analysis.confidence,
        has_syntax_error=analysis.has_syntax_error,
        doc=analysis.doc,
        has_main_guard=analysis.has_main_guard,
        title=analysis.title,
        inline_script_count=analysis.inline_script_count,
        inline_style_count=analysis.inline_style_count,
        element_ids=analysis.element_ids,  # type: ignore[arg-type]
        css_classes=list(analysis.css_classes),
        css_ids=list(analysis.css_ids),
        symbols=analysis.symbols,  # type: ignore[arg-type]
        references=analysis.references,  # type: ignore[arg-type]
        exports=analysis.exports,  # type: ignore[arg-type]
        diagnostics=analysis.diagnostics,  # type: ignore[arg-type]
    )


def _ranges(ranges: tuple[tuple[int, int], ...]) -> list[LineRangeOut]:
    return [LineRangeOut(start_line=a, end_line=b) for a, b in ranges]


def _context_response(project: ProjectAnalysis, result: AssembledContext) -> ContextResponse:
    return ContextResponse(
        notice=NOTICE,
        intent=result.intent,  # type: ignore[arg-type]
        target_file=result.target_path,
        target_symbols=list(result.target_symbols),
        context=result.text,
        budget=BudgetOut(**vars(result.budget)),
        coverage_summary=CoverageSummaryOut(**vars(result.summary)),
        files=[
            FileCoverageOut(
                path=c.path,
                status=c.status,  # type: ignore[arg-type]
                line_count=c.line_count,
                included_ranges=_ranges(c.included_ranges),
                omitted_ranges=_ranges(c.omitted_ranges),
                outline_entries_listed=c.outline_entries_listed,
                outline_entries_total=c.outline_entries_total,
                reasons=list(c.reasons),
                estimated_tokens=c.estimated_tokens,
            )
            for c in result.coverage
        ],
        omitted_regions=[OmittedRegionOut(**vars(r)) for r in result.omitted_regions],
        omitted_region_total=result.omitted_region_total,
        warnings=list(result.warnings),
        project=_summary(project),
    )


@router.post(
    "/inspect",
    response_model=InspectResponse,
    summary="Inspect a project ZIP: files, symbols, diagnostics and dependency graph",
    responses=_ERRORS,
    openapi_extra=_ZIP_BODY,
)
async def inspect_project(request: Request) -> InspectResponse:
    """Validate the archive, discover supported source files (`.py .js .jsx .mjs .cjs .html .htm .css`),
    run static analysis and map dependencies. Deterministic: the same archive gives the same response.

    Every archive file is accounted for: either under `files` or under `excluded` with a reason.
    Rejected archives answer 4xx with a stable error code (see the README).
    """
    project = await analyse_upload(request)
    return InspectResponse(
        notice=NOTICE,
        summary=_summary(project),
        files=[_source_file(project, f.path) for f in project.files],
        excluded=[ExcludedFileOut.model_validate(e) for e in project.excluded],
        graph=GraphOut.model_validate(project.graph),
    )


@router.post(
    "/context",
    response_model=ContextResponse,
    summary="Build a token-budgeted, AI-ready context from a project ZIP",
    responses=_ERRORS,
    openapi_extra=_ZIP_BODY,
)
async def project_context(
    request: Request,
    file: Annotated[
        str | None,
        Query(max_length=300, description="Project-relative path of the selected file (required for explain/debug)"),
    ] = None,
    symbol: Annotated[
        str | None,
        Query(max_length=200, description="Optional symbol inside `file`, e.g. `handler` or `Class.method`"),
    ] = None,
    intent: Annotated[Literal["overview", "explain", "debug"], Query(description="What the context is for")] = "overview",
    instruction_reserve_tokens: Annotated[
        int | None,
        Query(ge=0, description="Tokens to keep free for future instructions (default from server settings)"),
    ] = None,
) -> ContextResponse:
    """Build a context for the model that stays inside the input token budget.

    The model is **not** called. The response reports exactly which files and line
    ranges made it into `context`, which were only outlined, and what was omitted.
    """
    settings: ProjectSettings = request.app.state.project_settings
    budget: TokenBudget = request.app.state.budget
    reserve = settings.instruction_reserve_tokens if instruction_reserve_tokens is None else instruction_reserve_tokens
    if reserve > budget.input_limit - MIN_CONTEXT_TOKENS:
        raise ApiError(
            422,
            "INVALID_REQUEST",
            f"instruction_reserve_tokens ({reserve}) leaves fewer than {MIN_CONTEXT_TOKENS} tokens for context; "
            f"the model's input budget is {budget.input_limit} estimated tokens.",
        )

    project = await analyse_upload(request)
    context_request = ContextRequest(intent=intent, path=file, symbol=symbol)
    # SelectionError (unknown file/symbol) becomes a 422 through the registered error handlers.
    result = await asyncio.to_thread(_build_context, project, context_request, budget, reserve)
    logger.info(
        "Built %s context: %d/%d estimated tokens, %d omitted regions",
        intent,
        result.budget.estimated_tokens,
        result.budget.context_limit,
        result.omitted_region_total,
    )
    return _context_response(project, result)
