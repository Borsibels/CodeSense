"""Asynchronous client for a locally hosted Ollama server.

Framework-independent on purpose: nothing here imports FastAPI, so the same
service can be used from scripts, tests and (later) API routes.

Typical use::

    async with OllamaService() as ollama:
        if (await ollama.health_check()).healthy and await ollama.model_available():
            text = await ollama.generate("Explain this code: ...")

Error model
-----------
Every failure is raised as a subclass of :class:`OllamaError`. ``str(exc)``
carries technical detail for logs; ``exc.user_message`` is a fixed, safe string
suitable for showing to end users. The original ``httpx`` exception (if any) is
preserved as ``__cause__`` and never leaks through the public API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

from app.config import OllamaSettings

logger = logging.getLogger(__name__)

_MAX_ERROR_DETAIL_CHARS = 300


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class OllamaError(Exception):
    """Base class for all errors raised by :class:`OllamaService`."""

    default_user_message = "The local AI service failed to process the request."

    def __init__(self, detail: str | None = None, *, user_message: str | None = None) -> None:
        super().__init__(detail or self.default_user_message)
        self.user_message = user_message or self.default_user_message


class OllamaInvalidPromptError(OllamaError, ValueError):
    """The prompt was empty, blank, or not a string. No request was sent."""

    default_user_message = "The prompt must be a non-empty string."


class OllamaUnavailableError(OllamaError):
    """The Ollama server could not be reached (not running, refused, DNS, ...)."""

    default_user_message = (
        "The local AI service (Ollama) is not reachable. Make sure Ollama is running."
    )


class OllamaConnectTimeoutError(OllamaUnavailableError):
    """Establishing the TCP connection to Ollama timed out."""

    default_user_message = "Timed out while connecting to the local AI service (Ollama)."


class OllamaTimeoutError(OllamaError):
    """Ollama accepted the connection but did not respond in time.

    Covers the generation (read) timeout, plus write and pool timeouts.
    """

    default_user_message = "The AI model took too long to respond. Try again with a smaller input."


class OllamaModelNotFoundError(OllamaError):
    """The configured model is not installed on the Ollama server."""

    default_user_message = "The required AI model is not installed."


class OllamaHTTPError(OllamaError):
    """Ollama answered with a non-success HTTP status."""

    default_user_message = "The local AI service returned an error."

    def __init__(
        self, status_code: int, detail: str = "", *, user_message: str | None = None
    ) -> None:
        super().__init__(
            f"Ollama returned HTTP {status_code}" + (f": {detail}" if detail else ""),
            user_message=user_message,
        )
        self.status_code = status_code


class OllamaResponseError(OllamaError):
    """Ollama answered 2xx but the body was malformed, incomplete or empty."""

    default_user_message = "The local AI service returned an unexpected response."


class OllamaInferenceError(OllamaError):
    """Catch-all for unexpected failures while talking to Ollama."""

    default_user_message = "An unexpected error occurred while generating a response."


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OllamaHealth:
    """Outcome of :meth:`OllamaService.health_check`.

    ``detail`` is user-safe; technical detail is written to the log instead.
    """

    healthy: bool
    detail: str
    version: str | None = None


@dataclass(frozen=True)
class OllamaGeneration:
    """Outcome of :meth:`OllamaService.generate_detailed`.

    ``truncated`` is True when generation stopped because it hit the
    ``num_predict`` token limit, i.e. ``text`` is a partial answer.
    ``done_reason`` is Ollama's raw reason (``"stop"``, ``"length"``, ...) or
    ``None`` if it was not reported.
    """

    text: str
    done_reason: str | None
    truncated: bool
    # Ollama's own token counts (None if not reported). prompt_eval_count excludes
    # tokens served from Ollama's prompt cache, so it can under-count, never over-count.
    prompt_eval_count: int | None = None
    eval_count: int | None = None


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class OllamaService:
    """Reusable async wrapper around Ollama's HTTP API.

    A single ``httpx.AsyncClient`` is created lazily on first use and reused
    for every call (connection pooling). Release it with :meth:`aclose` or by
    using the service as an async context manager. Calling a method after
    :meth:`aclose` transparently opens a fresh client.

    ``transport`` exists so tests can inject ``httpx.MockTransport``.
    """

    def __init__(
        self,
        settings: OllamaSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or OllamaSettings.from_env()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

        s = self.settings
        self._timeout = httpx.Timeout(
            connect=s.connect_timeout,
            read=s.read_timeout,
            write=s.write_timeout,
            pool=s.pool_timeout,
        )
        self._health_timeout = httpx.Timeout(
            connect=s.connect_timeout,
            read=s.health_read_timeout,
            write=s.write_timeout,
            pool=s.pool_timeout,
        )

    # -- lifecycle ---------------------------------------------------------- #
    async def __aenter__(self) -> OllamaService:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Safe to call more than once."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.base_url,
                timeout=self._timeout,
                transport=self._transport,
                # Ollama is local: never route it through HTTP(S)_PROXY from the
                # environment, a common source of confusing 502s on dev machines.
                trust_env=False,
            )
        return self._client

    # -- public API --------------------------------------------------------- #
    async def health_check(self) -> OllamaHealth:
        """Report whether the Ollama server is reachable. Never runs inference.

        Uses ``GET /api/version``. Does not raise for an unhealthy server; the
        returned :class:`OllamaHealth` says what is wrong.
        """
        try:
            response = await self._request("GET", "/api/version", timeout=self._health_timeout)
            self._raise_for_status(response)
            data = self._json_object(response, "/api/version")
        except OllamaError as exc:
            logger.warning("Ollama health check failed: %s", exc)
            return OllamaHealth(healthy=False, detail=exc.user_message)

        version = data.get("version")
        return OllamaHealth(
            healthy=True,
            detail="Ollama is reachable.",
            version=version if isinstance(version, str) else None,
        )

    async def model_available(self) -> bool:
        """Return whether the configured model is installed (``GET /api/tags``).

        ``True``/``False`` always mean the server answered and the model list
        was read. If that cannot be determined (server down, HTTP error,
        malformed body) an :class:`OllamaError` is raised instead, so "model
        missing" is never confused with "server unreachable".
        """
        response = await self._request("GET", "/api/tags", timeout=self._health_timeout)
        self._raise_for_status(response)
        data = self._json_object(response, "/api/tags")

        models = data.get("models")
        if not isinstance(models, list):
            raise OllamaResponseError("/api/tags response has no 'models' list")

        installed: set[str] = set()
        for entry in models:
            if not isinstance(entry, dict):
                raise OllamaResponseError("/api/tags 'models' contains a non-object entry")
            names = [entry.get(key) for key in ("name", "model")]
            names = [n for n in names if isinstance(n, str) and n]
            if not names:
                raise OllamaResponseError("/api/tags model entry has no 'name'")
            installed.update(_with_tag(n) for n in names)

        return _with_tag(self.settings.model) in installed

    async def generate(self, prompt: str) -> str:
        """Run a single non-streaming completion and return the generated text.

        Thin wrapper over :meth:`generate_detailed` for callers that only need
        the text. Raises the same errors.
        """
        return (await self.generate_detailed(prompt)).text

    async def generate_detailed(
        self, prompt: str, *, response_format: dict[str, Any] | None = None
    ) -> OllamaGeneration:
        """Like :meth:`generate`, but also reports truncation and token counts.

        ``response_format`` is an optional JSON schema; it is sent as Ollama's
        ``format`` field so the output is constrained to that schema. This is
        for internal callers only (see ``app.services.structured``); the model
        and every generation option remain fixed by the server settings.

        Raises:
            OllamaInvalidPromptError: ``prompt`` is empty/blank/not a string.
            OllamaUnavailableError / OllamaConnectTimeoutError: server unreachable.
            OllamaTimeoutError: generation exceeded the read timeout.
            OllamaModelNotFoundError: the model is not installed.
            OllamaHTTPError: any other non-2xx answer.
            OllamaResponseError: malformed JSON, or missing/empty ``response``.
            OllamaInferenceError: any other unexpected failure.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise OllamaInvalidPromptError()

        s = self.settings
        payload: dict[str, Any] = {
            "model": s.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_ctx": s.num_ctx,
                "num_predict": s.num_predict,
                "temperature": s.temperature,
            },
        }
        if response_format is not None:
            payload["format"] = response_format

        response = await self._request("POST", "/api/generate", timeout=self._timeout, json=payload)
        self._raise_for_status(response)
        data = self._json_object(response, "/api/generate")

        if "error" in data:
            raise OllamaInferenceError(f"Ollama reported an error: {_truncate(data['error'])}")

        text = data.get("response")
        if text is None:
            raise OllamaResponseError("/api/generate response is missing the 'response' field")
        if not isinstance(text, str):
            raise OllamaResponseError(
                f"/api/generate 'response' must be a string, got {type(text).__name__}"
            )
        if not text.strip():
            raise OllamaResponseError("/api/generate returned an empty 'response'")
        if data.get("done") is False:
            raise OllamaResponseError("/api/generate returned an incomplete response (done=false)")

        done_reason = data.get("done_reason")
        done_reason = done_reason if isinstance(done_reason, str) else None
        truncated = done_reason == "length"
        if truncated:
            logger.warning(
                "Generation stopped at the num_predict limit (%d tokens); output may be truncated.",
                s.num_predict,
            )
        return OllamaGeneration(
            text=text,
            done_reason=done_reason,
            truncated=truncated,
            prompt_eval_count=_optional_count(data.get("prompt_eval_count")),
            eval_count=_optional_count(data.get("eval_count")),
        )

    # -- internals ---------------------------------------------------------- #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: httpx.Timeout,
        json: Any = None,
    ) -> httpx.Response:
        """Send a request, translating transport failures into OllamaErrors."""
        client = self._get_client()
        logger.debug("Ollama %s %s", method, path)
        try:
            return await client.request(method, path, json=json, timeout=timeout)
        except httpx.ConnectTimeout as exc:
            raise OllamaConnectTimeoutError(
                f"Timed out after {timeout.connect}s connecting to {self.settings.base_url}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise OllamaTimeoutError(
                f"{type(exc).__name__} during {method} {path} "
                f"(read={timeout.read}s, write={timeout.write}s, pool={timeout.pool}s)"
            ) from exc
        except httpx.ConnectError as exc:
            raise OllamaUnavailableError(
                f"Cannot connect to Ollama at {self.settings.base_url}: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - deliberate: wrap everything unexpected
            raise OllamaInferenceError(
                f"Unexpected {type(exc).__name__} during {method} {path}: {exc}"
            ) from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        detail = _truncate(_error_text(response))
        lowered = detail.lower()
        if response.status_code == 404 and "model" in lowered and "not found" in lowered:
            model = self.settings.model
            raise OllamaModelNotFoundError(
                f"Model {model!r} is not installed on Ollama: {detail}",
                user_message=(
                    f"The required AI model '{model}' is not installed. "
                    f"Install it with: ollama pull {model}"
                ),
            )
        raise OllamaHTTPError(response.status_code, detail)

    @staticmethod
    def _json_object(response: httpx.Response, endpoint: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
            raise OllamaResponseError(f"{endpoint} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise OllamaResponseError(
                f"{endpoint} returned JSON {type(data).__name__}, expected an object"
            )
        return data


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _with_tag(name: str) -> str:
    """Ollama treats ``foo`` as ``foo:latest``; normalise before comparing."""
    return name if ":" in name else f"{name}:latest"


def _optional_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _truncate(value: object) -> str:
    text = str(value)
    if len(text) > _MAX_ERROR_DETAIL_CHARS:
        return text[:_MAX_ERROR_DETAIL_CHARS] + "..."
    return text


def _error_text(response: httpx.Response) -> str:
    """Best-effort extraction of Ollama's ``{"error": "..."}`` message."""
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return body["error"]
    return response.text
