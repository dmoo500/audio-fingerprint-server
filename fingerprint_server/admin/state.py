"""Typed application state for the private administrator service."""

from typing import Protocol, cast

import httpx
from aiohttp import web

from fingerprint_server.admin.data import AdminData
from fingerprint_server.admin.live import AdminLive
from fingerprint_server.admin.settings import AdminSettings
from fingerprint_server.admin.sqlite_data import SQLiteAdminData

type Data = AdminData | SQLiteAdminData


class Auth(Protocol):
    """Session operations consumed by the administrator routes."""

    async def login(self, username: str, password: str, request: web.Request) -> None: ...

    async def is_authenticated(self, request: web.Request) -> bool: ...

    async def logout(self, request: web.Request) -> None: ...


CONFIG_KEY = web.AppKey("admin_config", AdminSettings)
DATA_KEY: web.AppKey[Data] = cast(web.AppKey[Data], web.AppKey("admin_data", object))
LIVE_KEY = web.AppKey("admin_live", AdminLive)
AUTH_KEY = web.AppKey("admin_auth", Auth)
GATEWAY_KEY = web.AppKey("admin_gateway", httpx.AsyncClient)
SESSION_KEY = web.RequestKey("admin_session", dict[str, str])
SESSION_SECRET_KEY = web.AppKey("admin_session_secret", bytes)
