"""Pydantic request/response schemas for the AI endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class GenerateRequest(BaseModel):
    # Unknown fields (e.g. "model", "options") are rejected with a 422 rather than
    # silently ignored: callers cannot pick models or tune Ollama.
    model_config = ConfigDict(extra="forbid")

    prompt: str

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be empty or whitespace-only")
        return value


class GenerateResponse(BaseModel):
    model: str
    response: str
    # "truncated" = generation hit the token limit, so `response` is partial.
    status: Literal["completed", "truncated"]


class OllamaStatus(BaseModel):
    healthy: bool
    version: str | None
    detail: str | None


class ModelStatus(BaseModel):
    name: str
    # True/False = server answered; None = could not be determined (see `detail`).
    available: bool | None
    detail: str | None


class HealthResponse(BaseModel):
    # ready       Ollama reachable AND model installed: generation can proceed.
    # degraded    Ollama reachable but the model is missing / could not be checked.
    # unavailable Ollama not reachable.
    status: Literal["ready", "degraded", "unavailable"]
    ollama: OllamaStatus
    model: ModelStatus


class ErrorDetail(BaseModel):
    field: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
