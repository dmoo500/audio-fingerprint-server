"""Public, bounded recognition facade; credentials never leave this process."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from aiohttp import web

from fingerprint_server.aiohttp_support import error_responses
from fingerprint_server.async_utils import run_sync
from fingerprint_server.recognition.api import Admission as Admission
from fingerprint_server.recognition.api import create_routes
from fingerprint_server.recognition.history import HISTORY_KEY, History
from fingerprint_server.recognition.settings import CLIENT_KEY, SETTINGS_KEY, RecognitionSettings
from fingerprint_server.recognition.upstream import open_client

LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
FRONTEND = Path(__file__).with_name("dist")


def create_app(
    configuration: RecognitionSettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> web.Application:
    """Create the public web service without opening the fingerprint database."""

    application = web.Application(
        middlewares=[error_responses], handler_args={"auto_decompress": False}
    )

    async def lifespan(app: web.Application) -> AsyncIterator[None]:
        for asset_path in ("index.html", "assets/app.js", "assets/index.css"):
            if not (FRONTEND / asset_path).is_file():
                raise RuntimeError(
                    "Recognition frontend assets are missing. Build recognition-frontend "
                    "with pnpm before starting the service."
                )
        settings = configuration or RecognitionSettings()  # type: ignore[call-arg]
        if settings.history_database is not None:
            history = History(settings.history_database)
            await run_sync(history.initialize)
            app[HISTORY_KEY] = history
        async with open_client(settings, transport) as client:
            app[SETTINGS_KEY] = settings
            app[CLIENT_KEY] = client
            yield

    async def browser_headers(request: web.Request, response: web.StreamResponse) -> None:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' https://archive.org https://*.archive.org "
            "https://coverartarchive.org; connect-src 'self'; worker-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
        response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def index(request: web.Request) -> web.StreamResponse:
        settings = request.app[SETTINGS_KEY]
        if f"{request.scheme}://{request.host}/" != str(settings.public_url):
            raise web.HTTPTemporaryRedirect(str(settings.public_url))
        return web.FileResponse(FRONTEND / "index.html")

    async def asset(request: web.Request) -> web.FileResponse:
        name = request.match_info["name"]
        if name in ("app.js", "index.css"):
            return web.FileResponse(FRONTEND / "assets" / name)
        if name in ("capture.js", "recording.js"):
            return web.FileResponse(STATIC / name)
        raise web.HTTPNotFound()

    application.cleanup_ctx.append(lifespan)
    application.on_response_prepare.append(browser_headers)
    application.router.add_get("/health", health, allow_head=False)
    application.router.add_get("/", index, allow_head=False)
    application.router.add_get("/assets/{name}", asset, allow_head=False)
    create_routes(application)
    return application


app = create_app()
