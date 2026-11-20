"""Authenticated reference ingestion and transient PCM recognition API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from typing import Annotated, Any, Literal

import soxr
from aiohttp import web
from audio_fingerprint import MAX_DOCUMENT_BYTES, SAMPLE_RATE, decode_pcm, fingerprint
from audio_fingerprint.codec import ENCODING_ID, decode_document
from pydantic import BaseModel, Field, ValidationError

from fingerprint_server.aiohttp_support import HTTPError, bounded_body
from fingerprint_server.api_state import OWNERSHIP_TASK, SETTINGS, STORE, WORKER, WORKER_TASK
from fingerprint_server.artwork import ArtworkStore
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database import Storage
from fingerprint_server.database.sqlite import QueryTooBroadError, QueueFullError, Store
from fingerprint_server.schemas import AvailabilityRequest, LandmarkQuery, Submission
from fingerprint_server.settings import Settings

LOGGER = logging.getLogger(__name__)
AUDIT_LOGGER = logging.getLogger("uvicorn.error.fingerprint.auth")
MAX_PCM_BYTES = 15 * 192000 * 2 * 4
MAX_LANDMARK_QUERY_BYTES = 512_000


def parse_submission(raw: bytes, key: str) -> tuple[str, str]:
    """Validate the canonical producer document and its SHA-256 idempotency key."""
    try:
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("Fingerprint document must be an object")
        decoded = decode_document(document)
        identity = json.dumps(decoded, sort_keys=True, separators=(",", ":"), allow_nan=False)
        Submission.model_validate_json(identity)
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValidationError, ValueError, UnicodeError) as err:
        raise HTTPError(422, "Invalid fingerprint document or coverage") from err
    submission_id = hashlib.sha256(identity.encode()).hexdigest()
    if len(identity.encode()) > MAX_DOCUMENT_BYTES:
        raise HTTPError(413, "Decoded submission exceeds the size limit")
    if document["format_id"] == "ma-dejavu-landmarks-v1" and (
        len(raw) > 2_000_000 or len(identity.encode()) > 2_000_000
    ):
        raise HTTPError(413, "Legacy submission exceeds the size limit")
    if not hmac.compare_digest(key, submission_id):
        raise HTTPError(422, "Idempotency-Key must be the canonical document SHA-256")
    return submission_id, canonical


def extract_query(raw: bytes, sample_rate: int, channels: int) -> list[tuple[str, int]]:
    """Fingerprint a bounded microphone capture entirely in memory."""
    try:
        audio = decode_pcm(raw, "pcm_f32le", channels)
        if not 3 <= len(audio) / sample_rate <= 15:
            raise ValueError("Capture must contain between 3 and 15 seconds of PCM")
        if sample_rate != SAMPLE_RATE:
            audio = soxr.resample(audio, sample_rate, SAMPLE_RATE, quality="HQ")
        rms = float((audio.astype("float64") ** 2).mean() ** 0.5)
        peak = float(abs(audio).max())
        query = fingerprint(audio)
        if len(query) > 10000:
            raise ValueError("Capture contains too many landmarks")
        LOGGER.info(
            "Recognition capture seconds=%.2f input_rate=%s channels=%s "
            "rms=%.6f peak=%.6f landmarks=%s",
            len(audio) / SAMPLE_RATE,
            sample_rate,
            channels,
            rms,
            peak,
            len(query),
        )
        return query
    except ValueError as err:
        raise HTTPError(422, str(err)) from err


class PcmParameters(BaseModel):
    sample_rate: Annotated[int, Field(ge=8000, le=192000)]
    channels: Annotated[int, Field(ge=1, le=2)] = 1


def create_router() -> web.RouteTableDef:
    """Create the fingerprint routes with process-local admission state."""
    request_times: dict[str, deque[float]] = {
        "upload": deque(),
        "read": deque(),
        "statistics": deque(),
        "availability": deque(),
    }
    query_slots = asyncio.Semaphore(2)

    router = web.RouteTableDef()

    async def worker_available(request: web.Request) -> bool:
        monitor = request.app[OWNERSHIP_TASK]
        if monitor is not None and monitor.done():
            return False
        task = request.app[WORKER_TASK]
        if task is not None:
            return not task.done()
        store = request.app[STORE]
        if isinstance(store, Store):
            return False
        return bool(await run_sync(store.worker_available))

    async def authorize(
        request: web.Request, scope: Literal["read", "upload", "statistics", "availability"]
    ) -> None:
        """Check scope-specific credentials before reading or parsing any request body."""
        settings: Settings = request.app[SETTINGS]
        application_identity = "-"
        if settings.gateway_enabled:
            application_key = settings.application_key
            if application_key is None:
                raise HTTPError(503, "Application authentication is not configured")
            supplied = request.headers.get("X-Application-Key", "")
            if hmac.compare_digest(
                supplied.encode(),
                application_key.get_secret_value().encode(),
            ):
                application_identity = "fingerprint"
            else:
                import sqlite3

                from fingerprint_server.database.application_keys import ApplicationKeys

                database_error: type[Exception] = sqlite3.Error
                if settings.database_backend == "postgres":
                    from psycopg import Error

                    database_error = Error
                try:
                    identity = await run_sync(
                        ApplicationKeys(request.app[STORE]).authenticate, supplied
                    )
                except database_error as err:
                    AUDIT_LOGGER.error(
                        "Application credential verification unavailable: %s", type(err).__name__
                    )
                    raise HTTPError(503, "Application authentication unavailable") from err
                application_identity = identity or "-"
            if application_identity == "-":
                raise HTTPError(401, "A valid application key is required")
        expected = settings.upload_key if scope in ("upload", "availability") else settings.read_key
        if not hmac.compare_digest(
            request.headers.get("X-API-Key", "").encode(), expected.get_secret_value().encode()
        ):
            raise HTTPError(401, "A valid scope-specific API key is required")
        limit = 60 if scope in ("upload", "availability") else 30
        store = request.app[STORE]
        if not isinstance(store, Store):
            allowed = await run_sync(store.allow_request, scope, limit)
        else:
            now = time.monotonic()
            times = request_times[scope]
            while times and times[0] <= now - 60:
                times.popleft()
            allowed = len(times) < limit
            if allowed:
                times.append(now)
        if not allowed:
            raise HTTPError(429, "Request rate limit reached", headers={"Retry-After": "60"})
        username = "-"
        if settings.gateway_enabled:
            authenticated_user = request.headers.get("X-Consumer-Username", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", authenticated_user):
                username = authenticated_user
            else:
                AUDIT_LOGGER.warning("Authenticated gateway request has no valid consumer identity")
        resource = request.match_info.route.resource
        AUDIT_LOGGER.info(
            "authenticated user=%s application=%s scope=%s method=%s route=%s",
            username,
            application_identity,
            scope,
            request.method,
            resource.canonical if resource else "-",
        )

    @asynccontextmanager
    async def recognition_slot(request: web.Request) -> AsyncIterator[None]:
        store = request.app[STORE]
        if not isinstance(store, Store):
            owners = ExitStack()
            try:
                acquired = await run_sync(owners.enter_context, store.recognition_slot())
                if not acquired:
                    raise HTTPError(429, "Recognition busy", headers={"Retry-After": "2"})
                yield
            finally:
                await run_sync(owners.close)
        else:
            if query_slots.locked():
                raise HTTPError(429, "Recognition busy", headers={"Retry-After": "2"})
            async with query_slots:
                yield

    @router.get("/health", allow_head=False)
    async def health(request: web.Request) -> web.Response:
        healthy = await worker_available(request)
        if healthy and request.app[SETTINGS].database_backend == "sqlite":
            import sqlite3

            def check_database() -> None:
                with request.app[STORE].connect() as db:
                    db.execute("SELECT value FROM service_state LIMIT 1").fetchone()

            try:
                await run_sync(check_database)
            except (sqlite3.Error, OSError):
                LOGGER.exception("SQLite health check failed")
                healthy = False
        return web.json_response(
            {"status": "ok" if healthy else "failed"}, status=200 if healthy else 503
        )

    @router.get("/api/v1/dejavu/statistics", allow_head=False)
    async def statistics(request: web.Request) -> web.Response:
        await authorize(request, "statistics")
        result = await run_sync(request.app[STORE].statistics)
        result["worker_available"] = await worker_available(request)
        return web.json_response(result)

    @router.get("/api/v1/dejavu/capabilities", allow_head=False)
    async def capabilities(request: web.Request) -> web.Response:
        await authorize(request, "upload")
        return web.json_response(
            {
                "api_version": 1,
                "fingerprint_formats": ["ma-dejavu-landmarks-v1", "ma-dejavu-landmarks-v2"],
                "fingerprint_encodings": ["json", ENCODING_ID],
                "max_document_bytes": MAX_DOCUMENT_BYTES,
                "availability_version": 1,
                "release_hints_version": 1,
                "recognition_landmarks_version": 1,
            }
        )

    @router.post("/api/v1/dejavu/availability")
    async def availability(request: web.Request) -> web.Response:
        await authorize(request, "availability")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPError(415, "Send application/json")
        raw = await bounded_body(request, 128_000)
        try:
            query = AvailabilityRequest.model_validate_json(raw)
        except (ValidationError, ValueError) as err:
            raise HTTPError(422, "Invalid availability request") from err
        items = [item.model_dump() for item in query.items]
        results = await run_sync(request.app[STORE].availability, items)
        for item, result in zip(items, results, strict=True):
            result["index"] = item["index"]
        return web.json_response({"version": 1, "items": results})

    @router.post("/api/v1/dejavu/submissions")
    async def submit(request: web.Request) -> web.Response:
        await authorize(request, "upload")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPError(415, "Send application/json")
        raw = await bounded_body(request, MAX_DOCUMENT_BYTES)
        submission_id, canonical = await run_sync(
            parse_submission, raw, request.headers.get("Idempotency-Key", "")
        )
        if not await worker_available(request):
            raise HTTPError(503, "Ingestion worker unavailable")
        store: Storage = request.app[STORE]
        settings: Settings = request.app[SETTINGS]
        try:
            row = await run_sync(
                store.submit,
                submission_id,
                canonical,
                settings.queue_limit,
                settings.queue_bytes_limit,
            )
        except QueueFullError as err:
            raise HTTPError(503, "Ingestion queue full", headers={"Retry-After": "300"}) from err
        if row["state"] == "failed":
            raise HTTPError(409, "Submission processing failed; inspect submission status")
        if row["state"] == "queued" and (worker := request.app[WORKER]) is not None:
            worker.wakeup.set()
        state = "queued" if row["state"] == "processing" else row["state"]
        return web.json_response(
            {"submission_id": submission_id, "status": state},
            status=200 if state == "processed" else 202,
        )

    @router.get("/api/v1/dejavu/submissions/{submission_id}", allow_head=False)
    async def submission_status(request: web.Request) -> web.Response:
        await authorize(request, "upload")
        row = await run_sync(request.app[STORE].get_submission, request.match_info["submission_id"])
        if row is None:
            raise HTTPError(404, "Submission not found")
        return web.json_response(row)

    @router.get("/api/v1/dejavu/artwork/{reference}", allow_head=False)
    async def artwork(request: web.Request) -> web.Response:
        await authorize(request, "read")
        reference = request.match_info["reference"]
        if not re.fullmatch(r"[a-f0-9]{64}", reference):
            raise HTTPError(422, "Invalid reference")
        if not request.app[SETTINGS].musicbrainz_enabled:
            return web.json_response({"status": "missing", "url": None})
        result = await run_sync(ArtworkStore(request.app[STORE]).request, reference)
        if (worker := request.app[WORKER]) is not None:
            worker.wakeup.set()
        return web.json_response(result)

    @router.post("/api/v1/dejavu/recognize")
    async def recognize(request: web.Request) -> web.Response:
        try:
            parameters = PcmParameters.model_validate(dict(request.query.items()))
        except ValidationError as err:
            errors = err.errors(include_url=False)
            for error in errors:
                error["loc"] = ("query", *error["loc"])
                if error["type"] == "missing":
                    error["input"] = None
            raise HTTPError(422, errors) from err
        sample_rate, channels = parameters.sample_rate, parameters.channels
        await authorize(request, "read")
        if request.headers.get("content-type") != "application/octet-stream":
            raise HTTPError(415, "Send little-endian float32 PCM as application/octet-stream")
        async with recognition_slot(request):
            raw = await bounded_body(request, min(MAX_PCM_BYTES, 15 * sample_rate * channels * 4))
            query = await run_sync(extract_query, raw, sample_rate, channels)
            return web.json_response(await match_query(request, query))

    @router.post("/api/v1/dejavu/recognize/landmarks")
    async def recognize_landmarks(request: web.Request) -> web.Response:
        await authorize(request, "read")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPError(415, "Send application/json")
        async with recognition_slot(request):
            raw = await bounded_body(request, MAX_LANDMARK_QUERY_BYTES)
            try:
                query = LandmarkQuery.model_validate_json(raw)
            except (ValidationError, ValueError) as err:
                raise HTTPError(422, "Invalid landmark query or capture coverage") from err
            return web.json_response(await match_query(request, query.fingerprints))

    async def match_query(request: web.Request, query: list[tuple[str, int]]) -> dict[str, Any]:
        if not query:
            LOGGER.info("Recognition decision=no_match reason=no_landmarks")
            return {"status": "no_match", "reason": "No acoustic landmarks", "match": None}
        try:
            candidates = await run_sync(request.app[STORE].match, query)
        except QueryTooBroadError as err:
            LOGGER.warning(
                "Recognition decision=unavailable reason=candidate_limit query_landmarks=%s",
                len(query),
            )
            raise HTTPError(503, "Query too repetitive to evaluate safely") from err
        settings: Settings = request.app[SETTINGS]
        if not candidates:
            LOGGER.info(
                "Recognition decision=no_match reason=no_candidates query_landmarks=%s", len(query)
            )
            return {"status": "no_match", "match": None}
        best = candidates[0]
        LOGGER.info(
            "Recognition evidence query_landmarks=%s candidates=%s best_matches=%s "
            "fraction=%.6f span_seconds=%.3f offset_seconds=%.3f runner_up_matches=%s",
            len(query),
            len(candidates),
            best["matched_landmarks"],
            best["match_fraction"],
            best["matched_span_seconds"],
            best["offset_seconds"],
            candidates[1]["matched_landmarks"] if len(candidates) > 1 else 0,
        )
        if (
            best["matched_landmarks"] < settings.minimum_matches
            or best["match_fraction"] < settings.minimum_fraction
            or best["matched_span_seconds"] < 1
            or best["offset_seconds"] < -0.1
        ):
            LOGGER.info("Recognition decision=no_match reason=acceptance_threshold")
            return {"status": "no_match", "match": None}
        if len(candidates) > 1 and (
            best["matched_landmarks"] < candidates[1]["matched_landmarks"] * 1.5
            or best["matched_landmarks"] - candidates[1]["matched_landmarks"] < 8
        ):
            LOGGER.info("Recognition decision=ambiguous")
            return {"status": "ambiguous", "match": None}
        LOGGER.info("Recognition decision=matched")
        return {"status": "matched", "match": best}

    return router
