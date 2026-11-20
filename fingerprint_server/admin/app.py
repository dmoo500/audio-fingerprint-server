"""Private administrator service for the fingerprint collection."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import psycopg
from aiohttp import web
from aiohttp.typedefs import Handler
from pydantic import SecretStr

from fingerprint_server.admin.api import routes as api_routes
from fingerprint_server.admin.api.dependencies import require_session
from fingerprint_server.admin.audit import AUTHENTICATED, audit_admin
from fingerprint_server.admin.data import AdminData
from fingerprint_server.admin.errors import ActionFailed, LoginFailed
from fingerprint_server.admin.live import AdminLive
from fingerprint_server.admin.settings import AdminSettings
from fingerprint_server.admin.sqlite_data import SQLiteAdminData
from fingerprint_server.admin.state import (
    AUTH_KEY,
    CONFIG_KEY,
    DATA_KEY,
    GATEWAY_KEY,
    LIVE_KEY,
    SESSION_KEY,
    SESSION_SECRET_KEY,
)
from fingerprint_server.aiohttp_support import error_responses
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database.application_keys import validate as validate_application_keys

LOGGER = logging.getLogger(__name__)
FRONTEND = Path(__file__).with_name("dist")
ASSETS = ("app.js", "index.css")
SESSION_SECONDS = 3600
COOKIE_NAME = "fp_admin"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
    "style-src 'self'; img-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "Referrer-Policy": "same-origin",
}


class AdminAuth:
    """Maintain bounded, revocable administrator sessions."""

    def __init__(self, key: SecretStr) -> None:
        self.key = key
        self.sessions: dict[str, float] = {}
        self.attempts: deque[float] = deque()

    async def login(self, username: str, password: str, request: web.Request) -> None:
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
        session = request[SESSION_KEY]
        previous = session.get("token", "")
        if len(self.sessions) >= 20 and previous not in self.sessions:
            raise LoginFailed("Too many active admin sessions")
        self.sessions.pop(previous, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + SESSION_SECONDS
        session.clear()
        session["token"] = token
        LOGGER.info("Administrator logged in")

    async def is_authenticated(self, request: web.Request) -> bool:
        return self.sessions.get(request[SESSION_KEY].get("token", ""), 0) > time.monotonic()

    async def logout(self, request: web.Request) -> None:
        session = request[SESSION_KEY]
        self.sessions.pop(session.get("token", ""), None)
        session.clear()


@web.middleware
async def admin_session(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Load a signed session cookie and persist session changes."""
    app = request.app
    secret = app[SESSION_SECRET_KEY]
    original = _decode_session(request.cookies.get(COOKIE_NAME, ""), secret)
    session = original.copy()
    request[SESSION_KEY] = session
    response = await handler(request)
    if session != original:
        if session:
            value = _encode_session(session, secret)
            response.set_cookie(
                COOKIE_NAME,
                value,
                max_age=SESSION_SECONDS,
                httponly=True,
                secure=app[CONFIG_KEY].admin_cookie_secure,
                samesite="Strict",
                path="/",
            )
        else:
            response.set_cookie(
                COOKIE_NAME,
                "",
                max_age=0,
                httponly=True,
                secure=app[CONFIG_KEY].admin_cookie_secure,
                samesite="Strict",
                path="/",
            )
    return response


