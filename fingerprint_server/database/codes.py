"""Reversible compact representations of repeated database values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class SubmissionState(IntEnum):
    """Persisted submission states; numeric values are part of the database format."""

    QUEUED = 0
    PROCESSING = 1
    PROCESSED = 2
    FAILED = 3
    PENDING_METADATA = 4
    UNKNOWN = 5


class ArtworkState(IntEnum):
    """Persisted artwork states; numeric values are part of the database format."""

    PENDING = 0
    READY = 1
    MISSING = 2
    UNAVAILABLE = 3
    UNKNOWN = 4


def encode_submission_state(value: str) -> int:
    """Encode a submission state, retaining unrecognized text separately."""
    for state in SubmissionState:
        if state is not SubmissionState.UNKNOWN and value == state.name.lower():
            return state.value
    return SubmissionState.UNKNOWN.value


def submission_state_raw(value: str) -> str | None:
    """Return original text only for an unrecognized submission state."""
    return value if encode_submission_state(value) == SubmissionState.UNKNOWN else None


def decode_submission_state(value: int | str | None, raw: str | None = None) -> str | None:
    """Decode a stored submission state and its optional raw value."""
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError("Invalid stored submission state")
    try:
        state = SubmissionState(value)
    except ValueError as err:
        raise ValueError("Invalid stored submission state") from err
    if state is SubmissionState.UNKNOWN:
        if not isinstance(raw, str):
            raise ValueError("Unknown submission state has no raw value")
        return raw
    return state.name.lower()


def encode_artwork_state(value: str) -> int:
    """Encode an artwork state, retaining unknown text separately."""
    for state in ArtworkState:
        if state is not ArtworkState.UNKNOWN and value == state.name.lower():
            return state.value
    return ArtworkState.UNKNOWN.value


def artwork_state_raw(value: str) -> str | None:
    """Return original text only for an unrecognized artwork state."""
    return value if encode_artwork_state(value) == ArtworkState.UNKNOWN else None


def decode_artwork_state(value: int | str | None, raw: str | None = None) -> str | None:
    """Decode a stored artwork state."""
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError("Invalid stored artwork state")
    try:
        state = ArtworkState(value)
    except ValueError as err:
        raise ValueError("Invalid stored artwork state") from err
    if state is ArtworkState.UNKNOWN:
        if not isinstance(raw, str):
            raise ValueError("Unknown artwork state has no raw value")
        return raw
    return state.name.lower()


def decoded_submission_state_sql(column: str, raw_column: str) -> str:
    """Decode a trusted SQLite column expression for text filtering and sorting."""
    return f"decode_submission_state({column},{raw_column})"


@dataclass(frozen=True)
class TextCodes:
    """Map known text to short codes without losing unknown values.

    The value order is persisted and must only be extended, never reordered.
    """

    values: tuple[str, ...]

    def encode(self, value: str | None) -> str | None:
        """Encode a value read from an external source or a legacy database."""
        if value is None:
            return None
        try:
            return f"~{self.values.index(value):x}"
        except ValueError:
            return f"~{value}" if value.startswith("~") else value

    def decode(self, value: str | None) -> str | None:
        """Decode a stored value, preserving unknown and escaped values."""
        if value is None or not value.startswith("~"):
            return value
        if value.startswith("~~"):
            return value[1:]
        try:
            index = int(value[1:], 16)
        except ValueError:
            return value
        if index < 0 or index >= len(self.values) or value != f"~{index:x}":
            return value
        return self.values[index]

    def decoded_sql(self, column: str) -> str:
        """Return a SQLite/PostgreSQL expression decoding a trusted SQL column expression."""

        def literal(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        cases = " ".join(
            f"WHEN {literal(f'~{index:x}')} THEN {literal(value)}"
            for index, value in enumerate(self.values)
        )
        return (
            f"CASE WHEN substr({column},1,2)='~~' THEN substr({column},2) "
            f"ELSE CASE {column} {cases} ELSE {column} END END"
        )

    def encoded_sql(self, column: str) -> str:
        """Return a SQLite/PostgreSQL expression encoding a trusted SQL column expression."""
        cases = " ".join(
            "WHEN '" + value.replace("'", "''") + "' THEN '" + str(self.encode(value)) + "'"
            for value in self.values
        )
        return (
            f"CASE {column} {cases} ELSE CASE WHEN substr({column},1,1)='~' "
            f"THEN '~'||{column} ELSE {column} END END"
        )


SUBMISSION_ERRORS = TextCodes(
    (
        "Incomplete metadata",
        "MusicBrainz identifier unavailable",
        "MusicBrainz service unavailable",
        "Metadata lookup unavailable",
        "Invalid reference or metadata response",
        "Retained reference metadata is invalid",
        "MusicBrainz ISRC ambiguous",
        "MusicBrainz ISRC not found",
        "MusicBrainz ISRC not found; metadata unconfirmed",
        "MusicBrainz ISRC not found; search metadata incomplete",
        "MusicBrainz ISRC recording list incomplete",
        "MusicBrainz recording not found",
        "MusicBrainz recording ISRCs unavailable",
        "MusicBrainz metadata search missing or incomplete",
        "MusicBrainz metadata search found no matching recording",
        "MusicBrainz metadata search recording not confirmed",
        "Selected MusicBrainz recording no longer matches",
        "Invalid MusicBrainz response",
        "MusicBrainz identifier redirected; verification required",
        "MusicBrainz response exceeds size limit",
        "MusicBrainz recording requires manual selection",
        "TheAudioDB request unavailable",
        "TheAudioDB HTTP 429",
        "TheAudioDB HTTP 502",
        "TheAudioDB HTTP 503",
        "Invalid TheAudioDB response",
        "TheAudioDB returned an API error",
        "TheAudioDB response exceeds size limit",
    )
)

ARTWORK_REASONS = TextCodes(
    (
        "legacy_retry",
        "edition_retry",
        "album_not_found",
        "recording_not_found",
        "release_hint_not_found",
        "incomplete_releases",
        "conflicting_release_hint",
        "ambiguous_album",
        "ready_release",
        "ready_group",
        "no_cover",
        "search_limit",
        "upstream_unavailable",
        "upstream_error",
    )
)
