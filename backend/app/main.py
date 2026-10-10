"""FastAPI application entry point (``app.main:app``).

Run from ``backend/``::

    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import ai, analysis, projects
from app.api.docs import register_docs
from app.api.errors import register_exception_handlers
from app.api.inference import audit_token_usage
from app.api.limiter import InferenceLimiter
from app.api.middleware import (
    BodyPolicy,
    HostGuardMiddleware,
    RequestGuardMiddleware,
    UnexpectedErrorMiddleware,
)
from app.config import ApiSettings, OllamaSettings, ProjectSettings
from app.services import OllamaService, StructuredGenerator, TokenBudget
from app.services.code_analysis_service import CodeAnalysisService

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Give the ``app.*`` loggers a console handler (uvicorn only sets up its own).

    Idempotent; records still propagate to the root logger, so pytest's caplog
    and any host application's logging keep working.
    """
    app_logger = logging.getLogger("app")
    if app_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     [%(name)s] %(message)s"))
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)


def create_app(
    ollama_settings: OllamaSettings | None = None,
    api_settings: ApiSettings | None = None,
    project_settings: ProjectSettings | None = None,
    *,
    ollama_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the application.

    Settings default to the environment. ``ollama_transport`` lets tests swap
    in ``httpx.MockTransport``. Invalid configuration fails here, at startup,
    with a ``ConfigError`` naming the bad variable.
    """
    _configure_logging()
    ollama_settings = ollama_settings or OllamaSettings.from_env()
    api_settings = api_settings or ApiSettings.from_env()
    project_settings = project_settings or ProjectSettings.from_env()
    # Validates the context-window arithmetic too (ConfigError at startup if it cannot work).
    budget = TokenBudget(
        total_context=ollama_settings.num_ctx,
        reserved_generation=ollama_settings.num_predict,
        safety_margin=api_settings.safety_margin_tokens,
        max_input_tokens=api_settings.max_input_tokens,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Creating the service makes no network call: the API starts even if
        # Ollama is down, and /api/ai/health reports that state.
        ollama = OllamaService(ollama_settings, transport=ollama_transport)
        app.state.ollama = ollama
        app.state.limiter = InferenceLimiter(
            api_settings.max_concurrent_requests, api_settings.queue_wait_timeout
        )
        app.state.api_settings = api_settings
        app.state.project_settings = project_settings
        app.state.project_limiter = projects.ProjectLimiter(
            project_settings.max_concurrent, project_settings.queue_wait_timeout
        )
        app.state.budget = budget
        app.state.structured = StructuredGenerator(
            ollama, max_retries=api_settings.structured_max_retries, budget=budget
        )
        app.state.analysis = CodeAnalysisService(
            app.state.structured,
            budget,
            reserve=project_settings.instruction_reserve_tokens,
            inference_slot=app.state.limiter.slot,
            on_generation=lambda estimated, generation: audit_token_usage(budget, estimated, generation),
        )
        if app.state.analysis.unavailable_reason:
            logger.warning("POST /api/ai/analyze is disabled: %s", app.state.analysis.unavailable_reason)
        logger.info(
            "Sift API starting: model=%s ollama=%s max_concurrent=%d queue_wait=%.1fs "
            "input_limit=%d est. tokens (ctx=%d, reserved_output=%d, margin=%d)",
            ollama_settings.model,
            ollama_settings.base_url,
            api_settings.max_concurrent_requests,
            api_settings.queue_wait_timeout,
            budget.input_limit,
            budget.total_context,
            budget.reserved_generation,
            budget.safety_margin,
        )
        try:
            yield
        finally:
            await ollama.aclose()
            logger.info("Sift API stopped; Ollama HTTP client closed")

    # Built-in /docs and /redoc load assets from CDNs; replaced by register_docs (offline).
    app = FastAPI(
        title="Sift API",
        description="Sift is an offline AI-powered code comprehension and debugging platform. Make sense of every line.",
        version="0.4.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    # add_middleware: the LAST one added is the OUTERMOST. Request path:
    #   HostGuard -> CORS -> RequestGuard -> UnexpectedError -> routes
    # (see app/api/middleware.py for why this order matters).
    app.add_middleware(UnexpectedErrorMiddleware)
    # The ZIP upload routes get their own size cap and content types; every other route keeps
    # the small JSON-only limit.
    zip_policy = BodyPolicy(
        max_bytes=project_settings.max_zip_bytes,
        content_types=projects.ZIP_CONTENT_TYPES,
        description="a ZIP archive",
    )
    app.add_middleware(
        RequestGuardMiddleware,
        max_body_bytes=api_settings.max_body_bytes,
        route_policies={path: zip_policy for path in (*projects.UPLOAD_PATHS, analysis.PATH)},
    )
    if api_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(api_settings.cors_origins),  # explicit list, never "*"
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )
    app.add_middleware(HostGuardMiddleware, allowed_hosts=api_settings.allowed_hosts)

    register_exception_handlers(app)
    register_docs(app)
    app.include_router(ai.router)
    app.include_router(analysis.router)
    app.include_router(projects.router)
    return app


app = create_app()
