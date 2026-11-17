"""Strict versioned reference documents; no audio URLs or arbitrary metadata fields."""

import hashlib
import json
import re
from bisect import bisect_right
from collections.abc import Mapping
from math import isfinite
from typing import Annotated, Literal
from uuid import UUID

from audio_fingerprint import (
    FFT_SIZE,
    HOP_SIZE,
    MAX_DURATION,
    MAX_TRACK_HASHES,
    SAMPLE_RATE,
    WINDOW_COUNT,
    WINDOW_SECONDS,
)
from audio_fingerprint.codec import decode_document
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{20}$")]
Offset = Annotated[int, Field(ge=0, le=MAX_DURATION * SAMPLE_RATE // HOP_SIZE)]
Text = Annotated[str, Field(max_length=500)]
ISRC_PATTERN = r"^[A-Za-z]{2}[A-Za-z0-9]{3}[0-9]{7}$"


def metadata_complete(metadata: object) -> bool:
    """Whether supplied metadata is sufficient to publish a searchable reference."""
    if not isinstance(metadata, Mapping):
        return False
    title, artists, album = (metadata.get(key) for key in ("title", "artists", "album"))
    duration = metadata.get("duration")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(artists, list)
        or not any(isinstance(artist, str) and artist.strip() for artist in artists)
        or not isinstance(album, str)
        or not album.strip()
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or duration <= 0
    ):
        return False
    try:
        if not isfinite(duration):
            return False
    except OverflowError:
        return False
    isrc = metadata.get("isrc")
    if isinstance(isrc, str) and re.fullmatch(ISRC_PATTERN, isrc):
        return True
    mbid = metadata.get("recording_mbid")
    if isinstance(mbid, str):
        try:
            UUID(mbid)
        except ValueError:
            return False
        return True
    return False


class StrictModel(BaseModel):
    """Reject unknown fields and type coercion."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class LandmarkQuery(StrictModel):
    """Transient client-extracted landmarks relative to one normalized capture."""

    version: Literal[1]
    format_id: Literal["ma-dejavu-landmarks-v1", "ma-dejavu-landmarks-v2"]
    sample_rate: Literal[44100]
    offset_unit: Literal["fft_hops"]
    sample_count: int = Field(ge=3 * SAMPLE_RATE, le=15 * SAMPLE_RATE)
    fingerprints: list[tuple[Digest, Offset]] = Field(max_length=10000)

    @model_validator(mode="after")
    def validate_capture(self) -> "LandmarkQuery":
        """Require distinct pairs whose anchor offsets belong to the capture."""
        if len(set(self.fingerprints)) != len(self.fingerprints):
            raise ValueError("Duplicate fingerprint/offset pairs")
        maximum = (self.sample_count - FFT_SIZE) // HOP_SIZE
        if any(offset > maximum for _, offset in self.fingerprints):
            raise ValueError("Fingerprint offset is outside capture coverage")
        return self


class Metadata(StrictModel):
    """Allowlisted supplied music metadata."""

    duration: float | None = Field(default=None, gt=0, le=MAX_DURATION)
    title: Text | None = None
    artists: list[Text] = Field(default_factory=list, max_length=30)
    album: Text | None = None
    recording_mbid: str | None = None
    isrc: Annotated[str, Field(pattern=ISRC_PATTERN)] | None = None

    @field_validator("recording_mbid")
    @classmethod
    def valid_mbid(cls, value: str | None) -> str | None:
        """Require a MusicBrainz recording UUID when supplied."""
        if value is not None:
            UUID(value)
        return value


class AvailabilityItem(Metadata):
    """Identity evidence for one recording."""

    format_id: Literal["ma-dejavu-landmarks-v1", "ma-dejavu-landmarks-v2"]
    index: int = Field(ge=0, le=99)


class AvailabilityRequest(StrictModel):
    """Bounded ordered availability query."""

    version: Literal[1]
    items: list[AvailabilityItem] = Field(min_length=1, max_length=100)


class AvailabilityResult(StrictModel):
    index: int
    status: Literal["full", "partial", "unknown"]
    recording_mbid: str | None = None


class AvailabilityResponse(StrictModel):
    version: Literal[1]
    items: list[AvailabilityResult]


class Window(StrictModel):
    """Sample coverage relative to the beginning of the reference track."""

    start_sample: int = Field(ge=0)
    sample_count: int = Field(ge=FFT_SIZE, le=WINDOW_SECONDS * SAMPLE_RATE)


class Submission(StrictModel):
    """The exact v1 document produced by the MA Dejavu analysis provider."""

    format_id: Literal["ma-dejavu-landmarks-v1", "ma-dejavu-landmarks-v2"]
    sample_rate: Literal[44100]
    offset_unit: Literal["fft_hops"]
    windows: list[Window] = Field(min_length=1, max_length=WINDOW_COUNT)
    fingerprints: list[tuple[Digest, Offset]] = Field(min_length=1, max_length=MAX_TRACK_HASHES)
    metadata: Metadata

    @model_validator(mode="after")
    def validate_coverage(self) -> "Submission":
        """Validate legacy sampled coverage or gap-free full-track coverage."""
        legacy = self.format_id == "ma-dejavu-landmarks-v1"
        if legacy and (len(self.windows) > 3 or len(self.fingerprints) > 100000):
            raise ValueError("Legacy submission exceeds its window or landmark limit")
        previous_end = 0
        previous_start = -1
        ranges: list[tuple[int, int]] = []
        duration = self.metadata.duration
        total = round((duration if duration is not None else MAX_DURATION) * SAMPLE_RATE)
        for window in self.windows:
            end = window.start_sample + window.sample_count
            if (
                window.start_sample % HOP_SIZE
                or window.start_sample <= previous_start
                or end > total
                or (legacy and window.start_sample < previous_end)
                or (not legacy and (window.start_sample > previous_end or end <= previous_end))
            ):
                raise ValueError("Window coverage is inconsistent with the track duration")
            lo, hi = window.start_sample // HOP_SIZE, (end - FFT_SIZE) // HOP_SIZE
            if ranges and lo <= ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
            else:
                ranges.append((lo, hi))
            previous_end = end
            previous_start = window.start_sample
        if not legacy and duration is not None and previous_end != total:
            raise ValueError("Full-track coverage must reach the exact track end")
        if len(set(self.fingerprints)) != len(self.fingerprints):
            raise ValueError("Duplicate fingerprint/offset pairs")
        starts = [lo for lo, _ in ranges]
        for _, offset in self.fingerprints:
            index = bisect_right(starts, offset) - 1
            if index < 0 or offset > ranges[index][1]:
                raise ValueError("Fingerprint offset is outside sampled coverage")
        return self


def decode_submission(raw: str | bytes) -> Submission:
    """Validate both legacy and bounded compact documents against the same acoustic schema."""
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("Fingerprint document must be an object")
    decoded = decode_document(document)
    return Submission.model_validate_json(
        json.dumps(decoded, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )


def acoustic_reference_id(submission: Submission) -> str:
    """Return the acoustic identity of a submission."""
    identity = {
        "format_id": submission.format_id,
        "windows": [window.model_dump() for window in submission.windows],
        "fingerprints": sorted(submission.fingerprints),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
