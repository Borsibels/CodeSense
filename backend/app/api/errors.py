"""Centralised error handling: every error leaves the API in one JSON shape.

    {"error": {"code": "OLLAMA_UNAVAILABLE", "message": "...", "details": [...]}}

``details`` only appears for request-validation errors. Messages are fixed,
actionable strings; raw exception text, stack traces, paths and Ollama bodies
are logged server-side and never sent to the client.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.services.code_analysis_service import AnalysisUnavailableError, NoAnalyzableSourceError
from app.services.context_selection import SelectionError
from app.services.project_ingestion import ArchiveError
from app.services.project_models import ProcessingTimeout
from app.services import (
    OllamaConnectTimeoutError,
    OllamaError,
    OllamaHTTPError,
    OllamaInvalidPromptError,
    OllamaModelNotFoundError,
    OllamaResponseError,
    OllamaTimeoutError,
    OllamaUnavailableError,
    PromptTooLargeError,
    StructuredOutputInvalidError,
    StructuredOutputTruncatedError,
)

logger = logging.getLogger(__name__)

# Stable codes for framework-level HTTP errors (HTTPStatus phrases differ between
# Python versions, e.g. 413).
HTTP_ERROR_CODES = {
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "REQUEST_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
}


class ApiError(Exception):
    """An error the API raises deliberately, with its HTTP representation."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status_code, headers=headers)


