"""``POST /api/ai/analyze``: explain or debug code from an uploaded project ZIP.

Thin on purpose. The ZIP is ingested by the Phase 3 helper (its own project slot, released before
any inference wait), then :class:`~app.services.code_analysis_service.CodeAnalysisService` does
the rest. The route accepts no model, temperature or token overrides, stores nothing and logs no
source text.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request

from app.api.projects import analyse_upload
from app.api.schemas import ErrorResponse
from app.services.analysis_models import DEFAULT_DEPTH, AnalysisResponse
from app.services.code_analysis_service import AnalysisRequest, CodeAnalysisService
from app.services.context_selection import SelectionError

PATH = "/api/ai/analyze"

router = APIRouter(prefix="/api/ai", tags=["ai"])

_ZIP_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "description": "The project folder as a ZIP archive (raw bytes, Content-Type: application/zip).",
        "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
    }
}
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (413, 415, 422, 500, 502, 503, 504)
}


@router.post(
    "/analyze",
    response_model=AnalysisResponse,
    summary="Explain or debug code from a project ZIP with the local AI",
    responses=_ERRORS,
    openapi_extra=_ZIP_BODY,
)
async def analyze(
    request: Request,
    intent: Annotated[
        Literal["overview", "explain", "debug"],
        Query(description="`overview`: what the project does. `explain`: explain a file or symbol. `debug`: look for possible bugs."),
    ] = "explain",
    depth: Annotated[
        Literal["beginner", "intermediate", "advanced"],
        Query(
            description=(
                "Explanation level. `beginner` = plain English for non-programmers (default). "
                "`intermediate` = developer. `advanced` = technical."
            )
        ),
    ] = DEFAULT_DEPTH,
    file_path: Annotated[
        str | None,
        Query(max_length=300, description="Project-relative path of the selected file (required for explain and debug)."),
    ] = None,
    symbol: Annotated[
        str | None,
        Query(max_length=200, description="Optional function/class inside `file_path`, e.g. `handler` or `Class.method`."),
    ] = None,
) -> AnalysisResponse:
    """Analyse the uploaded project with the local model and return a structured, evidence-checked answer.

    **The result is advice, not proof.** Fields are labelled AI-generated or deterministic. For `debug`,
    each finding's `verification` says whether the cited file, lines and quoted code were checked against
    the real project (`source_verified`, `hypothesis`, `unsupported`); `source_verified` never means the bug
    is confirmed. `coverage` and `limitations` come from the backend, never from the AI.

    Only one analysis runs on the model at a time; others wait briefly and then get `AI_BUSY`.
    """
    if intent != "overview" and not (file_path and file_path.strip()):
        raise SelectionError("FILE_REQUIRED", f"The '{intent}' intent needs a selected file (file_path).")
    if symbol and not (file_path and file_path.strip()):
        raise SelectionError("FILE_REQUIRED", "A symbol can only be selected together with the file that contains it.")

    service: CodeAnalysisService = request.app.state.analysis
    started = time.perf_counter()
    project = await analyse_upload(request)
    project_ms = (time.perf_counter() - started) * 1000
    return await service.analyze(
        project,
        AnalysisRequest(intent=intent, depth=depth, file_path=file_path, symbol=symbol),  # type: ignore[arg-type]
        project_ms=project_ms,
    )
