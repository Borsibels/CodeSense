"""Schema-validated JSON generation on top of :class:`OllamaService`.

Schema-specific logic lives here, not in the HTTP transport: the transport only
learned to forward an optional ``format`` (JSON schema) to Ollama.

Retry policy (deterministic, bounded)
-------------------------------------
At most ``max_retries + 1`` attempts, never recursive. After a rejected attempt
the next prompt is the original prompt plus one fixed sentence quoting a short,
sanitised reason, so retries differ from the first attempt in a known way.

* retried:   output that is not valid JSON, or valid JSON that fails the
             Pydantic model.
* NOT retried: any :class:`OllamaError` (server down, timeouts, HTTP errors,
             malformed Ollama replies); repeating a connectivity failure only
             burns time, and the caller should decide.
* NOT retried: output cut off at the token limit. The same prompt would hit the
             same limit and spend another full generation for nothing; this is
             raised as :class:`StructuredOutputTruncatedError` so the caller can
             change something (a smaller input, a larger ``num_predict``).

A reply is only a success if it parses **and** validates against the model;
valid JSON syntax alone is never enough.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import MAX_STRUCTURED_RETRIES, ConfigError
from app.services.ollama_service import OllamaError, OllamaGeneration, OllamaService
from app.services.token_budget import PromptTooLargeError, TokenBudget

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_MAX_REASON_CHARS = 200


class StructuredOutputError(OllamaError):
    """Base class: the model answered, but not with usable structured output."""

    default_user_message = "The AI returned a response in an unusable format."


class StructuredOutputInvalidError(StructuredOutputError):
    """Every allowed attempt produced invalid JSON or failed schema validation."""

    def __init__(self, attempts: int, reason: str) -> None:
        super().__init__(f"structured output invalid after {attempts} attempt(s): {reason}")
        self.attempts = attempts
        self.reason = reason


class StructuredOutputTruncatedError(StructuredOutputError):
    """The reply was cut off at the token limit and is not a complete valid object."""

    default_user_message = "The AI's answer was too long and was cut off. Try a smaller input."

    def __init__(self, attempts: int, reason: str) -> None:
        super().__init__(f"structured output truncated at the token limit: {reason}")
        self.attempts = attempts
        self.reason = reason


@dataclass(frozen=True)
class StructuredResult(Generic[T]):
    value: T
    attempts: int
    generation: OllamaGeneration  # metadata of the accepted attempt


class _Rejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_REASON_CHARS else text[: _MAX_REASON_CHARS - 3] + "..."


def retry_prompt(prompt: str, reason: str) -> str:
    """The prompt sent after a rejected attempt: the original plus one fixed sentence."""
    return (
        f"{prompt}\n\nYour previous reply was rejected: {reason}. "
        "Reply again with only valid JSON that matches the required schema."
    )


# A maximum-length, realistic rejection reason (schema-validation style), used by callers to
# check ahead of time whether a retry prompt would still fit. The generator re-checks every real
# retry against the budget anyway, so an unusual reason can only skip a retry, never overflow.
WORST_CASE_RETRY_REASON = _clip("the JSON did not match the required schema (sections.0.title: Field required; " * 3)


def _parse(text: str, schema_model: type[T]) -> T:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Rejected(f"the output was not valid JSON ({exc.msg} at position {exc.pos})") from exc
    try:
        return schema_model.model_validate(data)
    except ValidationError as exc:
        # loc + msg only: pydantic's str() would echo the offending input values.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'root'}: {err['msg']}" for err in exc.errors()[:3]
        )
        raise _Rejected(_clip(f"the JSON did not match the required schema ({problems})")) from exc


class StructuredGenerator:
    """Generate a Pydantic-validated object from the local model.

    The generator does no concurrency control: callers that expose it through
    an API must hold the inference slot around :meth:`generate` so that *all*
    attempts count against the limiter (see ``app.api.inference``).
    """

    def __init__(
        self,
        ollama: OllamaService,
        *,
        max_retries: int = 1,
        budget: TokenBudget | None = None,
    ) -> None:
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not (
            0 <= max_retries <= MAX_STRUCTURED_RETRIES
        ):
            raise ConfigError(
                f"max_retries must be an integer from 0 to {MAX_STRUCTURED_RETRIES}, got {max_retries!r}"
            )
        self.ollama = ollama
        self.max_retries = max_retries
        self.budget = budget

    async def generate(
        self, prompt: str, schema_model: type[T], *, max_retries: int | None = None
    ) -> StructuredResult[T]:
        """Return a validated ``schema_model`` instance or raise.

        ``max_retries`` overrides the instance default for this call only (e.g. ``0`` for a
        fallback attempt that must not be repeated).

        Raises:
            PromptTooLargeError: the prompt exceeds the token budget (nothing sent).
            OllamaError subclasses: transport/server failures (never retried here).
            StructuredOutputTruncatedError: output hit the token limit.
            StructuredOutputInvalidError: all attempts were rejected.
        """
        schema = schema_model.model_json_schema()
        retries = self.max_retries if max_retries is None else max_retries
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= MAX_STRUCTURED_RETRIES:
            raise ConfigError(f"max_retries must be an integer from 0 to {MAX_STRUCTURED_RETRIES}, got {retries!r}")
        max_attempts = retries + 1
        attempt_prompt = prompt
        reason = ""

        for attempt in range(1, max_attempts + 1):
            if self.budget is not None:
                try:
                    self.budget.ensure_fits(attempt_prompt)
                except PromptTooLargeError as exc:
                    if attempt == 1:
                        raise
                    logger.warning("Structured retry skipped: retry prompt exceeds the budget (%s)", exc)
                    raise StructuredOutputInvalidError(
                        attempt - 1, f"{reason} (retry skipped: prompt would exceed the input budget)"
                    ) from exc

            generation = await self.ollama.generate_detailed(attempt_prompt, response_format=schema)

            try:
                value = _parse(generation.text, schema_model)
            except _Rejected as rejected:
                reason = rejected.reason
                if generation.truncated:
                    # A cut-off object: retrying the identical prompt would just spend
                    # another full generation hitting the same limit.
                    logger.warning("Structured output truncated at the token limit: %s", reason)
                    raise StructuredOutputTruncatedError(attempt, reason) from None
                logger.warning(
                    "Structured output rejected (attempt %d of %d): %s", attempt, max_attempts, reason
                )
                attempt_prompt = (
                    f"{prompt}\n\nYour previous reply was rejected: {reason}. "
                    "Reply again with only valid JSON that matches the required schema."
                )
                continue

            if generation.truncated:
                # Complete and schema-valid, so it cannot be a partial answer; the model just
                # did not emit an end-of-sequence token before the limit.
                logger.warning("Valid structured output arrived with done_reason=length")
            return StructuredResult(value=value, attempts=attempt, generation=generation)

        raise StructuredOutputInvalidError(max_attempts, reason)
