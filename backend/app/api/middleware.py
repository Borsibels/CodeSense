"""Pure-ASGI middleware: host check, request guards and the 500 safety net.

Stack order matters. ``add_middleware`` makes the LAST one added the OUTERMOST,
and ``create_app`` adds them so the request path is::

    HostGuard -> CORS -> RequestGuard -> UnexpectedError -> routes

* UnexpectedError sits *inside* CORS. Starlette's built-in handler for plain
  ``Exception`` lives in ServerErrorMiddleware, which is outside every user
  middleware, so a 500 produced there carries no CORS headers and a browser
  reports a misleading CORS failure instead of the real error.
* RequestGuard is inside CORS too, so its 413/415 answers are readable by the
  allowed origin.
* HostGuard is outermost: a request with a foreign Host header is refused before
  anything else runs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import HTTP_ERROR_CODES, error_response

logger = logging.getLogger(__name__)

_BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class BodyPolicy:
    """Body rules for one exact route path, replacing the global JSON/size rules for it.

    Used by upload endpoints so a 10 MiB ZIP can be accepted on them without
    raising the 128 KiB limit that protects every other route.
    """

    max_bytes: int
    content_types: frozenset[str]
    description: str = "the expected content type"


class RequestBodyTooLarge(HTTPException):
    """Raised from ``receive`` when a streamed body passes the limit.

    Must be FastAPI's HTTPException (not Starlette's): FastAPI re-raises its own
    subclass from body parsing but would wrap any other exception into a 400.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"The request body is larger than {limit} bytes.")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _hostname(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8000
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.split(":", 1)[0]


class HostGuardMiddleware:
    """Reject requests whose Host header is not an allowed name (DNS-rebinding defence).

    A hostile web page can make a victim's browser send requests to 127.0.0.1
    with ``Host: attacker.example`` after re-pointing its DNS name at loopback.
    Loopback binding alone does not stop that; checking Host does.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        self.allowed = frozenset(allowed_hosts)
        self.allow_all = "*" in self.allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not self.allow_all:
            host = _header(scope, b"host")
            if host is None or _hostname(host) not in self.allowed:
                logger.warning("Rejected request with Host header %r", host)
                if scope["type"] == "http":
                    response = error_response(
                        400,
                        "INVALID_HOST",
                        "This API only answers requests addressed to a local host name "
                        "(127.0.0.1 or localhost).",
                    )
                    await response(scope, receive, send)
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


class RequestGuardMiddleware:
    """Cap request body size and require JSON bodies, before anything is parsed.

    * ``Content-Length`` over the cap is answered 413 without reading the body;
      chunked/streamed bodies are counted as they arrive.
    * Bodies must be ``application/json``. This keeps cross-site "simple"
      requests (``text/plain``, forms) from reaching a handler regardless of
      the installed FastAPI version's own strictness.

    ``route_policies`` maps an exact request path to a :class:`BodyPolicy` that
    replaces both rules for that path only (used by the ZIP upload routes).
    """

    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int,
        route_policies: Mapping[str, BodyPolicy] | None = None,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.route_policies = dict(route_policies or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        policy = self.route_policies.get(scope.get("path", ""))
        max_bytes = policy.max_bytes if policy else self.max_body_bytes
        length_header = _header(scope, b"content-length")
        has_body = length_header != "0" and (
            length_header is not None or _header(scope, b"transfer-encoding") is not None
        )

        if length_header is not None and length_header.isdigit() and int(length_header) > max_bytes:
            response = error_response(
                413,
                HTTP_ERROR_CODES[413],
                f"The request body is larger than the {max_bytes}-byte limit.",
            )
            await response(scope, receive, send)
            return

        if scope["method"] in _BODY_METHODS and has_body:
            content_type = (_header(scope, b"content-type") or "").split(";", 1)[0].strip().lower()
            allowed = policy.content_types if policy else {"application/json"}
            if content_type not in allowed:
                message = (
                    "Send the request body as JSON with the header 'Content-Type: application/json'."
                    if policy is None
                    else f"Send the request body as {policy.description} "
                    f"with the header 'Content-Type: {sorted(allowed)[0]}'."
                )
                response = error_response(415, HTTP_ERROR_CODES[415], message)
                await response(scope, receive, send)
                return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise RequestBodyTooLarge(max_bytes)
            return message

        await self.app(scope, counting_receive, send)


class UnexpectedErrorMiddleware:
    """Turn any exception no handler claimed into the standard 500 envelope.

    Details go to the log only. If the response already started there is nothing
    safe to send, so the exception is re-raised for the server to report.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            logger.exception("Unhandled error on %s %s", scope.get("method"), scope.get("path"))
            if started:
                raise
            response = error_response(
                500,
                "INTERNAL_ERROR",
                "An unexpected error occurred in the backend. Check the server log for details.",
            )
            await response(scope, receive, send)
