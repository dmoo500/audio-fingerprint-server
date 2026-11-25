"""Bounded TheAudioDB candidate searches for MusicBrainz verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

import aiohttp
from pydantic import SecretStr

from fingerprint_server.async_utils import run_sync
from fingerprint_server.database import Storage
from fingerprint_server.http_utils import retry_after

LOGGER = logging.getLogger(__name__)
TRACK_FIELDS = ("strTrack", "strArtist", "strAlbum", "intDuration", "strMusicBrainzID", "strISRC")


def _parse_tracks(value: object) -> list[dict[str, Any]]:
    if (
        not isinstance(value, list)
        or len(value) > 100
        or any(not isinstance(item, dict) for item in value)
    ):
        raise ValueError
    result = [{field: item.get(field) for field in TRACK_FIELDS} for item in value]
    for item in result:
        for field, field_value in item.items():
            if field_value is None:
                continue
            if field == "intDuration" and type(field_value) is int:
                continue
            if not isinstance(field_value, str) or len(field_value) > 500:
                raise ValueError
    return result


class AudioDBUnavailable(Exception):
    """A TheAudioDB lookup must be deferred without consuming a pending check."""

    def __init__(self, reason: str, delay: float) -> None:
        super().__init__(reason)
        self.delay = delay


class AudioDBClient:
    """Search tracks with a shared cache, request pacing and upstream cooldown."""

    def __init__(self, store: Storage, key: SecretStr, session: aiohttp.ClientSession) -> None:
        self.store = store
        self.key = key
        self.session = session
        self.lock = asyncio.Lock()
        self.next_lookup = 0.0
        self.cooldown_until = 0.0

    def _unavailable(self, reason: str, delay: float) -> AudioDBUnavailable:
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)
        return AudioDBUnavailable(reason, delay)

    async def search(self, artist: str, title: str) -> list[dict[str, Any]]:
        """Return cached or freshly retrieved track candidates."""
        query = json.dumps([artist, title], ensure_ascii=True, separators=(",", ":"))
        key = f"theaudiodb:v1:search:{hashlib.sha256(query.encode()).hexdigest()}"
        async with self.lock:
            try:
                cached = await run_sync(self.store.cached_enrichment, key)
            except json.JSONDecodeError:
                LOGGER.warning("Invalid TheAudioDB cache entry; refreshing")
                cached = None
            if cached is not None:
                try:
                    if not isinstance(cached, dict) or "tracks" not in cached:
                        raise ValueError
                    return _parse_tracks(cached["tracks"])
                except ValueError:
                    LOGGER.warning("Invalid TheAudioDB cache entry; refreshing")
            while (delay := max(self.next_lookup, self.cooldown_until) - time.monotonic()) > 0:
                await asyncio.sleep(delay)
            self.next_lookup = time.monotonic() + 2.1
            try:
                async with self.session.get(
                    f"https://www.theaudiodb.com/api/v1/json/{self.key.get_secret_value()}"
                    "/searchtrack.php",
                    params={"s": artist, "t": title},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        delay = max(
                            60 if response.status in (429, 502, 503) else 3600,
                            retry_after(response.headers.get("Retry-After")),
                        )
                        raise self._unavailable(f"TheAudioDB HTTP {response.status}", delay)
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if len(body) + len(chunk) > 2_000_000:
                            raise self._unavailable("TheAudioDB response exceeds size limit", 3600)
                        body.extend(chunk)
            except (aiohttp.ClientError, TimeoutError):
                raise self._unavailable("TheAudioDB request unavailable", 300) from None
            try:
                document = json.loads(body)
                if not isinstance(document, dict):
                    raise ValueError
                if "error" in document:
                    raise self._unavailable("TheAudioDB returned an API error", 60)
                tracks = document["track"]
                if tracks is None:
                    tracks = []
                result = _parse_tracks(tracks)
            except (KeyError, ValueError, UnicodeError, RecursionError):
                raise self._unavailable("Invalid TheAudioDB response", 3600) from None
            await run_sync(self.store.cache_enrichment, key, {"tracks": result})
            return result