def describe_ollama_error(exc: OllamaError, model: str) -> tuple[int, str, str]:
    """Map an OllamaError to ``(http_status, error_code, client_message)``.

    Order matters: subclasses must be tested before their parents
    (``OllamaConnectTimeoutError`` is an ``OllamaUnavailableError``).
    """
    if isinstance(exc, OllamaInvalidPromptError):
        return 422, "INVALID_REQUEST", "The prompt must be a non-empty string."
    if isinstance(exc, OllamaConnectTimeoutError):
        return (
            503,
            "OLLAMA_CONNECT_TIMEOUT",
            "Timed out connecting to the local AI service (Ollama). "
            "Make sure Ollama is running and try again.",
        )
    if isinstance(exc, OllamaUnavailableError):
        return (
            503,
            "OLLAMA_UNAVAILABLE",
            "The local AI service (Ollama) is unavailable. Start Ollama and try again.",
        )
    if isinstance(exc, OllamaModelNotFoundError):
        return (
            503,
            "MODEL_NOT_INSTALLED",
            f"The required AI model '{model}' is not installed. "
            f"Install it with: ollama pull {model}",
        )
    if isinstance(exc, OllamaTimeoutError):
        return (
            504,
            "GENERATION_TIMEOUT",
            "The AI model took too long to respond. Try a shorter prompt or try again.",
        )
    if isinstance(exc, OllamaHTTPError):
        # Ollama itself says it is overloaded -> retryable 503, anything else is a bad gateway.
        status = 503 if exc.status_code == 503 else 502
        return (
            status,
            "OLLAMA_UPSTREAM_ERROR",
            "The local AI service returned an error. Check the Ollama logs and try again.",
        )
    if isinstance(exc, StructuredOutputTruncatedError):
        return (
            502,
            "OUTPUT_TRUNCATED",
            "The AI's answer was too long and was cut off before it was complete. "
            "Try again with a smaller input.",
        )
    if isinstance(exc, StructuredOutputInvalidError):
        return (
            502,
            "INVALID_STRUCTURED_OUTPUT",
            "The AI did not produce a correctly structured answer. Try again.",
        )
    if isinstance(exc, OllamaResponseError):
        return (
            502,
            "INVALID_OLLAMA_RESPONSE",
            "The local AI service returned an invalid response. Try again.",
        )
    return (
        500,
        "INFERENCE_FAILED",
        "An unexpected error occurred while generating a response. Check the server log.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        logger.warning("%s %s -> %d %s", request.method, request.url.path, exc.status_code, exc.code)
        return error_response(exc.status_code, exc.code, exc.message, headers=exc.headers)

    @app.exception_handler(OllamaError)
    async def handle_ollama_error(request: Request, exc: OllamaError) -> JSONResponse:
        model = request.app.state.ollama.settings.model
        status, code, message = describe_ollama_error(exc, model)
        # Technical detail (str(exc), __cause__) stays in the log only.
        logger.log(
            logging.ERROR if status >= 500 and status != 503 else logging.WARNING,
            "%s %s -> %d %s (%s: %s)",
            request.method,
            request.url.path,
            status,
            code,
            type(exc).__name__,
            exc,
            exc_info=exc if status == 500 else None,
        )
        return error_response(status, code, message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        details = []
        for err in exc.errors():
            # loc looks like ("body", "prompt"); never echo `input` (could be huge/private).
            field = ".".join(str(part) for part in err.get("loc", ())[1:]) or "body"
            message = str(err.get("msg", "Invalid value")).removeprefix("Value error, ")
            details.append({"field": field, "message": message})
        logger.info("%s %s -> 422 INVALID_REQUEST %s", request.method, request.url.path, details)
        return error_response(
            422,
            "INVALID_REQUEST",
            "The request is invalid. Fix the listed fields and try again.",
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException):
        try:
            phrase = HTTPStatus(exc.status_code).phrase
        except ValueError:
            phrase = "Error"
        code = HTTP_ERROR_CODES.get(exc.status_code) or phrase.upper().replace(" ", "_").replace("-", "_")
        message = exc.detail if exc.status_code == 413 and isinstance(exc.detail, str) else phrase
        return error_response(exc.status_code, code, message, headers=getattr(exc, "headers", None))

    @app.exception_handler(PromptTooLargeError)
    async def handle_prompt_too_large(request: Request, exc: PromptTooLargeError) -> JSONResponse:
        check = exc.check
        logger.info("%s %s -> 422 PROMPT_TOO_MANY_TOKENS (%s)", request.method, request.url.path, exc)
        return error_response(
            422,
            "PROMPT_TOO_MANY_TOKENS",
            f"The prompt is too large for the model's context window: about {check.tokens} tokens "
            f"(an estimate) against a limit of {check.limit}. Shorten it and try again.",
        )

    @app.exception_handler(ArchiveError)
    async def handle_archive_error(request: Request, exc: ArchiveError) -> JSONResponse:
        # Entry names in the message come from the upload; the ingestion layer escapes them.
        logger.info("%s %s -> %d %s", request.method, request.url.path, exc.status, exc.code)
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(SelectionError)
    async def handle_selection_error(request: Request, exc: SelectionError) -> JSONResponse:
        logger.info("%s %s -> 422 %s", request.method, request.url.path, exc.code)
        return error_response(422, exc.code, exc.message)

    @app.exception_handler(NoAnalyzableSourceError)
    async def handle_no_analyzable_source(request: Request, exc: NoAnalyzableSourceError) -> JSONResponse:
        logger.info("%s %s -> 422 NO_ANALYZABLE_SOURCE", request.method, request.url.path)
        return error_response(422, "NO_ANALYZABLE_SOURCE", exc.message)

    @app.exception_handler(AnalysisUnavailableError)
    async def handle_analysis_unavailable(request: Request, exc: AnalysisUnavailableError) -> JSONResponse:
        logger.warning("%s %s -> 503 ANALYSIS_UNAVAILABLE", request.method, request.url.path)
        return error_response(503, "ANALYSIS_UNAVAILABLE", exc.message)

    @app.exception_handler(ProcessingTimeout)
    async def handle_processing_timeout(request: Request, exc: ProcessingTimeout) -> JSONResponse:
        logger.warning("%s %s -> 504 PROJECT_PROCESSING_TIMEOUT", request.method, request.url.path)
        return error_response(
            504,
            "PROJECT_PROCESSING_TIMEOUT",
            "Analysing the project took too long. Try a smaller project.",
        )

    # Unexpected (non-Ollama) exceptions are NOT handled here on purpose: a plain
    # `Exception` handler runs in Starlette's outermost ServerErrorMiddleware, past
    # CORS, so the 500 would lack CORS headers. UnexpectedErrorMiddleware
    # (app.api.middleware) produces the 500 envelope inside the CORS layer.
