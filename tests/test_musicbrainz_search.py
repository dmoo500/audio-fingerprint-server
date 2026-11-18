"""Conservative metadata resolution when MusicBrainz has no ISRC association."""

import logging
from pathlib import Path

import httpx
import pytest
from test_metadata_gate import document
from test_musicbrainz_validation import (
    ISRC,
    MBID,
    OTHER,
    online_settings,
    online_store,
    recording,
    run_until_idle,
)

from fingerprint_server.database.sqlite import Store
from fingerprint_server.musicbrainz import (
    EnrichmentError,
    MusicBrainzService,
    MusicBrainzUnavailable,
    VerificationPending,
)
from fingerprint_server.worker import Worker

METADATA = {"title": "Title", "artists": ["Artist"], "duration": 12.0, "isrc": ISRC}


def candidate(mbid: str = MBID, **fields):
    return {**recording(mbid), "length": 12000, "isrcs": [], **fields}


@pytest.mark.parametrize("with_isrc", [True, False])
async def test_unique_metadata_fills_recording_and_survives_reverification(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, with_isrc: bool
) -> None:
    calls = []

    def reply(request):
        calls.append(request)
        if "/isrc/" in request.url.path:
            return httpx.Response(404)
        if "query" in request.url.params:
            return httpx.Response(200, json={"count": 1, "offset": 0, "recordings": [candidate()]})
        return httpx.Response(200, json=candidate())

    metadata = dict(METADATA)
    if not with_isrc:
        metadata.pop("isrc")
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        with caplog.at_level(logging.INFO):
            result = await service.verify(metadata)
            assert await service.verify(result) == result
            assert await service.verify(metadata) == result
    assert result["recording_mbid"] == MBID
    assert result["album"] == "Album"
    assert result["title"] == "Title"
    assert len(calls) == (3 if with_isrc else 2)
    assert "resolved by exact metadata" in caplog.text
    if with_isrc:
        assert result["isrc"] == ISRC
        assert "ISRC association remains unconfirmed" in caplog.text


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "Title (Live)"},
        {"disambiguation": "live"},
        {"length": 14001},
        {"length": None},
        {"artist-credit": [{"name": "Other Artist"}]},
    ],
)
async def test_versions_missing_duration_and_wrong_credits_not_guessed(tmp_path, fields):
    def reply(request):
        if "/isrc/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(
            200, json={"count": 1, "offset": 0, "recordings": [candidate(**fields)]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        with pytest.raises(EnrichmentError):
            await service.verify(dict(METADATA))


@pytest.mark.parametrize("count", [2, 26])
async def test_ambiguous_or_truncated_results_stay_pending(tmp_path, count):
    def reply(request):
        if "/isrc/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(
            200, json={"count": count, "offset": 0, "recordings": [candidate(), candidate(OTHER)]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        with pytest.raises(VerificationPending):
            await service.verify(dict(METADATA))


async def test_hydrated_recording_must_still_match(tmp_path):
    def reply(request):
        if "/isrc/" in request.url.path:
            return httpx.Response(404)
        if "query" in request.url.params:
            return httpx.Response(200, json={"count": 1, "offset": 0, "recordings": [candidate()]})
        return httpx.Response(200, json=candidate(disambiguation="radio edit"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        with pytest.raises(VerificationPending, match="not confirmed"):
            await service.verify(dict(METADATA))


@pytest.mark.parametrize("status", [429, 502, 503])
async def test_search_uses_shared_backoff(tmp_path, status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status))
    ) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        with pytest.raises(MusicBrainzUnavailable):
            await service.verify({key: value for key, value in METADATA.items() if key != "isrc"})
    assert service.cooldown_until > 0


async def test_search_escapes_lucene_input_and_uses_shared_pacing(tmp_path, monkeypatch):
    calls = []
    deadlines = []

    async def wait(deadline):
        deadlines.append(deadline)

    def reply(request):
        calls.append(request)
        if "/isrc/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={"count": 0, "offset": 0, "recordings": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        monkeypatch.setattr(service, "_wait_until", wait)
        with pytest.raises(VerificationPending):
            await service.verify({**METADATA, "title": 'Title" OR artist:*'})
    assert (
        calls[-1].url.params["query"] == 'recording:"Title\\" OR artist\\:\\*" AND artist:"Artist"'
    )
    assert calls[-1].url.params["limit"] == "25"
    assert deadlines[0] == 0
    assert deadlines[1] > 0
    assert service.lookup_call == "GET /recording (metadata search)"


@pytest.mark.parametrize(
    "body",
    [
        {"recordings": []},
        {"count": True, "offset": 0, "recordings": []},
        {"count": 0, "offset": 1, "recordings": []},
        {"count": 0, "offset": 0, "recordings": [candidate()]},
    ],
)
async def test_malformed_search_not_cached(tmp_path, body):
    store = online_store(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        service = MusicBrainzService(store, online_settings(tmp_path), client)
        with pytest.raises(EnrichmentError, match="Invalid MusicBrainz"):
            await service.lookup("search", 'recording:"Title"')
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM enrichment_cache").fetchone()[0] == 0


@pytest.mark.parametrize("with_isrc", [True, False])
async def test_worker_publishes_recovered_recording_without_changing_submission(
    tmp_path, with_isrc
):
    import json

    store = Store(tmp_path / "test.db", True, housekeeping_enabled=False)
    store.initialize()
    payload = document()
    payload["metadata"] = {**METADATA, "album": "Album"}
    if not with_isrc:
        payload["metadata"].pop("isrc")
    original = json.dumps(payload)
    store.submit("recover", original, 10)

    def reply(request):
        if "/isrc/" in request.url.path:
            return httpx.Response(404)
        if "query" in request.url.params:
            return httpx.Response(200, json={"count": 1, "offset": 0, "recordings": [candidate()]})
        return httpx.Response(200, json=candidate())

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        row = await run_until_idle(
            Worker(
                store,
                online_settings(tmp_path).model_copy(update={"housekeeping_enabled": False}),
                client,
            ),
            store,
            "recover",
        )
    assert row["state"] == "processed"
    with store.connect() as db:
        assert (
            db.execute("SELECT document FROM submissions WHERE id='recover'").fetchone()["document"]
            == original
        )
        reference = db.execute("SELECT metadata,recording_key FROM reference_tracks").fetchone()
    assert json.loads(reference["metadata"])["recording_mbid"] == MBID
    assert reference["recording_key"] == MBID.replace("-", "")
