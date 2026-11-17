"""Rate-limited MusicBrainz lookups, response validation and identifier verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import unicodedata
from datetime import UTC
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

import httpx

from fingerprint_server.async_utils import run_sync
from fingerprint_server.schemas import ISRC_PATTERN
from fingerprint_server.settings import Settings
from fingerprint_server.store import Store

LOGGER = logging.getLogger(__name__)
MUSICBRAINZ_CACHE_VERSION = 3


def _matches_text(left: str, right: str) -> bool:
    """Compare names using a lightweight approximation of MA's loose comparison."""
    left, right = left.strip().lower(), right.strip().lower()
    if not left or not right or abs(len(left) - len(right)) > 4:
        return False
    left = left.replace(" & ", " and ")
    right = right.replace(" & ", " and ")
    normalized_left = "".join(
        char for char in unicodedata.normalize("NFKD", left) if char.isalnum()
    )
    normalized_right = "".join(
        char for char in unicodedata.normalize("NFKD", right) if char.isalnum()
    )
    if normalized_left == normalized_right:
        return True
    accuracy = 0.9 if len(left) + len(right) > 18 else 0.8
    return SequenceMatcher(a=left, b=right).ratio() > accuracy


def recording_matches(recording: dict[str, Any], metadata: dict[str, Any]) -> bool:
    """Match a reused ISRC against supplied title, artists and available duration."""
    title = metadata.get("title")
    artists = metadata.get("artists")
    if not isinstance(title, str) or not isinstance(artists, list) or not artists:
        return False
    candidate_title = recording.get("title")
    candidate_artists = recording.get("artist_names", recording.get("artists", []))
    if not isinstance(candidate_title, str) or not _matches_text(title, candidate_title):
        return False
    if not any(
        isinstance(artist, str) and isinstance(candidate, str) and _matches_text(artist, candidate)
        for artist in artists
        for candidate in candidate_artists
    ):
        return False
    length = recording.get("length")
    duration = metadata.get("duration")
    return length is None or duration is None or abs(length - duration * 1000) <= 8000


def _present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(item, str) and item.strip() for item in value)
    return bool(value)


class EnrichmentError(Exception):
    """Malformed or unusable MusicBrainz data that may be retried later."""


class VerificationPending(EnrichmentError):
    """A valid MusicBrainz result cannot yet resolve a unique complete recording."""


class UnresolvableVerification(EnrichmentError):
    """The supplied identifiers are absent or definitively conflict with MusicBrainz."""


class MissingIdentifierVerification(UnresolvableVerification):
    """Neither a recording identifier nor an ISRC is available."""


class MusicBrainzUnavailable(Exception):
    """A MusicBrainz response requiring a service-wide cooldown."""

    def __init__(self, status: int, delay: float) -> None:
        self.status = status
        self.delay = delay
        super().__init__(f"MusicBrainz HTTP {status}; retry in {delay} seconds")


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


