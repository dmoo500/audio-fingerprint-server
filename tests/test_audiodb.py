"""TheAudioDB candidate searches remain subject to MusicBrainz verification."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import httpx
import pytest
from pydantic import SecretStr, ValidationError
from test_metadata_gate import document
from test_musicbrainz_search import METADATA, candidate
from test_musicbrainz_validation import (
    ISRC,
    MBID,
    OTHER,
    online_settings,
    online_store,
    run_until_idle,
)

import fingerprint_server.audiodb as audiodb_module
from fingerprint_server.audiodb import AudioDBClient, AudioDBUnavailable
from fingerprint_server.musicbrainz import MusicBrainzService, RecordingReviewRequired
from fingerprint_server.musicbrainz_errors import VerificationPending
from fingerprint_server.worker import Worker


def track(**overrides):
    return {
        "strTrack": "Title",
        "strArtist": "Artist",
        "strAlbum": "Album",
        "intDuration": "12000",
        "strMusicBrainzID": MBID,
        "strISRC": ISRC,
        **overrides,
    }


def session_for(payload, *, status=200, headers=None):
    session = MagicMock(spec=aiohttp.ClientSession)
    response = MagicMock()
    response.status = status
    response.headers = headers or {}

    async def chunks(_size):
        yield json.dumps(payload).encode()

    response.content.iter_chunked.side_effect = chunks
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)
    session.get.return_value = response
    return session


def musicbrainz_reply(request):
    if "/isrc/" in request.url.path:
        return httpx.Response(404)
    if "query" in request.url.params:
        return httpx.Response(200, json={"count": 26, "offset": 0, "recordings": [candidate()]})
    return httpx.Response(200, json=candidate())


async def test_candidate_requires_detail_and_album_confirmation_and_is_cached(
    tmp_path, monkeypatch
):
    requests = []
    session = session_for({"track": [track()]})
    store = online_store(tmp_path)

    def reply(request):
        requests.append(request)
        return musicbrainz_reply(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        audiodb = AudioDBClient(store, SecretStr("123"), session)
        service = MusicBrainzService(store, online_settings(tmp_path), client, audiodb)
        monkeypatch.setattr(service, "_wait_until", AsyncMock())
        service.batch_recordings[MBID] = {"id": MBID}
        metadata = {**METADATA, "album": "Album"}
        first = await service.verify(metadata)
        second = await service.verify(metadata)
    assert first["recording_mbid"] == MBID
    assert second == first
    assert session.get.call_count == 1
    assert sum(request.url.path.endswith(f"/recording/{MBID}") for request in requests) == 1
    assert metadata == {**METADATA, "album": "Album"}


@pytest.mark.parametrize(
    "tracks",
    [
        [track(strAlbum="Another album")],
        [track(strTrack="Title (Live)")],
        [track(intDuration="14001")],
        [track(strISRC="USAAA2600002")],
        [track(strMusicBrainzID=None)],
        [track(), track(strMusicBrainzID=OTHER)],
    ],
)
async def test_unmatched_or_ambiguous_candidates_stay_pending(tmp_path, monkeypatch, tracks):
    requests = []
    session = session_for({"track": tracks})
    store = online_store(tmp_path)

    def reply(request):
        requests.append(request)
        return musicbrainz_reply(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(
            store,
            online_settings(tmp_path),
            client,
            AudioDBClient(store, SecretStr("123"), session),
        )
        monkeypatch.setattr(service, "_wait_until", AsyncMock())
        with pytest.raises(VerificationPending):
            await service.verify({**METADATA, "album": "Album"})
    assert not any(request.url.path.endswith(f"/recording/{MBID}") for request in requests)


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "Another title"},
        {"length": 14001},
        {"artist-credit": [{"name": "Other artist"}]},
        {"releases": []},
    ],
)
async def test_musicbrainz_must_confirm_track_and_album(tmp_path, monkeypatch, fields):
    session = session_for({"track": [track()]})

    def reply(request):
        if request.url.path.endswith(f"/recording/{MBID}"):
            return httpx.Response(200, json=candidate(**fields))
        return musicbrainz_reply(request)

    store = online_store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(
            store,
            online_settings(tmp_path),
            client,
            AudioDBClient(store, SecretStr("123"), session),
        )
        monkeypatch.setattr(service, "_wait_until", AsyncMock())
        with pytest.raises(VerificationPending):
            await service.verify({**METADATA, "album": "Album"})


async def test_musicbrainz_review_is_not_bypassed(tmp_path, monkeypatch):
    session = session_for({"track": [track()]})

    def reply(request):
        if "/isrc/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(
            200, json={"count": 2, "offset": 0, "recordings": [candidate(), candidate(OTHER)]}
        )

    store = online_store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(
            store,
            online_settings(tmp_path),
            client,
            AudioDBClient(store, SecretStr("123"), session),
        )
        monkeypatch.setattr(service, "_wait_until", AsyncMock())
        with pytest.raises(RecordingReviewRequired):
            await service.verify(METADATA)
    session.get.assert_not_called()


async def test_musicbrainz_outage_does_not_trigger_audiodb(tmp_path, monkeypatch):
    session = session_for({"track": [track()]})
    store = online_store(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as client:
        service = MusicBrainzService(
            store,
            online_settings(tmp_path),
            client,
            AudioDBClient(store, SecretStr("123"), session),
        )
        monkeypatch.setattr(service, "_wait_until", AsyncMock())
        from fingerprint_server.musicbrainz import MusicBrainzUnavailable

        with pytest.raises(MusicBrainzUnavailable):
            await service.verify({**METADATA, "album": "Album"})
    session.get.assert_not_called()


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"error": "limit"}, 200),
        ({}, 200),
        ({"track": [track(intDuration={"invalid": True})]}, 200),
        ({}, 429),
        ({}, 502),
        ({}, 503),
        ({}, 404),
    ],
)
async def test_unavailable_responses_are_not_cached(tmp_path, payload, status):
    store = online_store(tmp_path)
    session = session_for(payload, status=status, headers={"Retry-After": "120"})
    audiodb = AudioDBClient(store, SecretStr("123"), session)
    with pytest.raises(AudioDBUnavailable) as error:
        await audiodb.search("Artist", "Title")
    assert error.value.delay >= 60
    assert audiodb.cooldown_until > 0
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM enrichment_cache").fetchone()[0] == 0


async def test_empty_results_are_cached(tmp_path):
    session = session_for({"track": None})
    audiodb = AudioDBClient(online_store(tmp_path), SecretStr("123"), session)
    assert await audiodb.search("Artist", "Title") == []
    assert await audiodb.search("Artist", "Title") == []
    session.get.assert_called_once()


async def test_requests_respect_rate_and_retry_after(tmp_path, monkeypatch):
    session = session_for({"track": None})
    audiodb = AudioDBClient(online_store(tmp_path), SecretStr("123"), session)
    now = 100.0
    waits = []

    async def sleep(delay):
        nonlocal now
        waits.append(delay)
        now += delay

    monkeypatch.setattr(audiodb_module, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(audiodb_module, "asyncio", SimpleNamespace(sleep=sleep))
    await audiodb.search("Artist", "First")
    await audiodb.search("Artist", "Second")
    assert waits == [pytest.approx(2.1)]
    response = session.get.return_value
    response.status = 429
    response.headers = {"Retry-After": "120"}
    with pytest.raises(AudioDBUnavailable):
        await audiodb.search("Artist", "Third")
    response.status = 200
    await audiodb.search("Artist", "Fourth")
    assert waits[-1] == pytest.approx(120)
    assert session.get.call_count == 4


@pytest.mark.parametrize("status", [429, 502, 503])
async def test_upstream_unavailable_waits_at_least_one_minute(tmp_path, monkeypatch, status):
    session = session_for({"track": None}, status=status)
    audiodb = AudioDBClient(online_store(tmp_path), SecretStr("123"), session)
    now = 100.0
    waits = []

    async def sleep(delay):
        nonlocal now
        waits.append(delay)
        now += delay

    monkeypatch.setattr(audiodb_module, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(audiodb_module, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(AudioDBUnavailable) as error:
        await audiodb.search("Artist", "First")
    assert error.value.delay == 60
    session.get.return_value.status = 200
    await audiodb.search("Artist", "Second")
    assert waits == [60]


@pytest.mark.parametrize(
    "invalid_cache",
    ["not json", json.dumps({"tracks": "invalid"}), json.dumps({"tracks": [track(strTrack={})]})],
)
async def test_invalid_cached_response_is_refreshed(tmp_path, invalid_cache, caplog):
    store = online_store(tmp_path)
    session = session_for({"track": [track()]})
    audiodb = AudioDBClient(store, SecretStr("123"), session)
    expected = await audiodb.search("Artist", "Title")
    with store.connect() as db:
        db.execute(
            "UPDATE enrichment_cache SET document=? WHERE isrc LIKE 'theaudiodb:%'",
            (invalid_cache,),
        )
    audiodb.next_lookup = 0
    assert await audiodb.search("Artist", "Title") == expected
    assert session.get.call_count == 2
    assert "Invalid TheAudioDB cache entry; refreshing" in caplog.text


async def test_worker_publishes_only_after_musicbrainz_confirmation(tmp_path, monkeypatch):
    store = online_store(tmp_path)
    payload = document()
    payload["metadata"] = {**METADATA, "album": "Album"}
    store.submit("recover", json.dumps(payload), 10)
    session = session_for({"track": [track()]})
    configuration = online_settings(tmp_path).model_copy(
        update={"theaudiodb_api_key": SecretStr("123")}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(musicbrainz_reply)) as client:
        worker = Worker(store, configuration, client, session)
        monkeypatch.setattr(worker.musicbrainz, "_wait_until", AsyncMock())
        row = await run_until_idle(worker, store, "recover")
    assert row["state"] == "processed"
    with store.connect() as db:
        reference = db.execute("SELECT metadata FROM reference_tracks").fetchone()
    assert json.loads(reference["metadata"])["recording_mbid"] == MBID


async def test_worker_keeps_rate_limited_submission_pending(tmp_path, monkeypatch):
    store = online_store(tmp_path)
    payload = document()
    payload["metadata"] = {**METADATA, "album": "Album"}
    store.submit("recover", json.dumps(payload), 10)
    session = session_for({}, status=429, headers={"Retry-After": "120"})
    configuration = online_settings(tmp_path).model_copy(
        update={"theaudiodb_api_key": SecretStr("123")}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(musicbrainz_reply)) as client:
        worker = Worker(store, configuration, client, session)
        monkeypatch.setattr(worker.musicbrainz, "_wait_until", AsyncMock())
        row = await run_until_idle(worker, store, "recover")
    assert row["state"] == "pending_metadata"
    assert row["error"] == "TheAudioDB HTTP 429"
    with store.connect() as db:
        pending = db.execute(
            "SELECT pending_rechecks, pending_due_at FROM submissions WHERE id='recover'"
        ).fetchone()
        assert pending["pending_rechecks"] == 0
        assert pending["pending_due_at"] > 0
        assert db.execute("SELECT COUNT(*) FROM reference_tracks").fetchone()[0] == 0


async def test_worker_does_not_log_api_key_from_connection_errors(tmp_path, monkeypatch, caplog):
    store = online_store(tmp_path)
    payload = document()
    payload["metadata"] = {**METADATA, "album": "Album"}
    store.submit("recover", json.dumps(payload), 10)
    key = "12345678901234567890"
    session = session_for({})
    session.get.side_effect = aiohttp.ClientConnectionError(f"request URL contained {key}")
    configuration = online_settings(tmp_path).model_copy(
        update={"theaudiodb_api_key": SecretStr(key)}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(musicbrainz_reply)) as client:
        worker = Worker(store, configuration, client, session)
        monkeypatch.setattr(worker.musicbrainz, "_wait_until", AsyncMock())
        row = await run_until_idle(worker, store, "recover")
    assert row["state"] == "pending_metadata"
    assert row["error"] == "TheAudioDB request unavailable"
    assert key not in caplog.text


def test_optional_key_requires_musicbrainz_and_is_not_represented(tmp_path):
    from test_server import settings

    invalid_key = "private-invalid-key"
    with pytest.raises(ValidationError, match="TheAudioDB v1 API key must be numeric") as error:
        settings(tmp_path, theaudiodb_api_key=invalid_key)
    assert invalid_key not in str(error.value)
    with pytest.raises(ValidationError, match="require MusicBrainz"):
        settings(tmp_path, theaudiodb_api_key=SecretStr("123"))
    configuration = settings(
        tmp_path,
        musicbrainz_enabled=True,
        musicbrainz_contact="test@example.invalid",
        theaudiodb_api_key=SecretStr("123"),
    )
    assert "123" not in repr(configuration)
