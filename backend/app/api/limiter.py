"""Bounded inference concurrency, kept in the API layer (not in OllamaService).

A 4 GB GPU can run one generation at a time, so by default exactly one request
holds the inference slot; others wait in the event loop (no threads, no blocking)
for up to ``queue_wait_timeout`` seconds and then get ``AI_BUSY`` (HTTP 503).
Health checks never touch the limiter.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.api.errors import ApiError


class AiBusyError(ApiError):
    """No inference slot became free within the queue-wait timeout."""

    def __init__(self, wait_seconds: float) -> None:
        super().__init__(
            503,
            "AI_BUSY",
            "The AI is busy with another request. Wait a moment and try again.",
            headers={"Retry-After": "5"},
        )
        self.wait_seconds = wait_seconds


class InferenceLimiter:
    def __init__(self, max_concurrent: int, queue_wait_timeout: float) -> None:
        self.max_concurrent = max_concurrent
        self.queue_wait_timeout = queue_wait_timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active = 0

    @property
    def active(self) -> int:
        """Number of requests currently holding an inference slot."""
        return self._active

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold an inference slot for the duration of the ``async with`` body.

        Raises :class:`AiBusyError` if no slot frees up in time. The slot is
        released on success, error, timeout *and* task cancellation.
        """
        if not self._semaphore.locked():
            # Free slot: acquire() returns without suspending. Needed so that a
            # queue_wait_timeout of 0 means "fail at once only if busy"; wait_for(..., 0)
            # would cancel the acquire before it ever ran.
            await self._semaphore.acquire()
        else:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), self.queue_wait_timeout)
            except asyncio.TimeoutError:
                raise AiBusyError(self.queue_wait_timeout) from None

        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()
