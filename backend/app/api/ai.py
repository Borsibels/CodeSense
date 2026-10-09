"""AI routes: readiness diagnostics and a plain text-generation endpoint."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from app.api.errors import ApiError
from app.api.inference import audit_token_usage
from app.api.limiter import InferenceLimiter
from app.api.schemas import (
    ErrorResponse,
    GenerateRequest,
    GenerateResponse,
    HealthResponse,
    ModelStatus,
    OllamaStatus,
)
from app.config import ApiSettings
from app.services import OllamaError, OllamaService, TokenBudget

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse}
    for code in (422, 500, 502, 503, 504)
}


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="AI readiness: API, Ollama and model status",
)
async def ai_health(request: Request) -> HealthResponse:
    """Diagnostic endpoint; **always HTTP 200** when the API itself is running.

    Readiness is reported in the body (``status``), not the HTTP status:

    * ``ready``: Ollama is reachable and the model is installed.
    * ``degraded``: Ollama is reachable but the model is missing
      (``model.available == false``) or could not be checked
      (``model.available == null``).
    * ``unavailable``: Ollama is not reachable.

    Never runs inference and never waits for the inference slot.
    """
    ollama: OllamaService = request.app.state.ollama
    model_name = ollama.settings.model

    health = await ollama.health_check()
    ollama_status = OllamaStatus(
        healthy=health.healthy,
        version=health.version,
        detail=None if health.healthy else health.detail,
    )

    if not health.healthy:
        return HealthResponse(
            status="unavailable",
            ollama=ollama_status,
            model=ModelStatus(
                name=model_name,
                available=None,
                detail="Not checked because Ollama is unreachable.",
            ),
        )

    try:
        available = await ollama.model_available()
    except Exception as exc:  # noqa: BLE001 - a diagnostic endpoint must report, not fail
        logger.warning(
            "Model availability check failed: %s",
            exc,
            exc_info=None if isinstance(exc, OllamaError) else exc,
        )
        detail = exc.user_message if isinstance(exc, OllamaError) else (
            "Could not determine whether the model is installed. Check the server log."
        )
        return HealthResponse(
            status="degraded",
            ollama=ollama_status,
            model=ModelStatus(name=model_name, available=None, detail=detail),
        )

    if available:
        return HealthResponse(
            status="ready",
            ollama=ollama_status,
            model=ModelStatus(name=model_name, available=True, detail=None),
        )
    return HealthResponse(
        status="degraded",
        ollama=ollama_status,
        model=ModelStatus(
            name=model_name,
            available=False,
            detail=f"Model '{model_name}' is not installed. Install it with: ollama pull {model_name}",
        ),
    )


@router.post(
    "/generate",
    response_model=GenerateResponse,
    summary="Generate text with the local model (integration test interface)",
    responses=_ERROR_RESPONSES,
)
async def ai_generate(body: GenerateRequest, request: Request) -> GenerateResponse:
    """Send ``prompt`` to the configured local model and return its answer.

    The prompt must pass two size checks, both answered with HTTP 422:
    ``PROMPT_TOO_LONG`` (character limit) and ``PROMPT_TOO_MANY_TOKENS`` (an
    *estimated* token count against the context-window budget).

    ``status`` is ``completed``, or ``truncated`` when the model hit its token
    limit and ``response`` is only a partial answer. Only one generation runs
    at a time by default; others queue briefly, then receive ``AI_BUSY``.
    """
    ollama: OllamaService = request.app.state.ollama
    limiter: InferenceLimiter = request.app.state.limiter
    api_settings: ApiSettings = request.app.state.api_settings
    budget: TokenBudget = request.app.state.budget

    if len(body.prompt) > api_settings.max_prompt_chars:
        raise ApiError(
            422,
            "PROMPT_TOO_LONG",
            f"The prompt is too long ({len(body.prompt)} characters). "
            f"The maximum is {api_settings.max_prompt_chars}; shorten it and try again.",
        )

    # Second, token-based guard (the character limit above stays as a cheap first check).
    # Rejects instead of truncating; raises PromptTooLargeError -> 422 PROMPT_TOO_MANY_TOKENS.
    check = budget.ensure_fits(body.prompt)

    async with limiter.slot():
        result = await ollama.generate_detailed(body.prompt)

    audit_token_usage(budget, check.tokens, result)

    return GenerateResponse(
        model=ollama.settings.model,
        response=result.text,
        status="truncated" if result.truncated else "completed",
    )
