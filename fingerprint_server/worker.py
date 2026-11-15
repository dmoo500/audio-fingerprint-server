"""Validate queued metadata and publish references with optional MusicBrainz verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError

from fingerprint_server.async_utils import run_sync
from fingerprint_server.schemas import (
    ISRC_PATTERN,
    Metadata,
    Submission,
    decode_submission,
    metadata_complete,
)
from fingerprint_server.settings import Settings
from fingerprint_server.store import MUSICBRAINZ_VERIFICATION_VERSION, Store

LOGGER = logging.getLogger(__name__)
PUBLICATION_LOGGER = logging.getLogger("uvicorn.error.fingerprint.worker")


def _present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(item, str) and item.strip() for item in value)
    return bool(value)


class EnrichmentError(Exception):
    """Unexpected or oversized metadata lookup response."""


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


class Worker:
    """Validate durable submissions before atomically publishing reference landmarks."""

    def __init__(self, store: Store, settings: Settings, client: httpx.AsyncClient) -> None:
        self.store = store
        self.settings = settings
        self.client = client
        self.wakeup = asyncio.Event()
        self.next_lookup = 0.0
        self.lookup_lock = asyncio.Lock()
        self.cooldown_until = 0.0
        self.backoff = 0.0
        self.lookup_call = "-"

    async def _wait_until(self, deadline: float) -> None:
        while (delay := max(deadline, self.cooldown_until) - time.monotonic()) > 0:
            await asyncio.sleep(delay)

    async def run(self) -> None:
        """Process pending work; cancelled processing is recovered at the next startup."""
        while True:
            self.wakeup.clear()
            await self._wait_until(0)
            row = await run_sync(self.store.claim)
            if row is None:
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=30)
                except TimeoutError:
                    pass
                continue
            self.lookup_call = "-"
            try:
                submission = await run_sync(decode_submission, row["document"])
                metadata = submission.metadata.model_dump(exclude_none=True)
                if self.settings.musicbrainz_enabled:
                    reference_id = row["reference_id"] or self.reference_id(submission)
                    try:
                        old = await run_sync(self.store.reference_metadata, reference_id)
                    except ValueError as err:
                        raise EnrichmentError("Retained reference metadata is invalid") from err
                    if old:
                        try:
                            Metadata.model_validate(
                                {key: old[key] for key in Metadata.model_fields if key in old}
                            )
                        except ValidationError as err:
                            raise EnrichmentError("Retained reference metadata is invalid") from err
                    if metadata.get("recording_mbid") and old.get("recording_mbid"):
                        if UUID(metadata["recording_mbid"]) != UUID(old["recording_mbid"]):
                            raise EnrichmentError("Conflicting retained recording identifier")
                    old_isrc = old.get("isrc")
                    new_isrc = metadata.get("isrc")
                    if old_isrc and new_isrc and old_isrc.upper() != new_isrc.upper():
                        old_candidate = dict(old)
                        if not old_candidate.get("recording_mbid") and metadata.get(
                            "recording_mbid"
                        ):
                            old_candidate["recording_mbid"] = metadata["recording_mbid"]
                        old_verified = await self.verify(old_candidate)
                        new_candidate = dict(metadata)
                        if not new_candidate.get("recording_mbid") and old.get("recording_mbid"):
                            new_candidate["recording_mbid"] = old["recording_mbid"]
                        new_verified = await self.verify(new_candidate)
                        if old_verified["recording_mbid"] != new_verified["recording_mbid"]:
                            raise EnrichmentError("Conflicting retained recording identifiers")
                    for key in ("title", "artists", "album", "duration", "isrc", "recording_mbid"):
                        if not _present(metadata.get(key)) and _present(old.get(key)):
                            metadata[key] = old[key]
                    metadata = await self.verify(metadata)
                    for key in ("title", "artists", "album", "duration", "isrc", "recording_mbid"):
                        value = old.get(key)
                        if _present(value):
                            metadata[key] = value
                    validated = submission.model_dump()
                    validated["metadata"] = metadata
                    try:
                        await run_sync(Submission.model_validate, validated)
                    except ValidationError as err:
                        raise EnrichmentError(
                            "Enriched metadata conflicts with fingerprint coverage"
                        ) from err
                    for key, value in old.items():
                        if key not in validated["metadata"]:
                            metadata[key] = value
                if not metadata_complete(metadata):
                    reason = "Incomplete metadata"
                    if self.settings.musicbrainz_enabled:
                        missing = [
                            key
                            for key in ("title", "artists", "album", "duration")
                            if not _present(metadata.get(key))
                        ]
                        reason += " after MusicBrainz verification: " + ", ".join(missing)
                    await run_sync(self.store.pending_metadata, row["id"], reason)
                    continue
                await run_sync(self.publish, row["id"], submission, metadata, row["reference_id"])
                if self.settings.musicbrainz_enabled:
                    PUBLICATION_LOGGER.info(
                        "Submission %s verified with MusicBrainz and published", row["id"]
                    )
            except MusicBrainzUnavailable as err:
                delay = max(0.0, self.cooldown_until - time.monotonic())
                LOGGER.warning(
                    "MusicBrainz service cooldown: HTTP status=%s; retrying in %s seconds; "
                    "submission=%s; attempt preserved; api_call=%s",
                    err.status,
                    math.ceil(delay),
                    row["id"],
                    self.lookup_call,
                )
                await run_sync(self.store.defer_musicbrainz, row["id"], delay)
            except EnrichmentError as err:
                LOGGER.warning(
                    "Submission %s metadata verification pending: %s; api_call=%s",
                    row["id"],
                    err,
                    self.lookup_call,
                )
                await run_sync(self.store.pending_metadata, row["id"], str(err))
            except (ValidationError, ValueError) as err:
                LOGGER.error("Submission %s failed validation: %s", row["id"], type(err).__name__)
                await run_sync(
                    self.store.fail, row["id"], "Invalid reference or metadata response", None
                )
            except (httpx.HTTPError, TimeoutError) as err:
                retry = min(300 * row["attempts"], 3600) if row["attempts"] < 3 else None
                status = (
                    str(err.response.status_code)
                    if isinstance(err, httpx.HTTPStatusError)
                    else "unavailable"
                )
                LOGGER.warning(
                    "Submission %s metadata lookup failed: %s; HTTP status=%s; "
                    "attempt=%s; retry_seconds=%s; api_call=%s",
                    row["id"],
                    type(err).__name__,
                    status,
                    row["attempts"],
                    retry,
                    self.lookup_call,
                )
                await run_sync(self.store.fail, row["id"], "Metadata lookup unavailable", retry)

    def publish(
        self,
        submission_id: str,
        submission: Submission,
        metadata: dict[str, Any],
        reference_id: str | None = None,
    ) -> None:
        """Deduplicate exact acoustic references without overwriting conflicting metadata."""
        self.store.finish(
            submission_id,
            reference_id or self.reference_id(submission),
            metadata,
            submission.fingerprints,
            verified=self.settings.musicbrainz_enabled,
        )

    @staticmethod
    def reference_id(submission: Submission) -> str:
        """Return the unchanged acoustic identity of a submission."""
        identity = {
            "format_id": submission.format_id,
            "windows": [window.model_dump() for window in submission.windows],
            "fingerprints": sorted(submission.fingerprints),
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def verify(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Verify supplied identifiers and fill only missing, unambiguous fields."""
        mbid = metadata.get("recording_mbid")
        isrc = metadata.get("isrc")
        if not mbid and not isrc:
            raise EnrichmentError("MusicBrainz identifier unavailable")
        recording: dict[str, Any] | None = None
        if mbid:
            canonical = str(UUID(mbid))
            response = await self.lookup("recording", canonical)
            if response["status"] != "resolved":
                raise EnrichmentError(f"MusicBrainz recording {response['status']}")
            recording = response["recording"]
            if recording["id"] != canonical:
                raise EnrichmentError("MusicBrainz recording identifier conflict")
        if isrc:
            normalized = isrc.upper()
            response = await self.lookup("isrc", normalized)
            if response["status"] != "resolved":
                raise EnrichmentError(f"MusicBrainz ISRC {response['status']}")
            candidates = response["recordings"]
            if recording:
                if recording["isrcs"] is None:
                    raise EnrichmentError("MusicBrainz recording ISRCs unavailable")
                if normalized not in recording["isrcs"]:
                    raise EnrichmentError("MusicBrainz recording and ISRC conflict")
                if not any(item["id"] == recording["id"] for item in candidates):
                    if not response["complete"]:
                        raise EnrichmentError("MusicBrainz ISRC recording list incomplete")
                    raise EnrichmentError("MusicBrainz recording and ISRC conflict")
            elif not response["complete"] or len(candidates) != 1:
                raise EnrichmentError("MusicBrainz ISRC ambiguous")
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
        """Perform a rate-limited lookup using versioned, endpoint-specific cache entries."""
        endpoint = hashlib.sha256(
            str(self.settings.musicbrainz_base_url).rstrip("/").encode()
        ).hexdigest()
        key = f"musicbrainz:v{MUSICBRAINZ_VERIFICATION_VERSION}:{endpoint}:{kind}:{identifier}"
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
                minimum = 120.0 if response.status_code == 503 else 30.0
                self.backoff = min(900.0, max(minimum, self.backoff * 2))
                delay = max(self.backoff, _retry_after(response.headers.get("Retry-After")))
                now = time.monotonic()
                self.cooldown_until = max(self.cooldown_until, now + delay)
                raise MusicBrainzUnavailable(response.status_code, self.cooldown_until - now)
            if response.status_code == 404:
                self.backoff = 0.0
                result: dict[str, Any] = {"status": "not found"}
            elif 300 <= response.status_code < 400:
                raise EnrichmentError("MusicBrainz identifier redirected; verification required")
            else:
                response.raise_for_status()
                self.backoff = 0.0
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
                        if document.get("isrc", "").upper() != identifier:
                            raise ValueError
                        recordings = document.get("recordings")
                        if not isinstance(recordings, list) or len(recordings) > 1000:
                            raise ValueError
                        count = document.get("recording-count")
                        offset = document.get("recording-offset", 0)
                        if (
                            (count is not None and (type(count) is not int or count < 0))
                            or type(offset) is not int
                            or offset < 0
                            or (count is not None and offset + len(recordings) > count)
                        ):
                            raise ValueError
                        result = {
                            "status": "not found" if count == 0 and not recordings else "resolved",
                            "recordings": [self.parse_recording(item) for item in recordings],
                            "complete": count == len(recordings) and offset == 0,
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
        credits = value.get("artist-credit", [])
        if not isinstance(credits, list) or len(credits) > 30:
            raise ValueError("Invalid artists")
        artists = []
        for credit in credits:
            if not isinstance(credit, dict):
                raise ValueError("Invalid artist")
            name = credit.get("name") or (credit.get("artist") or {}).get("name")
            if not isinstance(name, str) or len(name) > 500:
                raise ValueError("Invalid artist name")
            if name.strip():
                artists.append(name)
        result["artists"] = artists
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
