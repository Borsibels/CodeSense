"""Unit tests for InferenceLimiter (the slot logic itself, without HTTP)."""

from __future__ import annotations

import asyncio

import pytest

from app.api.limiter import AiBusyError, InferenceLimiter

pytestmark = pytest.mark.anyio


async def test_slot_is_held_inside_the_block_and_released_after():
    limiter = InferenceLimiter(1, 0)

    async with limiter.slot():
        assert limiter.active == 1
    assert limiter.active == 0


async def test_zero_queue_timeout_fails_immediately_only_when_busy():
    limiter = InferenceLimiter(1, 0)

    async with limiter.slot():
        with pytest.raises(AiBusyError):
            async with limiter.slot():
                pytest.fail("should not get a slot")
    async with limiter.slot():  # free again -> acquired even with timeout 0
        pass


async def test_slot_released_when_body_raises():
    limiter = InferenceLimiter(1, 0)

    with pytest.raises(RuntimeError):
        async with limiter.slot():
            raise RuntimeError("boom")

    assert limiter.active == 0
    async with limiter.slot():
        pass


async def test_waiter_gets_the_slot_when_it_frees_within_the_timeout():
    limiter = InferenceLimiter(1, 2)
    order: list[str] = []

    async def holder():
        async with limiter.slot():
            order.append("holder")
            await asyncio.sleep(0.1)

    async def waiter():
        async with limiter.slot():
            order.append("waiter")

    await asyncio.gather(holder(), waiter())

    assert order == ["holder", "waiter"]


async def test_cancelling_a_waiter_does_not_leak_or_steal_a_slot():
    limiter = InferenceLimiter(1, 5)
    release = asyncio.Event()

    async def holder():
        async with limiter.slot():
            await release.wait()

    async def waiter():
        async with limiter.slot():
            pytest.fail("cancelled waiter must not run")

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0.02)
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.02)

    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task
    release.set()
    await holder_task

    assert limiter.active == 0
    async with limiter.slot():  # capacity is intact
        assert limiter.active == 1


async def test_ai_busy_error_is_a_503_with_retry_after():
    error = AiBusyError(1.0)

    assert (error.status_code, error.code) == (503, "AI_BUSY")
    assert "Retry-After" in error.headers
