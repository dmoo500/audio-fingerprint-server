"""Invitation registration, browser sessions and private recognition history."""

import asyncio
import sqlite3
from collections.abc import Callable
from urllib.parse import urlsplit

from aiohttp import web
from aiohttp.typedefs import Handler
from pydantic import BaseModel, Field, ValidationError

from fingerprint_server.aiohttp_support import HTTPError, bounded_body
from fingerprint_server.async_utils import run_sync
from fingerprint_server.recognition.history import COOKIE, HISTORY_KEY, SESSION_SECONDS, History
from fingerprint_server.recognition.settings import SETTINGS_KEY


class Credentials(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_-]{3,32}$")
    password: str = Field(min_length=12, max_length=128)


class Registration(Credentials):
    invite: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


def account_routes(app: web.Application, check: Callable[[web.Request, str, int], None]) -> None:
    """Mount browser account routes with bounded password checking."""
    slots = asyncio.Semaphore(2)

    def store(request: web.Request) -> History:
        history = request.app.get(HISTORY_KEY)
        if history is None:
            raise HTTPError(404, "Recognition accounts are not enabled.")
        return history

    def same_origin(request: web.Request) -> None:
        origin = urlsplit(str(request.app[SETTINGS_KEY].public_url))
        if request.headers.get("Origin") != f"{origin.scheme}://{origin.netloc}":
            raise HTTPError(403, "Use this website to manage your account.")

    async def session(request: web.Request) -> web.Response:
        history = request.app.get(HISTORY_KEY)
        token = request.cookies.get(COOKIE, "")
        username = await run_sync(history.username, token) if history and token else None
        return web.json_response({"enabled": history is not None, "username": username})

    async def authenticate(request: web.Request) -> web.Response:
        same_origin(request)
        history = store(request)
        check(request, "account", 8)
        if request.content_type != "application/json":
            raise HTTPError(415, "Send application/json.")
        raw = await bounded_body(request, 4096)
        registering = request.path.endswith("/register")
        try:
            body = (Registration if registering else Credentials).model_validate_json(raw)
        except ValidationError as err:
            raise HTTPError(422, "Check the username, password and invitation format.") from err
        if slots.locked():
            raise HTTPError(503, "Account service is busy. Please try again.")
        username = body.username.lower()
        async with slots:
            if isinstance(body, Registration):
                if not await run_sync(history.register, username, body.password, body.invite):
                    raise HTTPError(400, "Invitation unavailable or username already taken.")
                return web.json_response({}, status=201)
            token = await run_sync(
                history.login, username, body.password, request.cookies.get(COOKIE, "")
            )
        if not token:
            raise HTTPError(401, "Invalid username or password.")
        response = web.json_response({"username": username})
        response.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="Strict",
            secure=request.app[SETTINGS_KEY].public_url.scheme == "https",
            path="/",
        )
        return response

    async def logout(request: web.Request) -> web.Response:
        same_origin(request)
        history = store(request)
        if token := request.cookies.get(COOKIE):
            await run_sync(history.logout, token)
        response = web.json_response({})
        response.del_cookie(
            COOKIE, path="/", secure=request.app[SETTINGS_KEY].public_url.scheme == "https"
        )
        return response

    async def matches(request: web.Request) -> web.Response:
        history = store(request)
        token = request.cookies.get(COOKIE, "")
        if not token or not await run_sync(history.username, token):
            raise HTTPError(401, "Sign in to see your history.")
        return web.json_response({"items": await run_sync(history.recent, token)})

    async def clear(request: web.Request) -> web.Response:
        same_origin(request)
        history = store(request)
        token = request.cookies.get(COOKIE, "")
        if not token or not await run_sync(history.username, token):
            raise HTTPError(401, "Sign in to clear your history.")
        await run_sync(history.clear, token)
        return web.json_response({})

    @web.middleware
    async def storage_errors(request: web.Request, handler: Handler) -> web.StreamResponse:
        try:
            return await handler(request)
        except sqlite3.Error as err:
            request.app.logger.error("Recognition account storage failed: %s", type(err).__name__)
            return web.json_response(
                {"detail": "Account storage is temporarily unavailable."}, status=503
            )

    app.middlewares.append(storage_errors)
    app.router.add_get("/api/account/session", session, allow_head=False)
    app.router.add_post("/api/account/register", authenticate)
    app.router.add_post("/api/account/login", authenticate)
    app.router.add_post("/api/account/logout", logout)
    app.router.add_get("/api/account/history", matches, allow_head=False)
    app.router.add_post("/api/account/history/clear", clear)
