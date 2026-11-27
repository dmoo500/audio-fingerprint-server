"""Client-extracted landmark queries share the PCM recognition contract and limits."""

import json
from pathlib import Path

import pytest
from aiohttp_client import AsyncClient
from audio_fingerprint import FORMAT_ID, HOP_SIZE, SAMPLE_RATE, fingerprint
from test_server import READ, UPLOAD, audio, document, recognize, settings, submit

from fingerprint_server.api_state import STORE
from fingerprint_server.app import MAX_LANDMARK_QUERY_BYTES, create_app
from fingerprint_server.database.sqlite import QueryTooBroadError

PATH = "/api/v1/dejavu/recognize/landmarks"
HEADERS = {"X-API-Key": READ, "Content-Type": "application/json"}


def query() -> dict:
    samples = audio(5)
    return {
        "version": 1,
        "format_id": FORMAT_ID,
        "sample_rate": SAMPLE_RATE,
        "offset_unit": "fft_hops",
        "sample_count": len(samples),
        "fingerprints": [list(pair) for pair in fingerprint(samples)],
    }


@pytest.fixture
async def client(tmp_path: Path):
    application = create_app(settings(tmp_path))
    async with AsyncClient(application) as connection:
        yield connection, application


async def test_landmarks_equal_pcm_and_never_ingest(client) -> None:
    connection, application = client
    samples = audio()
    await submit(connection, document(samples))
    excerpt = samples[50 * HOP_SIZE : 50 * HOP_SIZE + 5 * SAMPLE_RATE]
    payload = query()
    payload["fingerprints"] = [list(pair) for pair in fingerprint(excerpt)]
    before = application[STORE].statistics()
    pcm = await recognize(connection, excerpt)
    response = await connection.post(PATH, headers=HEADERS, json=payload)
    assert response.status_code == 200
    assert response.json() == pcm.json()
    assert response.json()["status"] == "matched"
    assert application[STORE].statistics() == before


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"format_id": "unrelated-format"},
        {"sample_rate": 48000},
        {"offset_unit": "seconds"},
        {"sample_count": SAMPLE_RATE * 3 - 1},
        {"sample_count": SAMPLE_RATE * 15 + 1},
        {"sample_count": True},
        {"metadata": {}},
        {"fingerprints": [["invalid", 0]]},
        {"fingerprints": [["0" * 20, -1]]},
        {"fingerprints": [["0" * 20, True]]},
        {"fingerprints": [["0" * 20, 200]]},
        {"fingerprints": [["0" * 20, 0], ["0" * 20, 0]]},
        {"fingerprints": [[f"{value:020x}", 0] for value in range(10001)]},
    ],
)
async def test_reject_invalid_queries(client, change: dict) -> None:
    connection, _ = client
    response = await connection.post(PATH, headers=HEADERS, json=query() | change)
    assert response.status_code == 422, response.text


async def test_limits_authentication_and_empty_queries(client) -> None:
    connection, _ = client
    for key in ("wrong", UPLOAD):
        response = await connection.post(PATH, headers={"X-API-Key": key}, content=b"not json")
        assert response.status_code == 401
    response = await connection.post(PATH, headers={"X-API-Key": READ}, content=b"{}")
    assert response.status_code == 415
    response = await connection.post(
        PATH, headers=HEADERS, content=b" " * (MAX_LANDMARK_QUERY_BYTES + 1)
    )
    assert response.status_code == 413
    for body in (b"{", b"[]", b'{"version":NaN}'):
        response = await connection.post(PATH, headers=HEADERS, content=body)
        assert response.status_code == 422
    response = await connection.post(PATH, headers=HEADERS, json=query() | {"fingerprints": []})
    assert response.json() == {
        "status": "no_match",
        "reason": "No acoustic landmarks",
        "match": None,
    }


async def test_both_recognition_routes_share_request_budget(client) -> None:
    connection, _ = client
    payload = query() | {"fingerprints": []}
    for index in range(30):
        if index % 2:
            response = await recognize(connection, audio(3) * 0)
        else:
            response = await connection.post(PATH, headers=HEADERS, json=payload)
        assert response.status_code == 200
    response = await connection.post(PATH, headers=HEADERS, json=payload)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"


async def test_candidate_limit_is_error_not_no_match(client, monkeypatch) -> None:
    connection, application = client

    def too_broad(pairs):
        raise QueryTooBroadError

    monkeypatch.setattr(application[STORE], "match", too_broad)
    response = await connection.post(PATH, headers=HEADERS, json=query())
    assert response.status_code == 503


async def test_query_schema_rejects_encoded_documents(client) -> None:
    connection, _ = client
    response = await connection.post(
        PATH,
        headers=HEADERS,
        content=json.dumps(query() | {"fingerprints_encoding": "sha1-80-offset-delta-v1"}),
    )
    assert response.status_code == 422


async def test_optional_ambiguous_candidates_keep_normal_recognition_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = create_app(settings(tmp_path))
    candidate = {
        "matched_landmarks": 40,
        "match_fraction": 0.04,
        "matched_span_seconds": 2,
        "offset_seconds": 1,
        "metadata": {"title": "First", "artists": ["Artist"], "album": "Album"},
        "reference_id": "private",
    }
    weaker = candidate | {"matched_landmarks": 29, "metadata": {"title": "Weak"}}
    async with AsyncClient(application) as client:
        monkeypatch.setattr(application[STORE], "match", lambda _: [candidate, weaker])
        samples = audio(3)
        default = await recognize(client, samples)
        assert default.json() == {"status": "ambiguous", "match": None}
        response = await client.post(
            "/api/v1/dejavu/recognize?sample_rate=44100&include_candidates=true",
            content=samples.tobytes(),
            headers={"X-API-Key": READ, "Content-Type": "application/octet-stream"},
        )
        assert response.json() == {
            "status": "ambiguous",
            "match": None,
            "candidates": [
                {
                    "metadata": candidate["metadata"],
                    "match_fraction": 0.04,
                    "reference_id": candidate["reference_id"],
                }
            ],
        }
        monkeypatch.setattr(application[STORE], "match", lambda _: [weaker])
        response = await client.post(
            "/api/v1/dejavu/recognize?sample_rate=44100&include_candidates=true",
            content=samples.tobytes(),
            headers={"X-API-Key": READ, "Content-Type": "application/octet-stream"},
        )
        assert response.json() == {"status": "no_match", "match": None}


async def test_real_ambiguous_pcm_exposes_candidates_only_when_requested(client) -> None:
    connection, _ = client
    samples = audio()
    await submit(connection, document(samples))
    second = document(samples)
    second["fingerprints"] = second["fingerprints"][:-1]
    second["metadata"]["title"] = "Another interpretation"
    await submit(connection, second)
    excerpt = samples[50 * HOP_SIZE : 50 * HOP_SIZE + 5 * SAMPLE_RATE]
    response = await connection.post(
        "/api/v1/dejavu/recognize?sample_rate=44100&include_candidates=true",
        content=excerpt.tobytes(),
        headers={"X-API-Key": READ, "Content-Type": "application/octet-stream"},
    )
    result = response.json()
    assert result["status"] == "ambiguous"
    assert result["match"] is None
    assert {candidate["metadata"]["title"] for candidate in result["candidates"]} == {
        "Synthetic reference",
        "Another interpretation",
    }
    assert all(len(candidate["reference_id"]) == 64 for candidate in result["candidates"])
