"""Timeout/connection behaviour against real local sockets (still no Ollama).

The mocked tests prove error *mapping*; these prove that the configured httpx
timeouts and connection failures behave as expected on a real network stack.
"""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

from app.config import OllamaSettings
from app.services import OllamaService, OllamaTimeoutError, OllamaUnavailableError

pytestmark = pytest.mark.anyio


def unused_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_real_read_timeout_against_server_that_never_answers():
    async def never_answers(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(65536)  # swallow the request ...
            await reader.read()  # ... then hold the connection until the client leaves
        except ConnectionError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(never_answers, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = OllamaSettings(base_url=f"http://127.0.0.1:{port}", read_timeout=0.3)

    try:
        async with OllamaService(settings) as service:
            started = time.perf_counter()
            with pytest.raises(OllamaTimeoutError):
                await service.generate("hello")
            elapsed = time.perf_counter() - started
    finally:
        server.close()
        await server.wait_closed()

    assert 0.25 <= elapsed < 5  # fired at read_timeout, not instantly and not hanging


async def test_real_connection_refused_is_reported_as_unavailable():
    settings = OllamaSettings(base_url=f"http://127.0.0.1:{unused_local_port()}")

    # Windows retries a refused connect for ~2s, so keep this to two calls.
    async with OllamaService(settings) as service:
        health = await service.health_check()
        with pytest.raises(OllamaUnavailableError):
            await service.generate("hello")

    assert health.healthy is False
