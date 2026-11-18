"""Public, bounded recognition facade; credentials never leave this process."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, HttpUrl, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from fingerprint_server.access_log import AccessLogMiddleware
from fingerprint_server.http_utils import bounded_body, trusted_image

LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
MAX_RESPONSE_BYTES = 64_000


class RecognitionSettings(BaseSettings):
    """Configure the public origin and a dedicated, read-only upstream identity."""

    model_config = SettingsConfigDict(env_prefix="FP_WEB_", env_file=None)

    public_url: HttpUrl = HttpUrl("http://127.0.0.1:8100")
    api_url: HttpUrl = HttpUrl("http://127.0.0.1:8097")
    read_key: SecretStr = Field(min_length=24)
    application_key: SecretStr | None = Field(default=None, min_length=24)
    requests_per_minute: int = Field(default=4, ge=1, le=30)

    @field_validator("public_url", "api_url")
    @classmethod
    def plain_origin(cls, value: HttpUrl) -> HttpUrl:
        """Accept only HTTP(S) origins without credentials or URL suffixes."""
        if (
            value.username
            or value.password
            or value.query is not None
            or value.fragment is not None
            or value.path not in (None, "/")
        ):
            raise ValueError("Use an HTTP(S) origin without credentials, path, query or fragment")
        return value


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

    def check(self, request: Request, scope: str, limit: int) -> None:
        """Reject excess requests without keeping an unbounded client history."""
        now = time.monotonic()
        expired = [key for key, times in self.requests.items() if times[-1] <= now - 60]
        for key in expired:
            del self.requests[key]
        address = request.client.host if request.client else "unknown"
        key = (address, scope)
        if key not in self.requests and len(self.requests) >= 4096:
            raise HTTPException(503, "Recognition is busy. Please try again later.")
        times = self.requests.setdefault(key, deque())
        while times and times[0] <= now - 60:
            times.popleft()
        if len(times) >= limit:
            raise HTTPException(
                429, "Please wait a minute before trying again.", headers={"Retry-After": "60"}
            )
        times.append(now)


def create_app(
    configuration: RecognitionSettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create the public web service without opening the fingerprint database."""
    admission = Admission()
    statistics_lock = asyncio.Lock()
    statistics_count: int | None = None
    statistics_checked = float("-inf")
    statistics_retry = float("-inf")
    artwork_tokens: dict[str, tuple[str, float]] = {}

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        settings = configuration or RecognitionSettings()  # type: ignore[call-arg]
        headers = {"X-API-Key": settings.read_key.get_secret_value()}
        if settings.application_key is not None:
            headers["X-Application-Key"] = settings.application_key.get_secret_value()
        async with httpx.AsyncClient(
            base_url=str(settings.api_url),
            headers=headers,
            timeout=httpx.Timeout(25, connect=5),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            application.state.settings = settings
            application.state.client = client
            yield

    application = FastAPI(
        title="Fingerprint Recognition",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def browser_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
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
        return response

    async def upstream(
        request: Request, method: str, path: str, *, content: bytes | None = None
    ) -> bytes:
        client: httpx.AsyncClient = request.app.state.client
        try:
            async with client.stream(
                method,
                path,
                content=content,
                headers={"Content-Type": "application/octet-stream"} if content is not None else {},
            ) as response:
                if response.status_code != 200:
                    LOGGER.warning("Recognition upstream returned HTTP %s", response.status_code)
                    if response.status_code == 422:
                        raise HTTPException(422, "The recording could not be processed. Try again.")
                    if response.status_code == 429:
                        raise HTTPException(
                            429,
                            "Recognition is busy. Please try again in a minute.",
                            headers={"Retry-After": "60"},
                        )
                    raise HTTPException(503, "Recognition is temporarily unavailable.")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        LOGGER.error("Recognition upstream response exceeds the size limit")
                        raise HTTPException(502, "Recognition returned an invalid response.")
                    data.extend(chunk)
                return bytes(data)
        except httpx.RequestError as err:
            LOGGER.error("Recognition upstream request failed: %s", type(err).__name__)
            raise HTTPException(503, "Recognition is temporarily unavailable.") from err

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/")
    async def index(request: Request) -> Response:
        settings: RecognitionSettings = request.app.state.settings
        if str(request.base_url) != str(settings.public_url):
            return RedirectResponse(str(settings.public_url), status_code=307)
        return FileResponse(STATIC / "index.html")

    @application.get("/assets/{name}")
    async def asset(name: str) -> FileResponse:
        if name not in ("app.js", "style.css", "capture.js", "recording.js"):
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @application.get("/api/statistics")
    async def statistics(request: Request) -> dict[str, int]:
        nonlocal statistics_checked, statistics_count, statistics_retry
        admission.check(request, "statistics", 30)
        if statistics_lock.locked():
            raise HTTPException(
                503,
                "Collection count is refreshing. Please try again shortly.",
                headers={"Retry-After": "2"},
            )
        async with statistics_lock:
            now = time.monotonic()
            if now - statistics_checked < 60 and statistics_count is not None:
                return {"recordings": statistics_count}
            if now < statistics_retry:
                raise HTTPException(503, "Collection count is temporarily unavailable.")
            statistics_retry = now + 5
            raw = await upstream(request, "GET", "/api/v1/dejavu/statistics")
            try:
                count = UpstreamStatistics.model_validate_json(raw).recordings.total
            except ValidationError as err:
                LOGGER.error("Recognition upstream returned invalid collection statistics")
                raise HTTPException(502, "Collection count is temporarily unavailable.") from err
            statistics_count = count
            statistics_checked = time.monotonic()
            return {"recordings": count}

    @application.get("/api/artwork/{token}")
    async def artwork(request: Request, token: str) -> dict[str, str | None]:
        admission.check(request, "artwork", 60)
        entry = artwork_tokens.get(token)
        if entry is None or entry[1] < time.monotonic():
            raise HTTPException(404, "Artwork request expired.")
        raw = await upstream(request, "GET", f"/api/v1/dejavu/artwork/{entry[0]}")
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
            raise HTTPException(502, "Album artwork is temporarily unavailable.") from err
        return {"status": status, "url": url}

    @application.post("/api/recognize")
    async def recognize(
        request: Request, sample_rate: int = Query(ge=8000, le=192000)
    ) -> RecognitionResult:
        settings: RecognitionSettings = request.app.state.settings
        origin = urlsplit(str(settings.public_url))
        expected_origin = f"{origin.scheme}://{origin.netloc}"
        if (
            request.headers.get("origin") != expected_origin
            or request.headers.get("x-recognition-request") != "microphone"
        ):
            raise HTTPException(403, "Start recognition from this website.")
        admission.check(request, "recognize", settings.requests_per_minute)
        if request.headers.get("content-type") != "application/octet-stream":
            raise HTTPException(415, "Send a microphone recording.")
        if admission.active >= 2:
            raise HTTPException(503, "Recognition is busy. Please try again shortly.")
        admission.active += 1
        try:
            raw = await bounded_body(request, 15 * sample_rate * 4)
            if len(raw) % 4 or not 3 * sample_rate * 4 <= len(raw):
                raise HTTPException(422, "Record between 3 and 15 seconds of mono audio.")
            result = await upstream(
                request,
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
                if status == "matched":
                    song = UpstreamMatch.model_validate(data.get("match")).metadata
                    song.artwork = None
                    reference = data["match"].get("reference_id", "")
                    if isinstance(reference, str) and re.fullmatch(r"[a-f0-9]{64}", reference):
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
                return RecognitionResult.model_validate({"status": status, "match": song})
            except (ValidationError, ValueError, UnicodeError) as err:
                LOGGER.error("Recognition upstream returned an invalid match response")
                raise HTTPException(502, "Recognition returned an invalid response.") from err
        finally:
            admission.active -= 1

    application.add_middleware(AccessLogMiddleware)
    return application


app = create_app()
