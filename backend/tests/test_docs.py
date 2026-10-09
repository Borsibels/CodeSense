"""Offline API docs: every asset /docs needs is served locally and unchanged."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.anyio

STATIC = Path(__file__).resolve().parent.parent / "app" / "static" / "swagger"


def ollama_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"version": "x"})


async def test_docs_html_references_only_same_origin_assets(api_factory):
    api = await api_factory(ollama_ok)

    html = (await api.client.get("/docs")).text

    assert not re.findall(r"https?://", html), "docs page must not reference any absolute URL"
    assert not re.findall(r"//[A-Za-z0-9.-]+\.[a-z]{2,}", html)  # protocol-relative CDN links
    references = re.findall(r'(?:src|href)="([^"]+)"', html) + re.findall(r"""(?:url|favicon)['"]?\s*[:=]\s*['"]([^'"]+)['"]""", html)
    assert references, "expected the page to load scripts/styles"
    for ref in references:
        assert ref.startswith("/"), ref


async def test_every_asset_the_docs_page_loads_is_served_locally(api_factory):
    api = await api_factory(ollama_ok)
    html = (await api.client.get("/docs")).text
    assets = {
        ref for ref in re.findall(r'(?:src|href)="([^"]+)"', html) if ref.startswith("/docs-assets/")
    }

    assert assets >= {
        "/docs-assets/swagger-ui-bundle.js",
        "/docs-assets/swagger-ui.css",
        "/docs-assets/favicon.png",
    }
    for url in assets:
        response = await api.client.get(url)
        assert response.status_code == 200, url
        assert len(response.content) > 500, url
    js = await api.client.get("/docs-assets/swagger-ui-bundle.js")
    css = await api.client.get("/docs-assets/swagger-ui.css")
    png = await api.client.get("/docs-assets/favicon.png")
    assert "javascript" in js.headers["content-type"] and len(js.content) > 1_000_000
    assert js.content.count(b"SwaggerUIBundle") > 0
    assert css.headers["content-type"].startswith("text/css") and len(css.content) > 100_000
    assert png.headers["content-type"] == "image/png" and png.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_docs_page_points_at_the_local_openapi_schema_and_disables_the_validator(api_factory):
    api = await api_factory(ollama_ok)

    html = (await api.client.get("/docs")).text
    schema = await api.client.get("/openapi.json")

    assert "/openapi.json" in html
    assert re.search(r'"?validatorUrl"?\s*:\s*null', html)  # default would call validator.swagger.io
    assert schema.status_code == 200 and "/api/ai/generate" in schema.json()["paths"]


async def test_redoc_is_disabled_because_it_would_need_a_cdn(api_factory):
    api = await api_factory(ollama_ok)

    response = await api.client.get("/redoc")

    assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "name",
    [
        "LICENSE",  # exists on disk next to the assets, but is not whitelisted
        "NOTICE",
        "swagger-ui-bundle.js.LICENSE.txt",
        "PROVENANCE.md",
        "favicon-32x32.png",  # on-disk name; only the public alias is served
        "..%2Fconfig.py",
        "%2e%2e%2f%2e%2e%2fapp%2fconfig.py",
        "..\\..\\config.py",
        "does-not-exist.js",
    ],
)
async def test_only_whitelisted_assets_can_be_fetched(api_factory, name):
    api = await api_factory(ollama_ok)

    response = await api.client.get(f"/docs-assets/{name}")

    assert response.status_code == 404
    assert "def " not in response.text and "Apache License" not in response.text


def test_vendored_assets_match_the_recorded_hashes_and_ship_their_licenses():
    provenance = (STATIC / "PROVENANCE.md").read_text(encoding="utf-8")
    recorded = dict(re.findall(r"\| `([^`]+)` \| `([0-9a-f]{64})` \|", provenance))

    assert set(recorded) == {
        "swagger-ui-bundle.js",
        "swagger-ui.css",
        "favicon-32x32.png",
        "LICENSE",
        "NOTICE",
        "swagger-ui-bundle.js.LICENSE.txt",
    }
    for name, digest in recorded.items():
        data = (STATIC / name).read_bytes()
        if not name.endswith(".png"):
            # The hashes are of the LF form. On Windows with git's core.autocrlf=true a checkout/rebase rewrites text
            # assets with CRLF, which is the same content; only the line endings differ.
            data = data.replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == digest, f"{name} changed"
    assert "Apache License" in (STATIC / "LICENSE").read_text(encoding="utf-8")
    assert "swagger-ui-dist" in provenance and "5.33.1" in provenance
