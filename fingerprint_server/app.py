"""Authenticated reference ingestion and transient PCM recognition API."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import AsyncIterator

from aiohttp import web
from aiohttp.typedefs import Handler

from fingerprint_server.aiohttp_support import error_responses
from fingerprint_server.api import (
    MAX_DOCUMENT_BYTES as MAX_DOCUMENT_BYTES,
)
from fingerprint_server.api import (
    MAX_LANDMARK_QUERY_BYTES as MAX_LANDMARK_QUERY_BYTES,
)
from fingerprint_server.api import (
    create_router,
)
from fingerprint_server.api import (
    parse_submission as parse_submission,
)
from fingerprint_server.api_state import (
    OWNERSHIP_TASK,
    RECOGNITION_IDENTITY,
    RECOGNITION_OUTCOME,
    SETTINGS,
    STORE,
    WORKER,
    WORKER_TASK,
)
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database.recognition_metrics import record_attempt, record_outcome
from fingerprint_server.openapi import build_schema
from fingerprint_server.runtime import runtime
from fingerprint_server.settings import Settings

LOGGER = logging.getLogger(__name__)


def report_worker_exit(task: asyncio.Task[None]) -> None:
    """Report unexpected worker termination immediately, not only on health checks."""
    if not task.cancelled() and (error := task.exception()) is not None:
        LOGGER.error("Ingestion worker failed", exc_info=(type(error), error, error.__traceback__))


def create_app(configuration: Settings | None = None) -> web.Application:
    """Create an API process with the configured storage and optional local worker."""

    async def lifespan(application: web.Application) -> AsyncIterator[None]:
        settings = configuration or Settings()  # type: ignore[call-arg]
        if settings.service_role == "worker":
            raise ValueError("Use python -m fingerprint_server.worker_runtime for the worker role")
        async with runtime(settings) as (store, worker, task, monitor):
            if task is not None:
                task.add_done_callback(report_worker_exit)
            if monitor is not None:
                monitor.add_done_callback(report_worker_exit)
            application[SETTINGS] = settings
            application[STORE] = store
            application[WORKER] = worker
            application[WORKER_TASK] = task
            application[OWNERSHIP_TASK] = monitor
            yield

    @web.middleware
    async def check_ownership(request: web.Request, handler: Handler) -> web.StreamResponse:
        monitor = request.app[OWNERSHIP_TASK]
        if monitor is not None and monitor.done() and request.path != "/health":
            return web.json_response({"detail": "Database process ownership lost"}, status=503)
        return await handler(request)

    @web.middleware
    async def recognition_metrics(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.method != "POST" or request.path not in (
            "/api/v1/dejavu/recognize",
            "/api/v1/dejavu/recognize/landmarks",
        ):
            return await handler(request)
        database_error: type[Exception] = sqlite3.Error
        if request.app[SETTINGS].database_backend == "postgres":
            from psycopg import Error

            database_error = Error
        minute = int(time.time()) // 60
        recorded = False
        try:
            await run_sync(record_attempt, request.app[STORE], minute)
            recorded = True
        except database_error:
            LOGGER.exception("Could not persist recognition attempt; statistics are incomplete")
        try:
            response = await handler(request)
            if response.status >= 400:
                request[RECOGNITION_OUTCOME] = "failed"
            return response
        finally:
            if recorded:
                outcome = request.get(RECOGNITION_OUTCOME, "failed")
                try:
                    await run_sync(
                        record_outcome,
                        request.app[STORE],
                        minute,
                        outcome,
                        request.get(RECOGNITION_IDENTITY),
                    )
                except database_error:
                    LOGGER.exception(
                        "Could not persist recognition outcome; statistics are incomplete"
                    )

    async def security_headers(request: web.Request, response: web.StreamResponse) -> None:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; script-src 'self'; "
            "style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["Permissions-Policy"] = "microphone=(self)"

    async def openapi(request: web.Request) -> web.Response:
        return web.json_response(build_schema(gateway=request.app[SETTINGS].gateway_enabled))

    async def docs(request: web.Request) -> web.Response:
        return web.Response(
            text='<!doctype html><html lang="en"><meta charset="UTF-8">'
            "<title>Audio Fingerprint HTTP API</title><h1>Audio Fingerprint HTTP API</h1>"
            '<p><a href="/openapi.json">OpenAPI specification</a></p></html>',
            content_type="text/html",
        )

    application = web.Application(
        middlewares=[
            error_responses,
            recognition_metrics,
            check_ownership,
            web.normalize_path_middleware(
                append_slash=False,
                remove_slash=True,
                merge_slashes=False,
                redirect_class=web.HTTPTemporaryRedirect,
            ),
        ],
        handler_args={"auto_decompress": False},
    )
    application.cleanup_ctx.append(lifespan)
    application.on_response_prepare.append(security_headers)
    application.add_routes(create_router())
    application.router.add_get("/openapi.json", openapi, allow_head=False)
    application.router.add_get("/docs", docs, allow_head=False)
    application.router.add_get("/redoc", docs, allow_head=False)
    application.router.add_get("/docs/oauth2-redirect", docs, allow_head=False)
    return application


app = create_app()
