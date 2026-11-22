"""Shared MusicBrainz transport, cache, pacing and cooldown lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from fingerprint_server.async_utils import run_sync
from fingerprint_server.database import Storage
from fingerprint_server.musicbrainz_errors import EnrichmentError, MusicBrainzUnavailable
from fingerprint_server.musicbrainz_parsing import parse_artwork, parse_recording
from fingerprint_server.settings import Settings

MUSICBRAINZ_CACHE_VERSION = 8


def _retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    value = value.strip()
    if value.isascii() and value.isdigit():
        try:
            seconds = float(value)
        except ValueError:
            return 0.0
        return seconds if math.isfinite(seconds) else 0.0
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0.0, date.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError, OSError):
        return 0.0


class MusicBrainzClient:
    """Own the process-lifetime client state used by identifier verification."""

    parse_artwork = staticmethod(parse_artwork)
    parse_recording = staticmethod(parse_recording)

    def __init__(self, store: Storage, settings: Settings, client: httpx.AsyncClient) -> None:
        self.store = store
        self.settings = settings
        self.client = client
        self.next_lookup = 0.0
        self.lookup_lock = asyncio.Lock()
        self.cooldown_until = 0.0
        self.backoff = 0.0
        self.last_unavailable_status: int | None = None
        self.lookup_call = "-"
        self.batch_recordings: dict[str, dict[str, Any]] = {}
        self.batch_isrcs: dict[str, list[dict[str, Any]]] = {}
        self.batch_candidates: dict[str, dict[str, Any]] = {}
        self.batch_submissions: set[str] = set()

    async def wait_for_cooldown(self) -> None:
        """Wait for a service-wide cooldown before the worker claims more work."""
        await self._wait_until(0)

    async def _wait_until(self, deadline: float) -> None:
        while (delay := max(deadline, self.cooldown_until) - time.monotonic()) > 0:
            await asyncio.sleep(delay)

    async def lookup(self, kind: str, identifier: str) -> dict[str, Any]:
        """Fetch and cache a bounded, typed identifier lookup."""
        if kind == "recording" and identifier in self.batch_recordings:
            return {"status": "resolved", "recording": self.batch_recordings[identifier]}
        async with self.lookup_lock:
            self.lookup_call = (
                "GET /recording (identifier batch)"
                if kind == "batch"
                else "GET /release (recording artwork)"
                if kind == "artwork"
                else "GET /recording (metadata search)"
                if kind == "search"
                else f"GET /{kind}/{identifier}"
            )
            result = await self._lookup(kind, identifier)
            if kind == "artwork" and result.get("status") == "resolved":
                releases = list(result["releases"])
                for _ in range(2):
                    if result["complete"]:
                        break
                    result = await self._lookup(kind, identifier, page_offset=len(releases))
                    if result.get("status") != "resolved":
                        raise EnrichmentError("MusicBrainz artwork pagination failed")
                    releases.extend(result["releases"])
                return {**result, "releases": releases}
            return result

    async def _lookup(self, kind: str, identifier: str, *, page_offset: int = 0) -> dict[str, Any]:
        """Perform a rate-limited lookup using a parser-versioned cache namespace."""
        endpoint = hashlib.sha256(
            str(self.settings.musicbrainz_base_url).rstrip("/").encode()
        ).hexdigest()
        cache_identifier = (
            hashlib.sha256(identifier.encode()).hexdigest()
            if kind in ("search", "batch")
            else identifier
        )
        cache_kind = "artwork-v3" if kind == "artwork" else kind
        if kind == "artwork" and page_offset:
            cache_identifier = f"{identifier}:{page_offset}"
        key = f"musicbrainz:v{MUSICBRAINZ_CACHE_VERSION}:{endpoint}:{cache_kind}:{cache_identifier}"
        if (cached := await run_sync(self.store.cached_enrichment, key)) is not None:
            return cached
        await self._wait_until(self.next_lookup)
        self.next_lookup = time.monotonic() + 1.1
        async with self.client.stream(
            "GET",
            (
                f"{str(self.settings.musicbrainz_base_url).rstrip('/')}/release"
                if kind == "artwork"
                else f"{str(self.settings.musicbrainz_base_url).rstrip('/')}/recording"
                if kind in ("search", "batch")
                else f"{str(self.settings.musicbrainz_base_url).rstrip('/')}/{kind}/{identifier}"
            ),
            params={
                "recording": identifier,
                "offset": str(page_offset),
                "limit": "100",
                "inc": "release-groups",
                "fmt": "json",
            }
            if kind == "artwork"
            else {"query": identifier, "limit": "100" if kind == "batch" else "25", "fmt": "json"}
            if kind in ("search", "batch")
            else {
                "inc": "artists+isrcs+releases+release-groups"
                if kind == "recording"
                else "artists+releases",
                "fmt": "json",
            },
            headers={"User-Agent": f"MA-Fingerprint/0.1 ({self.settings.musicbrainz_contact})"},
            follow_redirects=False,
        ) as response:
            if response.status_code in (429, 502, 503):
                if response.status_code == 503:
                    self.backoff = (
                        min(900.0, self.backoff + 10)
                        if self.last_unavailable_status == 503 and self.backoff
                        else 60.0
                    )
                else:
                    self.backoff = min(900.0, max(30.0, self.backoff * 2))
                self.last_unavailable_status = response.status_code
                delay = max(self.backoff, _retry_after(response.headers.get("Retry-After")))
                now = time.monotonic()
                self.cooldown_until = max(self.cooldown_until, now + delay)
                raise MusicBrainzUnavailable(response.status_code, self.cooldown_until - now)
            if response.status_code == 404:
                self.backoff = 0.0
                self.last_unavailable_status = None
                result: dict[str, Any] = {"status": "not found"}
            elif 300 <= response.status_code < 400:
                raise EnrichmentError("MusicBrainz identifier redirected; verification required")
            else:
                response.raise_for_status()
                self.backoff = 0.0
                self.last_unavailable_status = None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 2_000_000:
                        raise EnrichmentError("MusicBrainz response exceeds size limit")
                    body.extend(chunk)
                try:
                    document = await run_sync(json.loads, body)
                    if not isinstance(document, dict):
                        raise ValueError
                    if kind == "artwork":
                        result = self.parse_artwork(document, offset=page_offset)
                    elif kind == "recording":
                        result = {"status": "resolved", "recording": self.parse_recording(document)}
                    elif kind in ("search", "batch"):
                        recordings = document.get("recordings")
                        count, offset = document.get("count"), document.get("offset")
                        if (
                            not isinstance(recordings, list)
                            or len(recordings) > (100 if kind == "batch" else 25)
                            or type(count) is not int
                            or count < len(recordings)
                            or type(offset) is not int
                            or offset != 0
                        ):
                            raise ValueError
                        result = {
                            "status": "resolved" if recordings else "not found",
                            "recordings": [self.parse_recording(item) for item in recordings],
                            "complete": count == len(recordings),
                        }
                    else:
                        response_isrc = document.get("isrc")
                        if (
                            not isinstance(response_isrc, str)
                            or response_isrc.upper() != identifier
                        ):
                            raise ValueError
                        recordings = document.get("recordings")
                        if not isinstance(recordings, list) or len(recordings) > 1000:
                            raise ValueError
                        has_count = "recording-count" in document
                        has_offset = "recording-offset" in document
                        count = document.get("recording-count")
                        offset = document.get("recording-offset", 0)
                        if (
                            (has_count and (type(count) is not int or count < 0))
                            or type(offset) is not int
                            or offset < 0
                            or (type(count) is int and offset + len(recordings) > count)
                        ):
                            raise ValueError
                        complete = (
                            count == len(recordings) and offset == 0
                            if has_count
                            else not has_offset
                        )
                        result = {
                            "status": "not found" if complete and not recordings else "resolved",
                            "recordings": [self.parse_recording(item) for item in recordings],
                            "complete": complete,
                        }
                except (
                    ValueError,
                    TypeError,
                    AttributeError,
                    KeyError,
                    UnicodeError,
                    RecursionError,
                ) as err:
                    raise EnrichmentError("Invalid MusicBrainz response") from err
        await run_sync(self.store.cache_enrichment, key, result)
        return result
