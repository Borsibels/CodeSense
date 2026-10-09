"""Shared fixtures: build an OllamaService on top of a mocked HTTP transport."""

from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest

from app.config import ApiSettings, OllamaSettings, ProjectSettings
from app.main import create_app
from app.services import OllamaService


@pytest.fixture
def anyio_backend() -> str:
    # Only asyncio is used by the app; skip the (uninstalled) trio backend.
    return "asyncio"


class TrackingTransport(httpx.MockTransport):
    """MockTransport that records requests and whether it was closed."""

    def __init__(self, handler) -> None:
        self.requests: list[httpx.Request] = []
        self.closed = False

        def recording_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(recording_handler)

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


@pytest.fixture
async def service_factory():
    """Return ``make(handler, **settings_overrides) -> (service, transport)``.

    Services created through the factory are closed after the test.
    """
    created: list[OllamaService] = []

    def make(handler, **overrides) -> tuple[OllamaService, TrackingTransport]:
        transport = TrackingTransport(handler)
        service = OllamaService(OllamaSettings(**overrides), transport=transport)
        created.append(service)
        return service, transport

    yield make

    for service in created:
        await service.aclose()


@pytest.fixture
async def api_factory():
    """Return ``await make(handler, api=ApiSettings(...), project=ProjectSettings(...), **ollama_overrides)``.

    Builds the real FastAPI app on top of a mocked Ollama transport, runs its
    lifespan, and yields an in-process httpx client (no sockets, no uvicorn).
    ``result.app``, ``result.client`` and ``result.transport`` are returned.
    Everything is torn down (lifespan shutdown included) after the test.
    """
    async with AsyncExitStack() as stack:

        async def make(
            handler,
            *,
            api: ApiSettings | None = None,
            project: ProjectSettings | None = None,
            **ollama_overrides,
        ):
            transport = TrackingTransport(handler)
            app = create_app(
                OllamaSettings(**ollama_overrides),
                api or ApiSettings(),
                project or ProjectSettings(),
                ollama_transport=transport,
            )
            await stack.enter_async_context(app.router.lifespan_context(app))
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    # Unhandled server errors become 500 responses instead of re-raising.
                    transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                    base_url="http://127.0.0.1",
                )
            )
            return SimpleNamespace(app=app, client=client, transport=transport)

        yield make
