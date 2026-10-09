"""API-layer helpers that run inference under the concurrency limiter.

Phase 3 endpoints should call :func:`run_structured` (or take the slot
themselves with ``app.state.limiter.slot()``) instead of calling the services
directly, so concurrency limits are enforced in one place.
"""

from __future__ import annotations

import logging
from typing import TypeVar

from fastapi import FastAPI
from pydantic import BaseModel

from app.services import OllamaGeneration, StructuredResult, TokenBudget
from app.services.token_budget import MEASURED_TEMPLATE_OVERHEAD_TOKENS

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


async def run_structured(app: FastAPI, prompt: str, schema_model: type[T]) -> StructuredResult[T]:
    """Structured generation holding ONE inference slot for every attempt.

    Retries therefore never run alongside another request, and a request that
    cannot get a slot in time fails with ``AI_BUSY`` before anything is sent.
    The prompt is budget-checked (by the generator) before each attempt.
    """
    async with app.state.limiter.slot():
        return await app.state.structured.generate(prompt, schema_model)


def audit_token_usage(
    budget: TokenBudget, estimated_prompt_tokens: int, generation: OllamaGeneration
) -> None:
    """Compare the estimate with Ollama's real counts and log disagreements.

    Log-only: a silent context overflow cannot be undone after the fact, but it
    can be noticed and the estimator or margin retuned. ``prompt_eval_count``
    excludes tokens served from Ollama's prompt cache, so it can under-count
    (hiding an overflow) but never over-count (no false alarms).
    """
    evaluated = generation.prompt_eval_count
    if evaluated is None:
        return
    prompt_only = evaluated - MEASURED_TEMPLATE_OVERHEAD_TOKENS
    if prompt_only > estimated_prompt_tokens:
        logger.warning(
            "Token estimator UNDER-estimated: estimated %d prompt tokens but Ollama evaluated %d "
            "(about %d after the chat template). Consider raising AI_SAFETY_MARGIN_TOKENS.",
            estimated_prompt_tokens,
            evaluated,
            prompt_only,
        )
    if evaluated + budget.reserved_generation > budget.total_context:
        logger.warning(
            "Prompt (%d tokens evaluated) plus reserved generation (%d) exceeds the %d-token "
            "context window; Ollama may have truncated the prompt or shifted the context.",
            evaluated,
            budget.reserved_generation,
            budget.total_context,
        )
