"""Offline API documentation: Swagger UI served from vendored local assets.

FastAPI's built-in ``/docs`` pulls its JavaScript, CSS and favicon from CDNs, so
it renders blank without internet. Here the built-in page is disabled and
replaced by one that references only ``/docs-assets/*``, which maps a fixed
whitelist of files shipped in ``app/static/swagger`` (see ``PROVENANCE.md``).
Nothing else on disk is reachable: unknown names are a 404, there is no
directory mount and no path is ever built from request input.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse

ASSETS_DIR = Path(__file__).resolve().parent.parent / "static" / "swagger"
ASSETS_URL = "/docs-assets"

# public name -> (file, media type). The ONLY files this module will ever serve.
_ASSETS: dict[str, tuple[str, str]] = {
    "swagger-ui-bundle.js": ("swagger-ui-bundle.js", "text/javascript; charset=utf-8"),
    "swagger-ui.css": ("swagger-ui.css", "text/css; charset=utf-8"),
    "favicon.png": ("favicon-32x32.png", "image/png"),
}


def register_docs(app: FastAPI) -> None:
    """Add ``/docs`` and ``/docs-assets/{name}``. The app must be created with
    ``docs_url=None, redoc_url=None``; ``/openapi.json`` stays as FastAPI serves it."""

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} docs",
            swagger_js_url=f"{ASSETS_URL}/swagger-ui-bundle.js",
            swagger_css_url=f"{ASSETS_URL}/swagger-ui.css",
            swagger_favicon_url=f"{ASSETS_URL}/favicon.png",
            oauth2_redirect_url=None,
            # Swagger UI's default would call validator.swagger.io; keep it fully local.
            swagger_ui_parameters={"validatorUrl": None},
        )

    @app.get(f"{ASSETS_URL}/{{name}}", include_in_schema=False)
    async def docs_asset(name: str) -> FileResponse:
        entry = _ASSETS.get(name)
        if entry is None:
            raise HTTPException(status_code=404, detail="Not Found")
        filename, media_type = entry
        return FileResponse(
            ASSETS_DIR / filename,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )
