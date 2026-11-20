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
from datetime import UTC, date
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

import httpx

from fingerprint_server.async_utils import run_sync
from fingerprint_server.database import Storage
from fingerprint_server.schemas import ISRC_PATTERN, metadata_searchable
from fingerprint_server.settings import Settings

LOGGER = logging.getLogger(__name__)
MUSICBRAINZ_CACHE_VERSION = 7


def _official_album_date(release: dict[str, Any]) -> str | None:
    """Return a known release date for an official album linked to the recording."""
    group = release.get("release-group")
    if (
        release.get("status") != "Official"
        or not isinstance(group, dict)
        or group.get("primary-type") != "Album"
    ):
        return None
    value = release.get("date")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}){0,2}", value):
        return None
    try:
        date.fromisoformat(value + "-01" * (2 - value.count("-")))
    except ValueError:
        return None
    return value


def _normalized_name(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", value.lower()) if char.isalnum())


def _release_evidence(recording: dict[str, Any], metadata: dict[str, Any]) -> bool:
    """Match the supplied release and group against the same MusicBrainz release."""
    release = str(UUID(value)) if (value := metadata.get("release_mbid")) else None
    group = str(UUID(value)) if (value := metadata.get("release_group_mbid")) else None
    return any(
        (not release or release == release_id) and (not group or group == group_id)
        for release_id, group_id in recording["release_ids"]
    )


def _metadata_matches(
    recording: dict[str, Any], metadata: dict[str, Any], *, contextual: bool = False
) -> bool:
    """Require exact normalized credits/title, explicit duration and no version ambiguity."""
    title = metadata.get("title")
    artists = metadata.get("artists")
    duration = metadata.get("duration")
    length = recording.get("length")
    credits = recording.get("artists", [])
    if (
        not isinstance(title, str)
        or not _normalized_name(title)
        or not isinstance(artists, list)
        or not artists
        or any(not isinstance(artist, str) or not _normalized_name(artist) for artist in artists)
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
        or type(length) is not int
        or abs(length - duration * 1000) > 2000
    ):
        return False
    version = recording.get("disambiguation", "")
    if contextual:
        expected = {_normalized_name(artist) for artist in artists}
        alternatives = recording["credit_names"]
        if len(artists) != len(alternatives) or not all(
            _normalized_name(artist) in {_normalized_name(name) for name in names}
            for artist, names in zip(artists, alternatives, strict=True)
        ):
            if expected != {_normalized_name(name) for name in credits}:
                return False
        candidate_title = recording.get("title", "")
        return bool(candidate_title) and _normalized_name(title) == _normalized_name(
            f"{candidate_title} {version}"
            if version and not _normalized_name(candidate_title).endswith(_normalized_name(version))
            else candidate_title
        )
    if version != "":
        return False
    return _normalized_name(title) == _normalized_name(recording.get("title", "")) and {
        _normalized_name(artist) for artist in artists
    } == {_normalized_name(artist) for artist in credits}


def _search_phrase(value: str) -> str:
    return '"' + re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", value) + '"'


def _matches_text(left: str, right: str) -> bool:
    """Compare names using a lightweight approximation of MA's loose comparison."""
    left, right = left.strip().lower(), right.strip().lower()
    if not left or not right or abs(len(left) - len(right)) > 4:
        return False
    left = left.replace(" & ", " and ")
    right = right.replace(" & ", " and ")
    normalized_left = _normalized_name(left)
    normalized_right = _normalized_name(right)
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


class RecordingReviewRequired(VerificationPending):
    """Multiple recordings matched the submission metadata."""

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        super().__init__("MusicBrainz recording requires manual selection")
        self.candidates = candidates


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

    async def prefetch(self, rows: list[dict[str, Any]]) -> None:
        """Fetch positive identifier evidence for up to twenty eligible submissions."""
        identifiers: set[tuple[str, str]] = set()
        for row in rows[:20]:
            try:
                metadata = json.loads(row["document"]).get("metadata", {})
                if not isinstance(metadata, dict):
                    continue
                mbid, isrc = metadata.get("recording_mbid"), metadata.get("isrc")
                if isinstance(mbid, str):
                    identifiers.add(("rid", str(UUID(mbid))))
                if isinstance(isrc, str) and re.fullmatch(ISRC_PATTERN, isrc):
                    identifiers.add(("isrc", isrc.upper()))
            except (ValueError, TypeError, AttributeError):
                # The ordinary per-submission parser reports malformed documents.
                continue
        if len(rows) < 2 or len(identifiers) < 2:
            self.batch_recordings.clear()
            self.batch_isrcs.clear()
            self.batch_candidates.clear()
            self.batch_submissions = {row["id"] for row in rows[:20]}
            return
        query = " OR ".join(
            f"{kind}:{_search_phrase(value)}" for kind, value in sorted(identifiers)
        )
        response = await self.lookup("batch", query)
        self.batch_recordings.clear()
        self.batch_isrcs.clear()
        self.batch_candidates.clear()
        self.batch_submissions = {row["id"] for row in rows[:20]}
        if response["status"] != "resolved" or not response["complete"]:
            LOGGER.info(
                "MusicBrainz batch: submissions=%s; identifiers=%s; recordings=%s; "
                "complete=%s; using direct lookups",
                len(self.batch_submissions),
                len(identifiers),
                len(response.get("recordings", [])),
                response.get("complete", False),
            )
            return
        for recording in response["recordings"]:
            self.batch_candidates[recording["id"]] = recording
            if ("rid", recording["id"]) in identifiers and recording["isrcs"] is not None:
                self.batch_recordings[recording["id"]] = recording
            for isrc in recording["isrcs"] or []:
                if ("isrc", isrc) in identifiers:
                    self.batch_isrcs.setdefault(isrc, []).append(recording)
        LOGGER.info(
            "MusicBrainz batch: submissions=%s; identifiers=%s; candidates=%s; "
            "recording_evidence=%s; isrc_evidence=%s; "
            "complete=True; candidates without ISRC evidence need recording lookups",
            len(self.batch_submissions),
            len(identifiers),
            len(self.batch_candidates),
            len(self.batch_recordings),
            len(self.batch_isrcs),
        )

    async def wait_for_cooldown(self) -> None:
        """Wait for a service-wide cooldown before the worker claims more work."""
        await self._wait_until(0)

    async def _wait_until(self, deadline: float) -> None:
        while (delay := max(deadline, self.cooldown_until) - time.monotonic()) > 0:
            await asyncio.sleep(delay)

    async def verify(
        self, metadata: dict[str, Any], *, selected_mbid: str | None = None
    ) -> dict[str, Any]:
        """Verify supplied identifiers and fill only missing, unambiguous fields."""
        mbid = selected_mbid or metadata.get("recording_mbid")
        isrc = metadata.get("isrc")
        recording: dict[str, Any] | None = None
        if mbid:
            try:
                canonical = str(UUID(mbid))
            except (ValueError, TypeError, AttributeError) as err:
                raise UnresolvableVerification("Invalid MusicBrainz recording identifier") from err
            if not metadata.get("album") and not self.batch_recordings.get(canonical, {}).get(
                "album"
            ):
                self.batch_recordings.pop(canonical, None)
            response = await self.lookup("recording", canonical)
            if response["status"] != "resolved":
                raise VerificationPending(f"MusicBrainz recording {response['status']}")
            recording = response["recording"]
            if recording["id"] != canonical:
                raise UnresolvableVerification("MusicBrainz recording identifier conflict")
            if selected_mbid and not _metadata_matches(recording, metadata, contextual=True):
                raise VerificationPending("Selected MusicBrainz recording no longer matches")
        if isrc:
            if not isinstance(isrc, str) or re.fullmatch(ISRC_PATTERN, isrc) is None:
                raise UnresolvableVerification("Invalid MusicBrainz ISRC")
            normalized = isrc.upper()
            if recording is None:
                if normalized not in self.batch_isrcs:
                    await self._hydrate_batch_candidate(metadata, normalized)
                candidates = {
                    item["id"]: item
                    for item in self.batch_isrcs.get(normalized, [])
                    if _metadata_matches(item, metadata)
                }
                if len(candidates) == 1 and (
                    metadata.get("album") or next(iter(candidates.values())).get("album")
                ):
                    candidate = next(iter(candidates.values()))
                    # The exact association is positive evidence, not proof of an exhaustive list.
                    LOGGER.info(
                        "MusicBrainz ISRC %s resolved by batch association and exact metadata",
                        normalized,
                    )
                    result = dict(metadata)
                    for field in ("title", "artists", "album", "recording_mbid"):
                        value = (
                            candidate["id"] if field == "recording_mbid" else candidate.get(field)
                        )
                        if not _present(result.get(field)) and value:
                            result[field] = value
                    return result
            response = await self.lookup("isrc", normalized)
            if response["status"] != "resolved":
                if response["status"] != "not found":
                    raise VerificationPending(f"MusicBrainz ISRC {response['status']}")
                if recording is None:
                    recording = await self._resolve_metadata(metadata)
                elif not _metadata_matches(recording, metadata, contextual=True):
                    raise VerificationPending("MusicBrainz ISRC not found; metadata unconfirmed")
                LOGGER.info(
                    "MusicBrainz recording %s resolved by exact metadata; "
                    "ISRC association remains unconfirmed",
                    recording["id"],
                )
            elif recording:
                candidates = response["recordings"]
                if recording["isrcs"] is None:
                    raise VerificationPending("MusicBrainz recording ISRCs unavailable")
                if normalized not in recording["isrcs"]:
                    raise UnresolvableVerification("MusicBrainz recording and ISRC conflict")
                if not any(item["id"] == recording["id"] for item in candidates):
                    if not response["complete"]:
                        raise VerificationPending("MusicBrainz ISRC recording list incomplete")
                    raise UnresolvableVerification("MusicBrainz recording and ISRC conflict")
            elif not response["complete"] or not response["recordings"]:
                raise VerificationPending("MusicBrainz ISRC ambiguous")
            elif len(response["recordings"]) > 1:
                candidates = response["recordings"]
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
                recording = response["recordings"][0]
        if recording is None:
            recording = await self._resolve_metadata(metadata)
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

    async def _hydrate_batch_candidate(self, metadata: dict[str, Any], isrc: str) -> None:
        """Confirm a unique strict batch candidate's ISRC through a recording lookup."""
        candidates = [
            item for item in self.batch_candidates.values() if _metadata_matches(item, metadata)
        ]
        if len(candidates) != 1:
            return
        identifier = candidates[0]["id"]
        response = await self.lookup("recording", identifier)
        if response["status"] == "resolved":
            recording = response["recording"]
            if (
                recording["id"] == identifier
                and isrc in (recording["isrcs"] or [])
                and _metadata_matches(recording, metadata)
                and (metadata.get("album") or recording.get("album"))
            ):
                self.batch_recordings[identifier] = recording
                self.batch_isrcs[isrc] = [recording]
                LOGGER.info(
                    "MusicBrainz batch candidate %s confirmed for ISRC %s by recording lookup",
                    identifier,
                    isrc,
                )
                return
        LOGGER.info(
            "MusicBrainz batch candidate %s did not confirm ISRC %s and strict metadata; "
            "using direct ISRC lookup",
            identifier,
            isrc,
        )

    async def _resolve_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Resolve exactly one complete metadata candidate, then verify its recording lookup."""
        title, artists = metadata.get("title"), metadata.get("artists")
        if not metadata_searchable(metadata):
            if not metadata.get("isrc") and not metadata.get("recording_mbid"):
                raise MissingIdentifierVerification("MusicBrainz identifier unavailable")
            raise VerificationPending("MusicBrainz ISRC not found; search metadata incomplete")
        assert isinstance(title, str) and isinstance(artists, list)
        title_clause = f"recording:{_search_phrase(title)}"
        if versioned := re.fullmatch(r"(.+?)\s+\([^()]+\)", title):
            title_clause = f"({title_clause} OR recording:{_search_phrase(versioned[1])})"
        query = " AND ".join(
            [title_clause] + [f"artist:{_search_phrase(artist)}" for artist in artists]
        )
        scope = [
            f"{field}:{UUID(value)}"
            for key, field in (("release_mbid", "reid"), ("release_group_mbid", "rgid"))
            if (value := metadata.get(key))
        ]
        if scope:
            # Search responses can omit releases without saying the list is incomplete.
            scoped = await self.lookup("search", " AND ".join([query, *scope]))
            if scoped["status"] == "resolved" and scoped["complete"]:
                matches = {
                    item["id"]
                    for item in scoped["recordings"]
                    if _metadata_matches(item, metadata, contextual=True)
                }
                if len(matches) == 1:
                    identifier = next(iter(matches))
                    hydrated = await self.lookup("recording", identifier)
                    if (
                        hydrated["status"] == "resolved"
                        and hydrated["recording"]["id"] == identifier
                        and _metadata_matches(hydrated["recording"], metadata, contextual=True)
                        and _release_evidence(hydrated["recording"], metadata)
                    ):
                        LOGGER.info(
                            "MusicBrainz recording %s resolved by release scope", identifier
                        )
                        return hydrated["recording"]
        response = await self.lookup("search", query)
        if response["status"] != "resolved" or not response["complete"]:
            raise VerificationPending("MusicBrainz metadata search missing or incomplete")
        candidates = {
            candidate["id"]: candidate
            for candidate in response["recordings"]
            if _metadata_matches(candidate, metadata, contextual=True)
        }
        if not candidates:
            raise VerificationPending("MusicBrainz metadata search found no matching recording")
        album = metadata.get("album")
        has_release_hint = bool(metadata.get("release_mbid") or metadata.get("release_group_mbid"))
        album_candidates = {
            key: candidate
            for key, candidate in candidates.items()
            if (
                _release_evidence(candidate, metadata)
                if has_release_hint
                else album and any(_matches_text(album, name) for name in candidate["albums"])
            )
        }
        # A partial release list cannot rule out a competing recording on the same album.
        use_album = bool(album_candidates) and all(
            candidate["albums_complete"] for candidate in candidates.values()
        )
        if use_album:
            candidates = album_candidates
        if len(candidates) > 1:
            raise RecordingReviewRequired(
                [
                    {
                        key: candidate.get(key)
                        for key in (
                            "id",
                            "title",
                            "artists",
                            "length",
                            "disambiguation",
                            "albums",
                            "earliest_album",
                        )
                    }
                    for candidate in candidates.values()
                ]
            )
        identifier = next(iter(candidates))
        hydrated = await self.lookup("recording", identifier)
        if (
            hydrated["status"] != "resolved"
            or hydrated["recording"]["id"] != identifier
            or not _metadata_matches(hydrated["recording"], metadata, contextual=True)
            or (
                use_album
                and not (
                    _release_evidence(hydrated["recording"], metadata)
                    if has_release_hint
                    else isinstance(album, str)
                    and any(_matches_text(album, name) for name in hydrated["recording"]["albums"])
                )
            )
        ):
            raise VerificationPending("MusicBrainz metadata search recording not confirmed")
        LOGGER.info("MusicBrainz recording %s resolved by exact metadata", identifier)
        return hydrated["recording"]

    async def lookup(self, kind: str, identifier: str) -> dict[str, Any]:
        """Fetch and cache a bounded, typed identifier lookup."""
        if kind == "recording" and identifier in self.batch_recordings:
            return {"status": "resolved", "recording": self.batch_recordings[identifier]}
        async with self.lookup_lock:
            self.lookup_call = (
                "GET /recording (identifier batch)"
                if kind == "batch"
                else f"GET /recording/{identifier} (artwork)"
                if kind == "artwork"
                else "GET /recording (metadata search)"
                if kind == "search"
                else f"GET /{kind}/{identifier}"
            )
            return await self._lookup(kind, identifier)

    async def _lookup(self, kind: str, identifier: str) -> dict[str, Any]:
        """Perform a rate-limited lookup using a parser-versioned cache namespace."""
        endpoint = hashlib.sha256(
            str(self.settings.musicbrainz_base_url).rstrip("/").encode()
        ).hexdigest()
        cache_identifier = (
            hashlib.sha256(identifier.encode()).hexdigest()
            if kind in ("search", "batch")
            else identifier
        )
        cache_kind = "artwork-v2" if kind == "artwork" else kind
        key = f"musicbrainz:v{MUSICBRAINZ_CACHE_VERSION}:{endpoint}:{cache_kind}:{cache_identifier}"
        if (cached := await run_sync(self.store.cached_enrichment, key)) is not None:
            return cached
        await self._wait_until(self.next_lookup)
        self.next_lookup = time.monotonic() + 1.1
        async with self.client.stream(
            "GET",
            (
                f"{str(self.settings.musicbrainz_base_url).rstrip('/')}/recording/{identifier}"
                if kind == "artwork"
                else f"{str(self.settings.musicbrainz_base_url).rstrip('/')}/recording"
                if kind in ("search", "batch")
                else f"{str(self.settings.musicbrainz_base_url).rstrip('/')}/{kind}/{identifier}"
            ),
            params={"query": identifier, "limit": "100" if kind == "batch" else "25", "fmt": "json"}
            if kind in ("search", "batch")
            else {
                "inc": "releases+release-groups"
                if kind == "artwork"
                else "artists+isrcs+releases+release-groups"
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
                        releases = document.get("releases")
                        if not isinstance(releases, list) or len(releases) > 1000:
                            raise ValueError("Invalid artwork releases")
                        result = {
                            "status": "resolved",
                            "releases": [
                                {
                                    "title": item["title"],
                                    "group": str(UUID(item["release-group"]["id"])),
                                    **(
                                        {"id": str(UUID(item["id"]))}
                                        if isinstance(item.get("id"), str)
                                        else {}
                                    ),
                                    **(
                                        {"group_title": item["release-group"]["title"]}
                                        if isinstance(item["release-group"].get("title"), str)
                                        else {}
                                    ),
                                }
                                for item in releases
                                if isinstance(item.get("title"), str)
                                and isinstance(item.get("release-group"), dict)
                            ],
                        }
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

    @staticmethod
    def parse_recording(value: Any) -> dict[str, Any]:
        """Extract only validated recording data from a MusicBrainz response."""
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise ValueError("Invalid recording")
        result: dict[str, Any] = {"id": str(UUID(value["id"]))}
        disambiguation = value.get("disambiguation", "")
        if not isinstance(disambiguation, str) or len(disambiguation) > 1000:
            raise ValueError("Invalid recording disambiguation")
        result["disambiguation"] = disambiguation
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
        credit_names = []
        for credit in credits:
            if not isinstance(credit, dict):
                raise ValueError("Invalid artist")
            artist = credit.get("artist") or {}
            if not isinstance(artist, dict):
                raise ValueError("Invalid artist")
            name = credit.get("name") or artist.get("name")
            if not isinstance(name, str) or len(name) > 500:
                raise ValueError("Invalid artist name")
            if name.strip():
                artists.append(name)
                artist_names.append(name)
            names = [name]
            canonical = artist.get("name")
            if isinstance(canonical, str) and canonical.strip():
                artist_names.append(canonical)
                names.append(canonical)
            aliases = artist.get("aliases", [])
            if not isinstance(aliases, list) or len(aliases) > 100:
                raise ValueError("Invalid artist aliases")
            for alias in aliases:
                if not isinstance(alias, dict) or not isinstance(alias.get("name"), str):
                    raise ValueError("Invalid artist alias")
                if len(alias["name"]) > 500:
                    raise ValueError("Invalid artist alias")
                artist_names.append(alias["name"])
                names.append(alias["name"])
            if name.strip():
                credit_names.append(names)
        result["artists"] = artists
        result["artist_names"] = artist_names
        result["credit_names"] = credit_names
        releases = value.get("releases", [])
        if not isinstance(releases, list) or len(releases) > 1000:
            raise ValueError("Invalid releases")
        albums = set()
        release_names = set()
        release_ids: list[tuple[str | None, str | None]] = []
        dated_albums: list[tuple[str, str]] = []
        for release in releases:
            if not isinstance(release, dict) or not isinstance(release.get("title"), str):
                raise ValueError("Invalid release")
            album = release["title"]
            if len(album) > 500:
                raise ValueError("Invalid release title")
            if album.strip():
                albums.add(album)
                release_names.add(album)
                if release_date := _official_album_date(release):
                    dated_albums.append((release_date, album))
            group = release.get("release-group")
            if isinstance(group, dict) and isinstance(group.get("title"), str):
                release_names.add(group["title"][:500])
            release_id = release.get("id")
            group_id = group.get("id") if isinstance(group, dict) else None
            release_ids.append(
                (
                    release_id.lower() if isinstance(release_id, str) else None,
                    group_id.lower() if isinstance(group_id, str) else None,
                )
            )
        release_count = value.get("release-count")
        release_offset = value.get("release-offset", 0)
        result["albums"] = sorted(release_names)
        result["release_ids"] = release_ids
        if dated_albums:
            release_date, title = min(dated_albums)
            result["earliest_album"] = {"title": title, "date": release_date}
        result["albums_complete"] = type(release_count) is int and (
            release_count == len(releases) and type(release_offset) is int and release_offset == 0
        )
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
