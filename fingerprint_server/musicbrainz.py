"""Rate-limited MusicBrainz lookups, response validation and identifier verification."""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fingerprint_server.musicbrainz_client import MusicBrainzClient
from fingerprint_server.musicbrainz_errors import (
    EnrichmentError as EnrichmentError,
)
from fingerprint_server.musicbrainz_errors import (
    MissingIdentifierVerification as MissingIdentifierVerification,
)
from fingerprint_server.musicbrainz_errors import (
    MusicBrainzUnavailable as MusicBrainzUnavailable,
)
from fingerprint_server.musicbrainz_errors import (
    RecordingReviewRequired as RecordingReviewRequired,
)
from fingerprint_server.musicbrainz_errors import (
    UnresolvableVerification as UnresolvableVerification,
)
from fingerprint_server.musicbrainz_errors import (
    VerificationPending as VerificationPending,
)
from fingerprint_server.schemas import ISRC_PATTERN, metadata_searchable

if TYPE_CHECKING:
    import httpx

    from fingerprint_server.audiodb import AudioDBClient
    from fingerprint_server.database import Storage
    from fingerprint_server.settings import Settings

LOGGER = logging.getLogger(__name__)


class _MetadataSearchUnresolved(VerificationPending):
    """A valid MusicBrainz metadata search found no unique candidate."""


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


def _audiodb_candidate(candidate: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    """Accept an exact TheAudioDB candidate only as a recording lookup hint."""
    for field, expected in (
        ("strTrack", metadata["title"]),
        ("strArtist", metadata["artists"][0]),
        ("strAlbum", metadata["album"]),
    ):
        value = candidate.get(field)
        if (
            not isinstance(value, str)
            or len(value) > 500
            or not _normalized_name(value)
            or _normalized_name(value) != _normalized_name(expected)
        ):
            return None
    length = candidate.get("intDuration")
    if isinstance(length, str) and len(length) <= 10 and length.isascii() and length.isdigit():
        length = int(length)
    if type(length) is not int or length <= 0 or abs(length - metadata["duration"] * 1000) > 2000:
        return None
    isrc = candidate.get("strISRC")
    if isrc and (
        not isinstance(isrc, str)
        or (metadata.get("isrc") and isrc.upper() != metadata["isrc"].upper())
    ):
        return None
    identifier = candidate.get("strMusicBrainzID")
    if not isinstance(identifier, str) or len(identifier) > 36:
        return None
    try:
        return str(UUID(identifier))
    except ValueError:
        return None


class MusicBrainzService(MusicBrainzClient):
    """Verify recording identifiers with bounded, cached and globally paced requests."""

    def __init__(
        self,
        store: Storage,
        settings: Settings,
        client: httpx.AsyncClient,
        audiodb: AudioDBClient | None = None,
    ) -> None:
        super().__init__(store, settings, client)
        self.audiodb = audiodb

    async def prefetch(self, rows: list[dict[str, Any]]) -> None:
        """Fetch positive identifier evidence for up to twenty eligible submissions."""
        identifiers: set[tuple[str, str]] = set()
        for row in rows[:20]:
            try:
                metadata = json.loads(row["document"]).get("metadata", {})
                if not isinstance(metadata, dict):
                    continue
                mbid = metadata.get("recording_mbid")
                if isinstance(mbid, str):
                    identifiers.add(("rid", str(UUID(mbid))))
            except (ValueError, TypeError, AttributeError):
                # The ordinary per-submission parser reports malformed documents.
                continue
        if len(rows) < 2 or len(identifiers) < 2:
            self.batch_recordings.clear()
            self.batch_submissions = {row["id"] for row in rows[:20]}
            return
        query = " OR ".join(
            f"{kind}:{_search_phrase(value)}" for kind, value in sorted(identifiers)
        )
        response = await self.lookup("batch", query)
        self.batch_recordings.clear()
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
            if ("rid", recording["id"]) in identifiers and recording["isrcs"] is not None:
                self.batch_recordings[recording["id"]] = recording
        LOGGER.info(
            "MusicBrainz batch: submissions=%s; identifiers=%s; recording_evidence=%s",
            len(self.batch_submissions),
            len(identifiers),
            len(self.batch_recordings),
        )

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
                candidates = {
                    item["id"]: item
                    for item in response["recordings"]
                    if recording_matches(item, metadata)
                }
                if len(candidates) != 1:
                    LOGGER.info(
                        "MusicBrainz confirmed ISRC %s with %s matching recordings; "
                        "retaining supplied metadata without assigning a recording MBID",
                        normalized,
                        len(candidates),
                    )
                    return dict(metadata)
                recording = next(iter(candidates.values()))
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

    async def _resolve_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Resolve exactly one complete metadata candidate, then verify its recording lookup."""
        try:
            return await self._search_metadata(metadata)
        except _MetadataSearchUnresolved:
            if self.audiodb is None:
                raise
            album = metadata.get("album")
            title = metadata.get("title")
            artists = metadata.get("artists")
            if (
                not isinstance(album, str)
                or not _normalized_name(album)
                or not isinstance(title, str)
                or not isinstance(artists, list)
                or not artists
                or not isinstance(artists[0], str)
            ):
                raise
            candidates = await self.audiodb.search(artists[0], title)
            identifiers = {
                identifier
                for candidate in candidates
                if (identifier := _audiodb_candidate(candidate, metadata)) is not None
            }
            if len(identifiers) != 1:
                raise
            identifier = next(iter(identifiers))
            response = await self.lookup("recording", identifier, use_batch=False)
            if response["status"] != "resolved":
                raise
            recording = response["recording"]
            if (
                recording["id"] != identifier
                or not _metadata_matches(recording, metadata, contextual=True)
                or not any(
                    _normalized_name(album) == _normalized_name(name)
                    for name in recording["albums"]
                )
                or (
                    (metadata.get("release_mbid") or metadata.get("release_group_mbid"))
                    and not _release_evidence(recording, metadata)
                )
            ):
                raise
            LOGGER.info("MusicBrainz recording %s resolved using TheAudioDB candidate", identifier)
            return recording

    async def _search_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Resolve a candidate using MusicBrainz metadata search."""
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
            raise _MetadataSearchUnresolved("MusicBrainz metadata search missing or incomplete")
        candidates = {
            candidate["id"]: candidate
            for candidate in response["recordings"]
            if _metadata_matches(candidate, metadata, contextual=True)
        }
        if not candidates:
            raise _MetadataSearchUnresolved(
                "MusicBrainz metadata search found no matching recording"
            )
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
                        **{
                            key: candidate.get(key)
                            for key in (
                                "id",
                                "title",
                                "artists",
                                "length",
                                "disambiguation",
                                "earliest_album",
                            )
                        },
                        "albums": candidate["review_albums"],
                        "other_releases": candidate["review_releases"],
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
