"""Private administrator service for the fingerprint collection."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import sqlite3
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import Field, HttpUrl, SecretStr, model_validator
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from fingerprint_server.access_log import AccessLogMiddleware
from fingerprint_server.admin.api import require_session, router
from fingerprint_server.admin.data import AdminData, Dataset
from fingerprint_server.admin.errors import ActionFailed, LoginFailed
from fingerprint_server.admin.live import AdminLive
from fingerprint_server.admin.sqlite_data import SQLiteAdminData
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database.application_keys import validate as validate_application_keys
from fingerprint_server.settings import Settings

LOGGER = logging.getLogger(__name__)
FRONTEND = Path(__file__).with_name("dist")
ASSETS = ("app.js", "index.css")
SESSION_SECONDS = 3600


class AdminSettings(Settings):
    """Configure the separately published administration service."""

    model_config = {**Settings.model_config, "hide_input_in_errors": True}

    admin_key: SecretStr = Field(min_length=32)
    admin_cookie_secure: bool = False
    gateway_admin_key: SecretStr = Field(min_length=24)
    gateway_admin_url: HttpUrl = HttpUrl("http://gateway:9180/apisix/admin/")
    worker_health_url: HttpUrl = HttpUrl("http://fingerprints:8097/health")

    @model_validator(mode="after")
    def validate_admin(self) -> AdminSettings:
        """Require separate credentials and URLs without embedded credentials."""
        if self.postgres_dsn is not None and not self.postgres_dsn.get_secret_value():
            raise ValueError("The admin PostgreSQL DSN must not be empty")
        if self.admin_key in (
            self.upload_key,
            self.read_key,
            self.application_key,
            self.gateway_admin_key,
        ):
            raise ValueError("Use a separate admin UI key")
        url = self.gateway_admin_url
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("Gateway admin URL must not include credentials/query/fragment")
        url = self.worker_health_url
        if url.username or url.password or url.query or url.fragment or url.path != "/health":
            raise ValueError("Worker health URL must be a plain /health URL")
        return self


class AdminProtection(BaseHTTPMiddleware):
    """Require same-origin writes and prevent caching of administrator responses."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response: Response
        origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.method not in ("GET", "HEAD", "OPTIONS") and (
            request.headers.get("origin") != origin
        ):
            response = JSONResponse(
                {"detail": "A same-origin administration request is required"}, status_code=403
            )
        else:
            response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
                "style-src 'self'; img-src 'self'; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                "Referrer-Policy": "same-origin",
            }
        )
        return response


