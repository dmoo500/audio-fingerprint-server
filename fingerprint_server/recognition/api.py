"""Public, bounded recognition facade; credentials never leave this process."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sqlite3
import time
from collections import deque
from typing import Annotated, Literal
from urllib.parse import urlsplit

from aiohttp import web
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from fingerprint_server.aiohttp_support import HTTPError, bounded_body
from fingerprint_server.async_utils import run_sync
from fingerprint_server.http_utils import trusted_image
from fingerprint_server.recognition.accounts import account_routes
from fingerprint_server.recognition.history import COOKIE, HISTORY_KEY
from fingerprint_server.recognition.settings import CLIENT_KEY, SETTINGS_KEY
from fingerprint_server.recognition.upstream import upstream

LOGGER = logging.getLogger(__name__)
SAMPLE_RATE = TypeAdapter(Annotated[int, Field(ge=8000, le=192000)])


class Song(BaseModel):
    """Only public song metadata is returned to the browser."""

    title: str = Field(min_length=1, max_length=1000)
    artists: list[str] = Field(min_length=1, max_length=100)
    album: str = Field(min_length=1, max_length=1000)
    artwork: str | None = Field(default=None, exclude_if=lambda value: value is None)


class UpstreamMatch(BaseModel):
    """Read the known metadata envelope without exposing internal match details."""

    metadata: Song


class RecognitionResult(BaseModel):
    """Public outcome, deliberately excluding scores and internal reference IDs."""

    status: Literal["matched", "no_match", "ambiguous"]
    match: Song | None


class RecordingCount(BaseModel):
    """Validate the API's logical searchable-recording count."""

    total: int = Field(ge=0, strict=True)


class UpstreamStatistics(BaseModel):
    """Read only the public portion of the authenticated statistics response."""

    recordings: RecordingCount


class Admission:
    """Bound single-process work and short-lived per-address request accounting."""

    def __init__(self) -> None:
        self.requests: dict[tuple[str, str], deque[float]] = {}
        self.active = 0

    def check(self, request: web.Request, scope: str, limit: int) -> None:
        """Reject excess requests without keeping an unbounded client history."""
        now = time.monotonic()
        expired = [key for key, times in self.requests.items() if times[-1] <= now - 60]
        for key in expired:
            del self.requests[key]
        address = request.remote or "unknown"
        key = (address, scope)
        if key not in self.requests and len(self.requests) >= 4096:
            raise HTTPError(503, "Recognition is busy. Please try again later.")
        times = self.requests.setdefault(key, deque())
        while times and times[0] <= now - 60:
            times.popleft()
        if len(times) >= limit:
            raise HTTPError(
                429, "Please wait a minute before trying again.", headers={"Retry-After": "60"}
            )
        times.append(now)


