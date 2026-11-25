"""Compact database values preserve textual semantics and unknown details."""

import sqlite3

import pytest

from fingerprint_server.database.codes import (
    ARTWORK_REASONS,
    SUBMISSION_ERRORS,
    ArtworkState,
    SubmissionState,
    TextCodes,
    artwork_state_raw,
    decode_artwork_state,
    decode_submission_state,
    encode_artwork_state,
    encode_submission_state,
    submission_state_raw,
)


@pytest.mark.parametrize("codec", (SUBMISSION_ERRORS, ARTWORK_REASONS))
def test_round_trip_and_sql_decoding(codec: TextCodes) -> None:
    values = [
        None,
        *codec.values,
        "",
        "TheAudioDB: unknown provider detail",
        "~0",
        "~ff",
        "~~escaped",
        "an apostrophe's value",
    ]
    with sqlite3.connect(":memory:") as db:
        for value in values:
            encoded = codec.encode(value)
            assert codec.decode(encoded) == value
            result = db.execute(f"SELECT {codec.decoded_sql('?')}", [encoded] * 4).fetchone()
            assert result is not None
            assert result[0] == value
            result = db.execute(f"SELECT {codec.encoded_sql('?')}", [value] * 4).fetchone()
            assert result is not None
            assert result[0] == encoded
        for value in codec.values:
            encoded = codec.encode(value)
            assert encoded is not None
            assert len(encoded.encode()) < len(value.encode())


def test_submission_state_codes_round_trip() -> None:
    for member in SubmissionState:
        if member is SubmissionState.UNKNOWN:
            continue
        text = member.name.lower()
        assert encode_submission_state(text) == member.value
        assert submission_state_raw(text) is None
        assert decode_submission_state(member.value) == text
    for text in ("", "0", "~0", "future state", "Ready"):
        assert encode_submission_state(text) == SubmissionState.UNKNOWN
        assert decode_submission_state(SubmissionState.UNKNOWN, submission_state_raw(text)) == text
    assert decode_submission_state(None) is None
    assert decode_submission_state(SubmissionState.QUEUED, "not used") == "queued"
    with pytest.raises(ValueError):
        decode_submission_state(SubmissionState.UNKNOWN)
    with pytest.raises(ValueError):
        decode_submission_state(999)


def test_artwork_state_codes_round_trip() -> None:
    for member in ArtworkState:
        if member is ArtworkState.UNKNOWN:
            continue
        assert encode_artwork_state(member.name.lower()) == member.value
        assert decode_artwork_state(member.value) == member.name.lower()
    for text in ("", "0", "~0", "future state", "Ready"):
        assert encode_artwork_state(text) == ArtworkState.UNKNOWN
        assert decode_artwork_state(ArtworkState.UNKNOWN, artwork_state_raw(text)) == text
    assert decode_artwork_state(ArtworkState.READY, "not used") == "ready"
    with pytest.raises(ValueError):
        decode_artwork_state(ArtworkState.UNKNOWN)
    with pytest.raises(ValueError):
        decode_artwork_state(999)