class AdminAuth:
    """Maintain bounded, revocable administrator sessions."""

    def __init__(self, key: SecretStr) -> None:
        self.key = key
        self.sessions: dict[str, float] = {}
        self.attempts: deque[float] = deque()

    async def login(self, username: str, password: str, request: Request) -> None:
        now = time.monotonic()
        while self.attempts and self.attempts[0] < now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 10:
            raise LoginFailed("Too many login attempts; wait one minute")
        self.attempts.append(now)
        valid_key = hmac.compare_digest(password.encode(), self.key.get_secret_value().encode())
        if username != "admin" or not valid_key:
            LOGGER.warning("Rejected administrator login")
            raise LoginFailed("Invalid administrator credentials")
        self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
        previous = request.session.get("token", "")
        if len(self.sessions) >= 20 and previous not in self.sessions:
            raise LoginFailed("Too many active admin sessions")
        self.sessions.pop(previous, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + SESSION_SECONDS
        request.session.clear()
        request.session["token"] = token
        LOGGER.info("Administrator logged in")

    async def is_authenticated(self, request: Request) -> bool:
        return self.sessions.get(request.session.get("token", ""), 0) > time.monotonic()

    async def logout(self, request: Request) -> None:
        self.sessions.pop(request.session.get("token", ""), None)
        request.session.clear()


class AdminSessionMiddleware:
    """Apply the configured cookie policy after settings are loaded at startup."""

    def __init__(self, app: ASGIApp) -> None:
        key = secrets.token_urlsafe(32)
        self.handlers = {
            secure: SessionMiddleware(
                app,
                secret_key=key,
                session_cookie="fp_admin",
                max_age=SESSION_SECONDS,
                same_site="strict",
                https_only=secure,
            )
            for secure in (False, True)
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.handlers[False](scope, receive, send)
            return
        config: AdminSettings = scope["app"].state.config
        await self.handlers[config.admin_cookie_secure](scope, receive, send)


def create_admin(configuration: AdminSettings | None = None) -> FastAPI:
    """Create the independent admin service without starting workers or migrating data."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        for path in (FRONTEND / "index.html", *(FRONTEND / "assets" / name for name in ASSETS)):
            if not path.is_file():
                raise RuntimeError("Admin frontend assets are missing. Build admin-frontend first.")
        config = configuration or AdminSettings()  # type: ignore[call-arg]
        data = (
            SQLiteAdminData(config, str(config.worker_health_url))
            if config.database_backend == "sqlite"
            else AdminData(config)
        )
        live = AdminLive(data)

        def check_registry() -> None:
            with data.store.connect() as db:
                validate_application_keys(db)

        try:
            await run_sync(check_registry)
            await live.sample()
            if live.failed:
                raise RuntimeError("Cannot initialize admin live data; see server log")
            application.state.config = config
            application.state.data = data
            application.state.live = live
            application.state.auth = AdminAuth(config.admin_key)
            async with httpx.AsyncClient(
                base_url=str(config.gateway_admin_url).rstrip("/") + "/",
                headers={"X-API-KEY": config.gateway_admin_key.get_secret_value()},
                trust_env=False,
                follow_redirects=False,
                timeout=15,
            ) as gateway:
                application.state.gateway = gateway
                task = asyncio.create_task(live.run())
                try:
                    yield
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        finally:
            if isinstance(data, SQLiteAdminData):
                await run_sync(data.close)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(AdminSessionMiddleware)
    app.add_middleware(AdminProtection)
    app.add_middleware(AccessLogMiddleware)
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "Invalid administrator request"}, status_code=422)

    @app.exception_handler(ActionFailed)
    async def action_failed(_request: Request, error: ActionFailed) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(psycopg.Error)
    @app.exception_handler(sqlite3.Error)
    async def database_failed(
        _request: Request, error: psycopg.Error | sqlite3.Error
    ) -> JSONResponse:
        LOGGER.error("Admin database operation failed: %s", type(error).__name__)
        return JSONResponse({"detail": "Administrator database is unavailable"}, status_code=503)

    @app.exception_handler(httpx.HTTPError)
    async def gateway_failed(_request: Request, error: httpx.HTTPError) -> JSONResponse:
        LOGGER.error("Admin gateway operation failed: %s", type(error).__name__)
        return JSONResponse({"detail": "Gateway administration failed; see server log"}, 502)

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin/")

    @app.get("/admin/events")
    async def events(request: Request) -> StreamingResponse:
        await require_session(request)
        live: AdminLive = request.app.state.live
        auth: AdminAuth = request.app.state.auth

        async def stream() -> AsyncIterator[str]:
            live.subscribers += 1
            try:
                while True:
                    if not await auth.is_authenticated(request):
                        yield "event: session-expired\ndata: {}\n\n"
                        return
                    changed = live.changed
                    event = "unavailable" if live.failed else "snapshot"
                    payload = {} if live.failed else live.snapshot
                    yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"
                    with suppress(TimeoutError):
                        await asyncio.wait_for(changed.wait(), timeout=10)
            finally:
                live.subscribers -= 1

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/admin/assets/{name}")
    async def asset(name: str) -> FileResponse:
        if name not in ASSETS:
            raise HTTPException(404)
        return FileResponse(FRONTEND / "assets" / name)

    @app.get("/admin/")
    @app.get("/admin/login")
    @app.get("/admin/musicbrainz-reviews")
    @app.get("/admin/keys")
    @app.get("/admin/applications")
    async def index() -> FileResponse:
        return FileResponse(FRONTEND / "index.html")

    @app.get("/admin/{dataset}/list")
    @app.get("/admin/{dataset}/detail/{identifier}")
    async def table_page(dataset: Dataset, identifier: str | None = None) -> FileResponse:
        return FileResponse(FRONTEND / "index.html")

    return app


app = create_admin()
