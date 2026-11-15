"""Authenticated reference ingestion and transient PCM recognition API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
import soxr
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from music_assistant_fingerprint import MAX_DOCUMENT_BYTES, SAMPLE_RATE, decode_pcm, fingerprint
from pydantic import ValidationError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from fingerprint_server.async_utils import run_sync
from fingerprint_server.schemas import Submission
from fingerprint_server.settings import Settings
from fingerprint_server.store import QueryTooBroadError, QueueFullError, Store
from fingerprint_server.worker import Worker

LOGGER = logging.getLogger(__name__)
MAX_PCM_BYTES = 15 * 192000 * 2 * 4
STATIC = Path(__file__).with_name("static")


def report_worker_exit(task: asyncio.Task[None]) -> None:
    """Report unexpected worker termination immediately, not only on health checks."""
    if not task.cancelled() and (error := task.exception()) is not None:
        LOGGER.error("Ingestion worker failed", exc_info=(type(error), error, error.__traceback__))


async def bounded_body(request: Request, limit: int) -> bytes:
    """Enforce byte limits even for streamed requests without Content-Length."""
    body = bytearray()
    try:
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise HTTPException(413, "Request exceeds the size limit")
                body.extend(chunk)
    except TimeoutError as err:
        raise HTTPException(408, "Request body timed out") from err
    return bytes(body)


def parse_submission(raw: bytes, key: str) -> tuple[str, str]:
    """Validate the canonical producer document and its SHA-256 idempotency key."""
    try:
        Submission.model_validate_json(raw)
        document = json.loads(raw)
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValidationError, ValueError, UnicodeError) as err:
        raise HTTPException(422, "Invalid fingerprint document or coverage") from err
    submission_id = hashlib.sha256(canonical.encode()).hexdigest()
    if document["format_id"] == "ma-dejavu-landmarks-v1" and len(raw) > 2_000_000:
        raise HTTPException(413, "Legacy submission exceeds the size limit")
    if not hmac.compare_digest(key, submission_id):
        raise HTTPException(422, "Idempotency-Key must be the canonical document SHA-256")
    return submission_id, canonical


def extract_query(raw: bytes, sample_rate: int, channels: int) -> list[tuple[str, int]]:
    """Fingerprint a bounded microphone capture entirely in memory."""
    try:
        audio = decode_pcm(raw, "pcm_f32le", channels)
        if not 3 <= len(audio) / sample_rate <= 15:
            raise ValueError("Capture must contain between 3 and 15 seconds of PCM")
        if sample_rate != SAMPLE_RATE:
            audio = soxr.resample(audio, sample_rate, SAMPLE_RATE, quality="HQ")
        query = fingerprint(audio)
        if len(query) > 10000:
            raise ValueError("Capture contains too many landmarks")
        return query
    except ValueError as err:
        raise HTTPException(422, str(err)) from err


def create_app(configuration: Settings | None = None) -> FastAPI:
    """Create a single-process service; initialize credentials and storage on startup."""
    request_times: dict[str, deque[float]] = {
        "upload": deque(),
        "read": deque(),
        "statistics": deque(),
    }
    query_slots = asyncio.Semaphore(2)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        settings = configuration or Settings()  # type: ignore[call-arg]
        store = Store(settings.database)
        await run_sync(store.initialize)
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            worker = Worker(store, settings, client)
            task = asyncio.create_task(worker.run(), name="fingerprint-ingestion")
            task.add_done_callback(report_worker_exit)
            application.state.settings = settings
            application.state.store = store
            application.state.worker = worker
            application.state.worker_task = task
            try:
                yield
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    application = FastAPI(title="Music Assistant Fingerprint Server", lifespan=lifespan)

    def authorize(request: Request, scope: Literal["read", "upload", "statistics"]) -> None:
        """Check scope-specific credentials before reading or parsing any request body."""
        settings: Settings = request.app.state.settings
        if settings.gateway_enabled:
            application_key = settings.application_key
            if application_key is None:
                raise HTTPException(503, "Application authentication is not configured")
            if not hmac.compare_digest(
                request.headers.get("X-Application-Key", "").encode(),
                application_key.get_secret_value().encode(),
            ):
                raise HTTPException(401, "A valid application key is required")
        expected = settings.upload_key if scope == "upload" else settings.read_key
        if not hmac.compare_digest(
            request.headers.get("X-API-Key", "").encode(), expected.get_secret_value().encode()
        ):
            raise HTTPException(401, "A valid scope-specific API key is required")
        now = time.monotonic()
        times = request_times[scope]
        while times and times[0] <= now - 60:
            times.popleft()
        if len(times) >= (60 if scope == "upload" else 30):
            raise HTTPException(429, "Request rate limit reached", headers={"Retry-After": "60"})
        times.append(now)

    @application.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; script-src 'self'; "
            "style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["Permissions-Policy"] = "microphone=(self)"
        return response

    @application.get("/health")
    async def health(request: Request) -> JSONResponse:
        task: asyncio.Task[None] = request.app.state.worker_task
        healthy = not task.done()
        return JSONResponse(
            {"status": "ok" if healthy else "failed"}, status_code=200 if healthy else 503
        )

    @application.get("/api/v1/dejavu/statistics")
    async def statistics(request: Request) -> dict[str, Any]:
        authorize(request, "statistics")
        result = await run_sync(request.app.state.store.statistics)
        result["worker_available"] = not request.app.state.worker_task.done()
        return result

    @application.post("/api/v1/dejavu/submissions")
    async def submit(request: Request) -> JSONResponse:
        authorize(request, "upload")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "Send application/json")
        raw = await bounded_body(request, MAX_DOCUMENT_BYTES)
        submission_id, canonical = await run_sync(
            parse_submission, raw, request.headers.get("Idempotency-Key", "")
        )
        if request.app.state.worker_task.done():
            raise HTTPException(503, "Ingestion worker unavailable")
        store: Store = request.app.state.store
        settings: Settings = request.app.state.settings
        try:
            row = await run_sync(
                store.submit,
                submission_id,
                canonical,
                settings.queue_limit,
                settings.queue_bytes_limit,
            )
        except QueueFullError as err:
            raise HTTPException(
                503, "Ingestion queue full", headers={"Retry-After": "300"}
            ) from err
        if row["state"] == "failed":
            raise HTTPException(409, "Submission processing failed; inspect submission status")
        if row["state"] == "queued":
            request.app.state.worker.wakeup.set()
        state = "queued" if row["state"] == "processing" else row["state"]
        return JSONResponse(
            {"submission_id": submission_id, "status": state},
            status_code=200 if state == "processed" else 202,
        )

    @application.get("/api/v1/dejavu/submissions/{submission_id}")
    async def submission_status(request: Request, submission_id: str) -> dict[str, Any]:
        authorize(request, "upload")
        row = await run_sync(request.app.state.store.get_submission, submission_id)
        if row is None:
            raise HTTPException(404, "Submission not found")
        return row

    @application.post("/api/v1/dejavu/recognize")
    async def recognize(
        request: Request,
        sample_rate: int = Query(ge=8000, le=192000),
        channels: int = Query(default=1, ge=1, le=2),
    ) -> dict[str, Any]:
        authorize(request, "read")
        if request.headers.get("content-type") != "application/octet-stream":
            raise HTTPException(415, "Send little-endian float32 PCM as application/octet-stream")
        if query_slots.locked():
            raise HTTPException(429, "Recognition busy", headers={"Retry-After": "2"})
        async with query_slots:
            raw = await bounded_body(request, min(MAX_PCM_BYTES, 15 * sample_rate * channels * 4))
            query = await run_sync(extract_query, raw, sample_rate, channels)
            if not query:
                return {"status": "no_match", "reason": "No acoustic landmarks", "match": None}
            try:
                candidates = await run_sync(request.app.state.store.match, query)
            except QueryTooBroadError as err:
                raise HTTPException(503, "Query too repetitive to evaluate safely") from err
        settings: Settings = request.app.state.settings
        if not candidates:
            return {"status": "no_match", "match": None}
        best = candidates[0]
        if (
            best["matched_landmarks"] < settings.minimum_matches
            or best["match_fraction"] < settings.minimum_fraction
            or best["matched_span_seconds"] < 1
            or best["offset_seconds"] < -0.1
        ):
            return {"status": "no_match", "match": None}
        if len(candidates) > 1 and (
            best["matched_landmarks"] < candidates[1]["matched_landmarks"] * 1.5
            or best["matched_landmarks"] - candidates[1]["matched_landmarks"] < 8
        ):
            return {"status": "ambiguous", "match": None}
        return {"status": "matched", "match": best}

    @application.get("/")
    async def microphone_page() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @application.get("/static/{name}")
    async def static_file(name: str) -> FileResponse:
        if name not in ("microphone.js", "capture.js", "style.css"):
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    return application


app = create_app()