def _encode_session(session: dict[str, str], secret: bytes) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(session, separators=(",", ":")).encode())
    encoded = payload.rstrip(b"=")
    signature = hmac.new(secret, encoded, hashlib.sha256).digest()
    return (encoded + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def _decode_session(value: str, secret: bytes) -> dict[str, str]:
    try:
        encoded, signature = value.encode().split(b".", 1)
        expected = hmac.new(secret, encoded, hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signature + b"=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, supplied):
            return {}
        payload = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        session = json.loads(payload)
        if isinstance(session, dict) and all(
            isinstance(key, str) and isinstance(item, str) for key, item in session.items()
        ):
            return session
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return {}


@web.middleware
async def admin_authentication(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Require a session for private API and event-stream requests."""
    if request.path.startswith("/admin/api/") and request.path not in (
        "/admin/api/login",
        "/admin/api/session",
    ):
        await require_session(request)
        request[AUTHENTICATED] = True
    elif request.path == "/admin/events":
        await require_session(request)
    return await handler(request)


@web.middleware
async def admin_errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Preserve the admin API's stable database and gateway error responses."""
    try:
        return await handler(request)
    except ActionFailed as err:
        return web.json_response({"detail": str(err)}, status=400)
    except (psycopg.Error, sqlite3.Error) as err:
        LOGGER.error("Admin database operation failed: %s", type(err).__name__)
        return web.json_response({"detail": "Administrator database is unavailable"}, status=503)
    except httpx.HTTPError as err:
        LOGGER.error("Admin gateway operation failed: %s", type(err).__name__)
        return web.json_response(
            {"detail": "Gateway administration failed; see server log"}, status=502
        )


@web.middleware
async def admin_protection(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Enforce same-origin writes and set private-content security headers."""
    response: web.StreamResponse
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = f"{request.scheme}://{request.host}"
        if request.headers.get("Origin") != origin:
            response = web.json_response(
                {"detail": "A same-origin administration request is required"}, status=403
            )
        else:
            response = await handler(request)
    else:
        response = await handler(request)
    response.headers.update(SECURITY_HEADERS)
    return response


def create_admin(configuration: AdminSettings | None = None) -> web.Application:
    """Create the independent admin service without starting workers or migrating data."""

    async def lifespan(application: web.Application) -> AsyncIterator[None]:
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
            application[CONFIG_KEY] = config
            application[DATA_KEY] = data
            application[LIVE_KEY] = live
            application[AUTH_KEY] = AdminAuth(config.admin_key)
            application[SESSION_SECRET_KEY] = secrets.token_bytes(32)
            async with httpx.AsyncClient(
                base_url=str(config.gateway_admin_url).rstrip("/") + "/",
                headers={"X-API-KEY": config.gateway_admin_key.get_secret_value()},
                trust_env=False,
                follow_redirects=False,
                timeout=15,
            ) as gateway:
                application[GATEWAY_KEY] = gateway
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

    app = web.Application(
        middlewares=[
            audit_admin,
            admin_protection,
            admin_errors,
            error_responses,
            admin_session,
            admin_authentication,
        ],
        client_max_size=64 * 1024,
        handler_args={"auto_decompress": False},
    )
    app.cleanup_ctx.append(lifespan)
    app.add_routes(api_routes())
    app.router.add_get("/", root)
    app.router.add_get("/admin/events", events)
    app.router.add_get("/admin/assets/{name}", asset)
    for path in (
        "/admin/",
        "/admin/login",
        "/admin/musicbrainz-reviews",
        "/admin/keys",
        "/admin/applications",
    ):
        app.router.add_get(path, index)
    app.router.add_get("/admin/{dataset}/list", table_page)
    app.router.add_get("/admin/{dataset}/detail/{identifier}", table_page)
    return app


async def root(_request: web.Request) -> web.StreamResponse:
    """Redirect the service root to its administrator interface."""
    raise web.HTTPTemporaryRedirect("/admin/")


async def events(request: web.Request) -> web.StreamResponse:
    """Stream current collection snapshots until logout or disconnect."""
    live = request.app[LIVE_KEY]
    auth = request.app[AUTH_KEY]
    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream", "X-Accel-Buffering": "no"},
    )
    response.headers.update(SECURITY_HEADERS)
    await response.prepare(request)
    live.subscribers += 1
    try:
        while True:
            if not await auth.is_authenticated(request):
                await response.write(b"event: session-expired\ndata: {}\n\n")
                break
            changed = live.changed
            event = "unavailable" if live.failed else "snapshot"
            payload: dict[str, Any] = {} if live.failed else live.snapshot
            await response.write(f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode())
            with suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), timeout=10)
    finally:
        live.subscribers -= 1
    return response


async def asset(request: web.Request) -> web.StreamResponse:
    """Serve one allowlisted administrator frontend asset."""
    name = request.match_info["name"]
    if name not in ASSETS:
        raise web.HTTPNotFound()
    return web.FileResponse(FRONTEND / "assets" / name)


async def index(_request: web.Request) -> web.StreamResponse:
    """Serve the administrator frontend entry point."""
    return web.FileResponse(FRONTEND / "index.html")


async def table_page(request: web.Request) -> web.StreamResponse:
    """Serve a frontend route for one supported dataset."""
    dataset = request.match_info["dataset"]
    if dataset not in ("submissions", "unresolved", "references"):
        raise web.HTTPUnprocessableEntity(reason="Invalid administrator request")
    return await index(request)


app = create_admin()
