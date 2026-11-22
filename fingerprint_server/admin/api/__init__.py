"""Private administrator API routes."""

import logging
import time

from aiohttp import web
from pydantic import BaseModel, Field

from fingerprint_server.admin.api import credentials, reviews, tables
from fingerprint_server.admin.api.dependencies import data_for
from fingerprint_server.admin.api.request import parse_model
from fingerprint_server.admin.errors import LoginFailed
from fingerprint_server.admin.history import ARTWORK, QUEUE, RECORDINGS, chart
from fingerprint_server.admin.state import AUTH_KEY, LIVE_KEY
from fingerprint_server.aiohttp_support import HTTPError
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database.metrics import HOUR
from fingerprint_server.database.recognition_metrics import PERIODS, recognition_history

LOGGER = logging.getLogger(__name__)


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=4096)


async def session(request: web.Request) -> web.Response:
    """Report whether the request has an active administrator session."""
    authenticated = await request.app[AUTH_KEY].is_authenticated(request)
    return web.json_response({"authenticated": authenticated})


async def login(request: web.Request) -> web.Response:
    """Authenticate an administrator and issue a signed session cookie."""
    body = await parse_model(request, Login)
    try:
        await request.app[AUTH_KEY].login(body.username, body.password, request)
    except LoginFailed as err:
        raise HTTPError(400, str(err)) from err
    return web.json_response({})


async def logout(request: web.Request) -> web.Response:
    """Revoke the current administrator session."""
    await request.app[AUTH_KEY].logout(request)
    return web.json_response({})


async def overview(request: web.Request) -> web.Response:
    """Return the latest collection overview and sampling status."""
    live = request.app[LIVE_KEY]
    return web.json_response({**live.snapshot, "failed": live.failed})


async def history(request: web.Request) -> web.Response:
    """Return the recent persisted collection and queue history."""
    hour = int(time.time()) // HOUR
    samples = await run_sync(data_for(request).hourly_history, hour)
    return web.json_response(
        {
            "charts": [
                ("Searchable recordings", chart(samples, hour, RECORDINGS)),
                ("Submission backlog", chart(samples, hour, QUEUE, ARTWORK)),
            ],
        }
    )


async def recognition_statistics(request: web.Request) -> web.Response:
    """Return anonymous recognition counters for a supported period."""
    period = request.query.get("period", "day")
    if period not in PERIODS:
        raise HTTPError(400, "Invalid recognition period")
    return web.json_response(await run_sync(recognition_history, data_for(request).store, period))


async def pending_review(request: web.Request) -> web.Response:
    """Schedule a non-destructive review of pending submissions."""
    await run_sync(data_for(request).store.request_pending_recheck)
    LOGGER.info("Admin requested non-destructive pending review")
    return web.json_response({})


def routes() -> list[web.RouteDef]:
    """Return all private API routes."""
    return [
        web.get("/admin/api/session", session),
        web.post("/admin/api/login", login, name="audit-login"),
        web.post("/admin/api/logout", logout, name="audit-logout"),
        web.get("/admin/api/overview", overview),
        web.get("/admin/api/history", history),
        web.get("/admin/api/recognition-statistics", recognition_statistics),
        web.post("/admin/api/pending-review", pending_review, name="audit-pending-review"),
        web.get("/admin/api/tables/{dataset}", tables.table),
        web.get("/admin/api/tables/{dataset}/{identifier}", tables.detail),
        web.get("/admin/api/references/{identifier}/artwork", tables.artwork),
        web.post("/admin/api/tables/{dataset}/actions", tables.action, name="audit-table-action"),
        web.get("/admin/api/reviews", reviews.reviews),
        web.post("/admin/api/reviews", reviews.select_review, name="audit-review-selection"),
        web.get("/admin/api/keys", credentials.list_keys),
        web.post("/admin/api/keys", credentials.change_key, name="audit-personal-key"),
        web.get("/admin/api/applications", credentials.applications),
        web.post("/admin/api/applications", credentials.change_application, name="audit-app-key"),
    ]