class MusicBrainzService:
    """Verify recording identifiers with bounded, cached and globally paced requests."""

    def __init__(self, store: Store, settings: Settings, client: httpx.AsyncClient) -> None:
        self.store = store
        self.settings = settings
        self.client = client
        self.next_lookup = 0.0
        self.lookup_lock = asyncio.Lock()
        self.cooldown_until = 0.0
        self.backoff = 0.0
        self.last_unavailable_status: int | None = None
        self.lookup_call = "-"

    async def wait_for_cooldown(self) -> None:
        """Wait for a service-wide cooldown before the worker claims more work."""
        await self._wait_until(0)

    async def _wait_until(self, deadline: float) -> None:
        while (delay := max(deadline, self.cooldown_until) - time.monotonic()) > 0:
            await asyncio.sleep(delay)

    async def verify(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Verify supplied identifiers and fill only missing, unambiguous fields."""
        mbid = metadata.get("recording_mbid")
        isrc = metadata.get("isrc")
        if not mbid and not isrc:
            raise MissingIdentifierVerification("MusicBrainz identifier unavailable")
        recording: dict[str, Any] | None = None
        if mbid:
            try:
                canonical = str(UUID(mbid))
            except (ValueError, TypeError, AttributeError) as err:
                raise UnresolvableVerification("Invalid MusicBrainz recording identifier") from err
            response = await self.lookup("recording", canonical)
            if response["status"] != "resolved":
                raise VerificationPending(f"MusicBrainz recording {response['status']}")
            recording = response["recording"]
            if recording["id"] != canonical:
                raise UnresolvableVerification("MusicBrainz recording identifier conflict")
        if isrc:
            if not isinstance(isrc, str) or re.fullmatch(ISRC_PATTERN, isrc) is None:
                raise UnresolvableVerification("Invalid MusicBrainz ISRC")
            normalized = isrc.upper()
            response = await self.lookup("isrc", normalized)
            if response["status"] != "resolved":
                raise VerificationPending(f"MusicBrainz ISRC {response['status']}")
            candidates = response["recordings"]
            if recording:
                if recording["isrcs"] is None:
                    raise VerificationPending("MusicBrainz recording ISRCs unavailable")
                if normalized not in recording["isrcs"]:
                    raise UnresolvableVerification("MusicBrainz recording and ISRC conflict")
                if not any(item["id"] == recording["id"] for item in candidates):
                    if not response["complete"]:
                        raise VerificationPending("MusicBrainz ISRC recording list incomplete")
                    raise UnresolvableVerification("MusicBrainz recording and ISRC conflict")
            elif not response["complete"] or not candidates:
                raise VerificationPending("MusicBrainz ISRC ambiguous")
            elif len(candidates) > 1:
                recording = next(
                    (item for item in candidates if recording_matches(item, metadata)), None
                )
                if recording is None:
                    LOGGER.info(
                        "MusicBrainz confirmed ISRC %s with %s candidate recordings; "
                        "retaining supplied metadata without assigning a recording MBID",
                        normalized,
                        len(candidates),
                    )
                    return dict(metadata)
            else:
                recording = candidates[0]
        assert recording is not None
        result = dict(metadata)
        for field in ("title", "artists", "album", "recording_mbid"):
            if not _present(result.get(field)):
                value = recording["id"] if field == "recording_mbid" else recording.get(field)
                if value:
                    result[field] = value
        isrcs = recording["isrcs"]
        if not result.get("isrc") and isrcs and len(set(isrcs)) == 1:
            result["isrc"] = isrcs[0]
        return result

    async def lookup(self, kind: str, identifier: str) -> dict[str, Any]:
        """Fetch and cache a bounded, typed identifier lookup."""
        async with self.lookup_lock:
            self.lookup_call = f"GET /{kind}/{identifier}"
            return await self._lookup(kind, identifier)

    async def _lookup(self, kind: str, identifier: str) -> dict[str, Any]:
        """Perform a rate-limited lookup using a parser-versioned cache namespace."""
        endpoint = hashlib.sha256(
            str(self.settings.musicbrainz_base_url).rstrip("/").encode()
        ).hexdigest()
        key = f"musicbrainz:v{MUSICBRAINZ_CACHE_VERSION}:{endpoint}:{kind}:{identifier}"
        if (cached := await run_sync(self.store.cached_enrichment, key)) is not None:
            return cached
        await self._wait_until(self.next_lookup)
        self.next_lookup = time.monotonic() + 1.1
        async with self.client.stream(
            "GET",
            f"{str(self.settings.musicbrainz_base_url).rstrip('/')}/{kind}/{identifier}",
            params={
                "inc": "artists+isrcs+releases" if kind == "recording" else "artists+releases",
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
                    if kind == "recording":
                        result = {"status": "resolved", "recording": self.parse_recording(document)}
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

    @staticmethod
    def parse_recording(value: Any) -> dict[str, Any]:
        """Extract only validated recording data from a MusicBrainz response."""
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise ValueError("Invalid recording")
        result: dict[str, Any] = {"id": str(UUID(value["id"]))}
        title = value.get("title")
        if title is not None:
            if not isinstance(title, str) or len(title) > 500:
                raise ValueError("Invalid recording title")
            result["title"] = title
        length = value.get("length")
        if length is not None:
            if type(length) is not int or length < 0:
                raise ValueError("Invalid recording length")
            result["length"] = length
        credits = value.get("artist-credit", [])
        if not isinstance(credits, list) or len(credits) > 30:
            raise ValueError("Invalid artists")
        artists = []
        artist_names = []
        for credit in credits:
            if not isinstance(credit, dict):
                raise ValueError("Invalid artist")
            name = credit.get("name") or (credit.get("artist") or {}).get("name")
            if not isinstance(name, str) or len(name) > 500:
                raise ValueError("Invalid artist name")
            if name.strip():
                artists.append(name)
                artist_names.append(name)
            artist = credit.get("artist") or {}
            if not isinstance(artist, dict):
                raise ValueError("Invalid artist")
            canonical = artist.get("name")
            if isinstance(canonical, str) and canonical.strip():
                artist_names.append(canonical)
            aliases = artist.get("aliases", [])
            if not isinstance(aliases, list) or len(aliases) > 100:
                raise ValueError("Invalid artist aliases")
            for alias in aliases:
                if not isinstance(alias, dict) or not isinstance(alias.get("name"), str):
                    raise ValueError("Invalid artist alias")
                if len(alias["name"]) > 500:
                    raise ValueError("Invalid artist alias")
                artist_names.append(alias["name"])
        result["artists"] = artists
        result["artist_names"] = artist_names
        releases = value.get("releases", [])
        if not isinstance(releases, list) or len(releases) > 1000:
            raise ValueError("Invalid releases")
        albums = set()
        for release in releases:
            if not isinstance(release, dict) or not isinstance(release.get("title"), str):
                raise ValueError("Invalid release")
            album = release["title"]
            if len(album) > 500:
                raise ValueError("Invalid release title")
            if album.strip():
                albums.add(album)
        release_count = value.get("release-count")
        release_offset = value.get("release-offset", 0)
        if (
            len(albums) == 1
            and type(release_count) is int
            and release_count == len(releases)
            and type(release_offset) is int
            and release_offset == 0
        ):
            result["album"] = albums.pop()
        isrcs = value.get("isrcs")
        if isrcs is not None and (
            not isinstance(isrcs, list)
            or len(isrcs) > 1000
            or any(
                not isinstance(code, str) or re.fullmatch(ISRC_PATTERN, code) is None
                for code in isrcs
            )
        ):
            raise ValueError("Invalid ISRC list")
        result["isrcs"] = [code.upper() for code in isrcs] if isrcs is not None else None
        return result