def create_routes(application: web.Application) -> None:
    """Create public JSON routes with isolated admission and cache state."""
    admission = Admission()
    statistics_lock = asyncio.Lock()
    statistics_count: int | None = None
    statistics_checked = float("-inf")
    statistics_retry = float("-inf")
    availability_lock = asyncio.Lock()
    availability_checked = float("-inf")
    available = False
    artwork_tokens: dict[str, tuple[str, float]] = {}

    async def statistics(request: web.Request) -> web.Response:
        nonlocal statistics_checked, statistics_count, statistics_retry
        admission.check(request, "statistics", 30)
        if statistics_lock.locked():
            raise HTTPError(
                503,
                "Collection count is refreshing. Please try again shortly.",
                headers={"Retry-After": "2"},
            )
        async with statistics_lock:
            now = time.monotonic()
            if now - statistics_checked < 60 and statistics_count is not None:
                return web.json_response({"recordings": statistics_count})
            if now < statistics_retry:
                raise HTTPError(503, "Collection count is temporarily unavailable.")
            statistics_retry = now + 5
            raw = await upstream(request.app[CLIENT_KEY], "GET", "/api/v1/dejavu/statistics")
            try:
                count = UpstreamStatistics.model_validate_json(raw).recordings.total
            except ValidationError as err:
                LOGGER.error("Recognition upstream returned invalid collection statistics")
                raise HTTPError(502, "Collection count is temporarily unavailable.") from err
            statistics_count = count
            statistics_checked = time.monotonic()
            return web.json_response({"recordings": count})

    async def availability(request: web.Request) -> web.Response:
        """Report current recognition availability without exposing backend details."""
        nonlocal available, availability_checked
        admission.check(request, "availability", 30)
        async with availability_lock:
            now = time.monotonic()
            if now - availability_checked >= 10:
                try:
                    raw = await upstream(request.app[CLIENT_KEY], "GET", "/health")
                    payload = json.loads(raw)
                    if payload != {"status": "ok"}:
                        raise ValueError("Invalid upstream health response")
                except HTTPError:
                    available = False
                except (ValueError, UnicodeError):
                    LOGGER.error("Recognition upstream returned invalid health data")
                    available = False
                else:
                    available = True
                availability_checked = time.monotonic()
            return web.json_response({"available": available})

    async def artwork(request: web.Request) -> web.Response:
        admission.check(request, "artwork", 60)
        token = request.match_info["token"]
        entry = artwork_tokens.get(token)
        if entry is None or entry[1] < time.monotonic():
            raise HTTPError(404, "Artwork request expired.")
        return await resolve_artwork(request, entry[0])

    async def history_artwork(request: web.Request) -> web.Response:
        admission.check(request, "artwork", 60)
        history = request.app.get(HISTORY_KEY)
        token = request.cookies.get(COOKIE)
        if history is None or not token:
            raise HTTPError(404, "History artwork not found.")
        reference = await run_sync(history.artwork_reference, token, int(request.match_info["id"]))
        if reference is None or not re.fullmatch(r"[a-f0-9]{64}", reference):
            raise HTTPError(404, "History artwork not found.")
        return await resolve_artwork(request, reference)

    async def resolve_artwork(request: web.Request, reference: str) -> web.Response:
        raw = await upstream(request.app[CLIENT_KEY], "GET", f"/api/v1/dejavu/artwork/{reference}")
        try:
            data = json.loads(raw)
            status, url = data["status"], data["url"]
            if status not in ("pending", "ready", "missing", "unavailable", "busy"):
                raise ValueError("Invalid artwork status")
            if status == "ready":
                if not isinstance(url, str) or not trusted_image(url):
                    raise ValueError("Invalid artwork URL")
            elif url is not None:
                raise ValueError("Unexpected artwork URL")
        except (ValueError, KeyError, TypeError) as err:
            LOGGER.error("Recognition upstream returned invalid artwork")
            raise HTTPError(502, "Album artwork is temporarily unavailable.") from err
        return web.json_response({"status": status, "url": url})

    async def recognize(request: web.Request) -> web.Response:
        values = request.query.getall("sample_rate", [])
        if not values:
            return web.json_response(
                {
                    "detail": [
                        {
                            "type": "missing",
                            "loc": ["query", "sample_rate"],
                            "msg": "Field required",
                            "input": None,
                        }
                    ]
                },
                status=422,
            )
        try:
            sample_rate = SAMPLE_RATE.validate_python(values[-1])
        except ValidationError as err:
            errors = err.errors(include_url=False)
            for error in errors:
                error["loc"] = ("query", "sample_rate", *error["loc"])
            return web.json_response({"detail": errors}, status=422)
        settings = request.app[SETTINGS_KEY]
        origin = urlsplit(str(settings.public_url))
        expected_origin = f"{origin.scheme}://{origin.netloc}"
        if (
            request.headers.get("origin") != expected_origin
            or request.headers.get("x-recognition-request") != "microphone"
        ):
            raise HTTPError(403, "Start recognition from this website.")
        admission.check(request, "recognize", settings.requests_per_minute)
        if request.headers.get("content-type") != "application/octet-stream":
            raise HTTPError(415, "Send a microphone recording.")
        if admission.active >= 2:
            raise HTTPError(503, "Recognition is busy. Please try again shortly.")
        admission.active += 1
        try:
            raw = await bounded_body(request, 15 * sample_rate * 4)
            if len(raw) % 4 or not 3 * sample_rate * 4 <= len(raw):
                raise HTTPError(422, "Record between 3 and 15 seconds of mono audio.")
            result = await upstream(
                request.app[CLIENT_KEY],
                "POST",
                f"/api/v1/dejavu/recognize?sample_rate={sample_rate}&channels=1",
                content=raw,
            )
            try:
                # The upstream match envelope and the public result intentionally differ.
                data = json.loads(result)
                if not isinstance(data, dict):
                    raise ValueError("Expected recognition result")
                status = data.get("status")
                song = None
                artwork_reference = None
                if status == "matched":
                    song = UpstreamMatch.model_validate(data.get("match")).metadata
                    song.artwork = None
                    reference = data["match"].get("reference_id", "")
                    if isinstance(reference, str) and re.fullmatch(r"[a-f0-9]{64}", reference):
                        artwork_reference = reference
                        now = time.monotonic()
                        for token, (_, expires) in list(artwork_tokens.items()):
                            if expires < now:
                                del artwork_tokens[token]
                        if len(artwork_tokens) >= 4096:
                            del artwork_tokens[next(iter(artwork_tokens))]
                        token = secrets.token_urlsafe(24)
                        artwork_tokens[token] = (reference, now + 600)
                        song.artwork = f"/api/artwork/{token}"
                elif data.get("match") is not None:
                    raise ValueError("Unexpected recognition match")
                public = RecognitionResult.model_validate({"status": status, "match": song})
            except (ValidationError, ValueError, UnicodeError) as err:
                LOGGER.error("Recognition upstream returned an invalid match response")
                raise HTTPError(502, "Recognition returned an invalid response.") from err
            response = web.json_response(public.model_dump(mode="json"))
            history = request.app.get(HISTORY_KEY)
            session_token = request.cookies.get(COOKIE)
            if public.match is not None and history is not None and session_token:
                try:
                    await run_sync(
                        history.save,
                        session_token,
                        {
                            "title": public.match.title,
                            "artists": public.match.artists,
                            "recognized_at": int(time.time()),
                        },
                        artwork_reference,
                    )
                except sqlite3.Error as err:
                    LOGGER.error("Could not save recognition history: %s", type(err).__name__)
                    response.headers["X-Recognition-History"] = "unavailable"
            return response
        finally:
            admission.active -= 1

    application.router.add_get("/api/statistics", statistics, allow_head=False)
    application.router.add_get("/api/availability", availability, allow_head=False)
    application.router.add_get("/api/artwork/{token}", artwork, allow_head=False)
    application.router.add_get(
        r"/api/account/history/{id:[0-9]{1,18}}/artwork", history_artwork, allow_head=False
    )
    application.router.add_post("/api/recognize", recognize)
    account_routes(application, admission.check)
