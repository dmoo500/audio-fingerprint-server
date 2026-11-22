"""Online identifier verification and durable publication transitions."""

import asyncio
import json
import logging
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_metadata_gate import document
from test_server import settings

from fingerprint_server.database.sqlite import Store
from fingerprint_server.musicbrainz import (
    EnrichmentError,
    MusicBrainzService,
    VerificationPending,
)
from fingerprint_server.schemas import Submission
from fingerprint_server.worker import Worker

MBID = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
ISRC = "USAAA2600001"


def recording(mbid: str = MBID, albums: list[str] | None = None) -> dict[str, Any]:
    """Return a representative MusicBrainz recording."""
    titles = ["Album"] if albums is None else albums
    return {
        "id": mbid,
        "title": "Title",
        "artist-credit": [{"name": "Artist", "artist": {"name": "Artist"}}],
        "isrcs": [ISRC],
        "releases": [
            {
                "title": title,
                "release-group": {
                    "primary-type": "Album",
                    "secondary-types": [],
                },
            }
            for title in titles
        ],
        "release-count": len(titles),
    }


def api(request: httpx.Request) -> httpx.Response:
    """Serve deterministic recording and ISRC lookups."""
    if "/recording/" in request.url.path:
        return httpx.Response(200, json=recording())
    return httpx.Response(
        200, json={"isrc": ISRC, "recording-count": 1, "recordings": [recording()]}
    )


def online_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    return store


def online_settings(tmp_path: Path) -> Any:
    return settings(tmp_path, musicbrainz_enabled=True, musicbrainz_contact="test@example.invalid")


async def run_until_idle(
    worker: Worker,
    store: Store,
    identifier: str,
    *,
    completed: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    task = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(8):
            while True:
                row = store.get_submission(identifier)
                if (
                    row
                    and row["state"] not in ("queued", "processing")
                    and not (row["state"] == "pending_metadata" and row["error"] is None)
                    and (completed is None or completed())
                ):
                    return row
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("identifier", [{"recording_mbid": MBID}, {"isrc": ISRC}])
async def test_identifier_fills_only_missing_fields(
    tmp_path: Path, identifier: dict[str, str]
) -> None:
    store = online_store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        metadata = {"title": " Supplied ", "duration": 12.0, **identifier}
        result = await worker.musicbrainz.verify(metadata)
        assert result["title"] == " Supplied "
        assert result["artists"] == ["Artist"]
        assert result["album"] == "Album"
        assert result["recording_mbid"] == MBID
        assert result["duration"] == 12.0
        assert metadata == {"title": " Supplied ", "duration": 12.0, **identifier}


@pytest.mark.parametrize("identifier", [{"recording_mbid": MBID}, {"isrc": ISRC}])
async def test_unique_album_excludes_singles_and_compilations(tmp_path, identifier):
    source = recording()
    source["releases"] = [
        {"title": "Single", "release-group": {"primary-type": "Single"}},
        {
            "title": "Sampler",
            "release-group": {"primary-type": "Album", "secondary-types": ["Compilation"]},
        },
        {
            "title": "Studio",
            "release-group": {"primary-type": "Album", "secondary-types": []},
        },
    ]
    source["release-count"] = len(source["releases"])

    def reply(request):
        if "/recording/" in request.url.path:
            return httpx.Response(200, json=source)
        return httpx.Response(200, json={"isrc": ISRC, "recordings": [source]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        result = await service.verify(identifier)
        assert result["album"] == "Studio"
        supplied = await service.verify({**identifier, "album": "Sampler"})
        assert supplied["album"] == "Sampler"


@pytest.mark.parametrize(
    "releases,release_count",
    [
        ([{"title": "Single", "release-group": {"primary-type": "Single"}}], 1),
        (
            [
                {
                    "title": "Sampler",
                    "release-group": {
                        "primary-type": "Album",
                        "secondary-types": ["Compilation"],
                    },
                }
            ],
            1,
        ),
        (
            [
                {"title": "Studio", "release-group": {"primary-type": "Album"}},
            ],
            1,
        ),
        (
            [
                {
                    "title": "Studio",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                },
                {"title": "Unknown"},
            ],
            2,
        ),
        (
            [
                {
                    "title": "Studio",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                }
            ],
            2,
        ),
        (
            [
                {
                    "title": name,
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                }
                for name in ("Studio", "Other")
            ],
            2,
        ),
    ],
)
def test_unresolved_album_is_not_guessed(releases, release_count):
    result = MusicBrainzService.parse_recording(
        {**recording(), "releases": releases, "release-count": release_count}
    )
    assert "album" not in result


async def test_worker_publishes_only_after_unique_non_compilation_album(tmp_path):
    source = recording()
    source["releases"] = [
        {"title": "Single", "release-group": {"primary-type": "Single"}},
        {
            "title": "Sampler",
            "release-group": {"primary-type": "Album", "secondary-types": ["Compilation"]},
        },
        {
            "title": "Studio",
            "release-group": {"primary-type": "Album", "secondary-types": []},
        },
    ]
    source["release-count"] = len(source["releases"])
    payload = document()
    payload["metadata"].pop("album")
    store = online_store(tmp_path)
    store.submit("submission", json.dumps(payload), 10)

    def reply(request):
        if "/isrc/" in request.url.path:
            return httpx.Response(200, json={"isrc": ISRC, "recordings": [source]})
        return httpx.Response(200, json=source)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        row = await run_until_idle(
            Worker(store, online_settings(tmp_path), client), store, "submission"
        )
    assert row["state"] == "processed"
    with store.connect() as db:
        saved = db.execute(
            "SELECT metadata FROM reference_tracks WHERE reference_key=?", (row["reference_id"],)
        ).fetchone()
    assert json.loads(saved["metadata"])["album"] == "Studio"


async def test_agreement_and_conflict(tmp_path: Path) -> None:
    store = online_store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        assert (await worker.musicbrainz.verify({"recording_mbid": MBID, "isrc": ISRC}))[
            "isrc"
        ] == ISRC

    def conflict(request: httpx.Request) -> httpx.Response:
        if "/recording/" in request.url.path:
            return httpx.Response(200, json=recording())
        return httpx.Response(
            200, json={"isrc": ISRC, "recording-count": 1, "recordings": [recording(OTHER)]}
        )

    other_store = Store(tmp_path / "other.db", True)
    other_store.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(conflict)) as client:
        worker = Worker(other_store, online_settings(tmp_path), client)
        with pytest.raises(EnrichmentError, match="conflict"):
            await worker.musicbrainz.verify({"recording_mbid": MBID, "isrc": ISRC})


@pytest.mark.parametrize("status", [404, 301])
async def test_missing_and_redirect_not_approved(tmp_path: Path, status: int) -> None:
    store = online_store(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status))
    ) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        with pytest.raises(EnrichmentError):
            await worker.musicbrainz.verify({"recording_mbid": MBID})


@pytest.mark.parametrize("body", [b"{}", b"[]", b"not-json", b"x" * 2_000_001])
async def test_malformed_and_oversized_results_not_cached(tmp_path: Path, body: bytes) -> None:
    store = online_store(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    ) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        with pytest.raises(EnrichmentError):
            await worker.musicbrainz.verify({"recording_mbid": MBID})
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM enrichment_cache").fetchone()[0] == 0


async def test_partial_recording_and_release_lists_are_not_guessed(tmp_path: Path) -> None:
    store = online_store(tmp_path)
    partial = recording()
    partial["release-count"] = 2
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"isrc": ISRC, "recording-count": 2, "recordings": [partial]}
            )
        )
    ) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        with pytest.raises(EnrichmentError, match="ambiguous"):
            await worker.musicbrainz.verify({"isrc": ISRC})
        assert "album" not in worker.musicbrainz.parse_recording(partial)


@pytest.mark.parametrize("complete_metadata", [True, False])
async def test_multiple_isrc_recordings_confirm_identifier_without_guessing_metadata(
    tmp_path: Path, complete_metadata: bool
) -> None:
    store = online_store(tmp_path)
    payload = document()
    payload["metadata"]["isrc"] = ISRC
    payload["metadata"].pop("recording_mbid", None)
    payload["metadata"]["title"] = "Unrelated"
    if not complete_metadata:
        payload["metadata"].pop("album", None)
    supplied = dict(payload["metadata"])
    store.submit("submission", json.dumps(payload), 10)

    def reply(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "isrc": ISRC,
                "recording-count": 2,
                "recordings": [recording(), recording(OTHER)],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        assert await worker.musicbrainz.verify(supplied) == supplied
        row = await run_until_idle(worker, store, "submission")
    assert row["state"] == ("processed" if complete_metadata else "pending_metadata")
    with store.connect() as db:
        references = db.execute("SELECT metadata,recording_key FROM reference_tracks").fetchall()
    if complete_metadata:
        assert len(references) == 1
        assert json.loads(references[0]["metadata"]) == supplied
        assert references[0]["recording_key"] is None
    else:
        assert "Incomplete metadata" in row["error"]
        assert references == []


async def test_multiple_isrc_selects_first_matching_candidate(tmp_path: Path) -> None:
    store = online_store(tmp_path)
    wrong_title = {**recording(OTHER), "title": "Different"}
    wrong_artist = {**recording(OTHER), "artist-credit": [{"name": "Someone else"}]}
    wrong_duration = {**recording(OTHER), "length": 22000}
    correct = {**recording(MBID), "length": 12000}
    reply = {
        "isrc": ISRC,
        "recording-count": 5,
        "recordings": [wrong_title, wrong_artist, wrong_duration, correct, recording(OTHER)],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply))
    ) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        result = await worker.musicbrainz.verify(
            {"isrc": ISRC, "title": "Title", "artists": ["Artist"], "duration": 12.0}
        )
    assert result["recording_mbid"] == MBID


@pytest.mark.parametrize("length", [-1, 1.5, True, "12000"])
def test_invalid_recording_length_rejected(length: object) -> None:
    with pytest.raises(ValueError, match="length"):
        MusicBrainzService.parse_recording({**recording(), "length": length})


async def test_multiple_candidates_without_io(monkeypatch: pytest.MonkeyPatch) -> None:
    service = object.__new__(MusicBrainzService)
    service.batch_isrcs = {}
    service.batch_candidates = {}
    candidates = [
        service.parse_recording({**recording(OTHER), "title": "Different"}),
        service.parse_recording({**recording(OTHER), "length": 22000}),
        service.parse_recording({**recording(MBID), "length": 12000}),
        service.parse_recording(recording(OTHER)),
    ]
    response = {"status": "resolved", "recordings": candidates, "complete": True}

    async def lookup(kind: str, identifier: str) -> dict[str, Any]:
        assert kind == "isrc" and identifier == ISRC
        return response

    monkeypatch.setattr(service, "lookup", lookup)
    metadata = {"isrc": ISRC, "title": "Title", "artists": ["Artist"], "duration": 12.0}
    assert (await service.verify(metadata))["recording_mbid"] == MBID
    no_match = {**metadata, "title": "Unrelated"}
    assert await service.verify(no_match) == no_match
    response["complete"] = False
    with pytest.raises(VerificationPending):
        await service.verify(metadata)


@pytest.mark.parametrize("conflict", [False, True])
def test_existing_reference_association_preserves_metadata(
    monkeypatch: pytest.MonkeyPatch, conflict: bool
) -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE reference_tracks (id INTEGER PRIMARY KEY,reference_key TEXT UNIQUE,"
        "metadata TEXT,created_at REAL,publication_ready INTEGER,verification_version INTEGER,"
        "recording_key TEXT);"
        "CREATE TABLE submissions (id TEXT,state TEXT,reference_id TEXT,error TEXT,"
        "background_kind INTEGER,pending_check INTEGER);"
        "CREATE TABLE landmarks (hash BLOB,reference_id INTEGER,offset INTEGER);"
    )
    metadata = document()["metadata"]
    db.execute(
        "INSERT INTO reference_tracks VALUES(1,'reference',?,0,1,1,NULL)",
        (json.dumps(metadata),),
    )
    db.execute("INSERT INTO submissions VALUES('submission','processing',NULL,NULL,0,0)")
    db.commit()
    store = object.__new__(Store)
    store.musicbrainz_enabled = True

    @contextmanager
    def connect() -> Iterator[sqlite3.Connection]:
        with db:
            yield db

    monkeypatch.setattr(store, "connect", connect)
    supplied = {**metadata, "recording_mbid": MBID}
    if conflict:
        supplied["title"] = "Different"
    store.finish("submission", "reference", supplied, [], verified=True)
    row = db.execute("SELECT metadata,recording_key FROM reference_tracks").fetchone()
    expected = metadata if conflict else {**metadata, "recording_mbid": MBID}
    assert json.loads(row["metadata"]) == expected
    assert row["recording_key"] == (None if conflict else MBID.replace("-", ""))
    db.close()


async def test_multiple_isrc_recordings_still_check_supplied_mbid(tmp_path: Path) -> None:
    store = online_store(tmp_path)

    def reply(request: httpx.Request) -> httpx.Response:
        if "/recording/" in request.url.path:
            return httpx.Response(200, json=recording())
        return httpx.Response(
            200,
            json={
                "isrc": ISRC,
                "recordings": [recording(OTHER), recording(MBID)],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        result = await worker.musicbrainz.verify({"isrc": ISRC, "recording_mbid": MBID})
        assert result["recording_mbid"] == MBID


async def test_ambiguous_album_remains_pending(tmp_path: Path) -> None:
    store = online_store(tmp_path)
    payload = document()
    payload["metadata"] = {"duration": 12.0, "isrc": ISRC}
    store.submit("submission", json.dumps(payload), 10)

    def reply(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "isrc": ISRC,
                "recording-count": 1,
                "recordings": [recording(albums=["A", "B"])],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        row = await run_until_idle(
            Worker(store, online_settings(tmp_path), client), store, "submission"
        )
    assert row["state"] == "pending_metadata"
    assert "Incomplete metadata" in row["error"]
    assert store.match([("0" * 20, 0)]) == []


async def test_cache_namespace_negative_cache_and_pacing(tmp_path: Path) -> None:
    store = online_store(tmp_path)
    store.cache_enrichment(ISRC, {"status": "resolved", "recording_mbid": OTHER})
    calls: list[float] = []

    def reply(_: httpx.Request) -> httpx.Response:
        calls.append(time.monotonic())
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        await asyncio.gather(
            worker.musicbrainz.lookup("isrc", ISRC), worker.musicbrainz.lookup("recording", MBID)
        )
        await worker.musicbrainz.lookup("isrc", ISRC)
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 1.09


async def test_transition_withholds_republishes_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    payload = document()
    payload["metadata"]["recording_mbid"] = MBID
    submission = Submission.model_validate_json(json.dumps(payload))
    offline = Store(path)
    offline.initialize()
    offline.submit("old", json.dumps(payload), 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        Worker(offline, settings(tmp_path), client).publish("old", submission, payload["metadata"])
    assert offline.match([("0" * 20, 0)])
    online = online_store(tmp_path)
    assert not online.match([("0" * 20, 0)])
    assert online.get_submission("old")["state"] == "pending_metadata"
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        row = await run_until_idle(Worker(online, online_settings(tmp_path), client), online, "old")
    assert row["state"] == "processed"
    assert online.match([("0" * 20, 0)])[0]["metadata"] == payload["metadata"]
    online.initialize()
    assert online.get_submission("old")["state"] == "processed"
    offline.initialize()
    online.initialize()
    assert online.get_submission("old")["state"] == "processed"
    assert online.match([("0" * 20, 0)])


async def test_transition_preserves_retained_metadata_and_pending_documents(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    payload = document()
    submission = Submission.model_validate_json(json.dumps(payload))
    offline = Store(path)
    offline.initialize()
    offline.submit("old", json.dumps(payload), 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        Worker(offline, settings(tmp_path), client).publish("old", submission, payload["metadata"])
    incomplete = document()
    incomplete["metadata"] = {"duration": 12.0}
    pending = document()
    pending["metadata"] = {"duration": 12.0, "isrc": ISRC}
    no_id = document()
    no_id["metadata"] = {"duration": 12.0}
    with offline.connect() as db:
        db.execute("UPDATE submissions SET document=? WHERE id='old'", (json.dumps(incomplete),))
    offline.submit("pending", json.dumps(pending), 10)
    offline.submit("no-id", json.dumps(no_id), 10)

    online = online_store(tmp_path)
    assert online.get_submission("old")["state"] == "pending_metadata"
    assert online.get_submission("pending")["state"] == "pending_metadata"
    assert online.get_submission("no-id")["state"] == "pending_metadata"
    assert online.statistics()["references"]["excluded"] == 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        row = await run_until_idle(Worker(online, online_settings(tmp_path), client), online, "old")
    assert row["state"] == "processed"
    assert online.match([("0" * 20, 0)])[0]["metadata"]["title"] == payload["metadata"]["title"]
    with online.connect() as db:
        assert db.execute("SELECT document FROM submissions WHERE id='old'").fetchone()[0] == (
            json.dumps(incomplete)
        )
        assert db.execute("SELECT count(*) FROM landmarks").fetchone()[0] == 1


async def test_disable_restores_offline_gate_after_unresolved_lookup(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    online = online_store(tmp_path)
    payload = document()
    online.submit("new", json.dumps(payload), 10)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as client:
        row = await run_until_idle(Worker(online, online_settings(tmp_path), client), online, "new")
    assert row["state"] == "pending_metadata"
    assert "not found" in row["error"]
    assert not online.match([("0" * 20, 0)])

    offline = Store(path)
    offline.initialize()
    assert offline.get_submission("new")["state"] == "queued"
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        row = await run_until_idle(Worker(offline, settings(tmp_path), client), offline, "new")
    assert row["state"] == "processed"
    assert offline.match([("0" * 20, 0)])
    online.initialize()
    assert not online.match([("0" * 20, 0)])
    assert online.get_submission("new")["state"] == "pending_metadata"


async def test_exhausted_transient_failure_is_not_cached(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = online_store(tmp_path)
    store.submit("retry", json.dumps(document()), 10)
    with store.connect() as db:
        db.execute("UPDATE submissions SET attempts=2 WHERE id='retry'")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as client:
        row = await run_until_idle(Worker(store, online_settings(tmp_path), client), store, "retry")
    assert row["state"] == "failed"
    assert row["attempts"] == 3
    assert row["error"] == "Metadata lookup unavailable"
    assert "HTTP status=500; attempt=3; retry_seconds=None" in caplog.text
    assert f"api_call=GET /isrc/{ISRC}" in caplog.text
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM enrichment_cache").fetchone()[0] == 0


async def test_publication_logged_only_after_verification(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="uvicorn.error.fingerprint.worker")
    caplog.set_level(logging.INFO, logger="fingerprint_server.worker")
    store = online_store(tmp_path)
    store.submit("valid", json.dumps(document()), 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        result = await run_until_idle(
            Worker(store, online_settings(tmp_path), client),
            store,
            "valid",
            completed=lambda: (
                "Submission valid verified with MusicBrainz and published" in caplog.text
            ),
        )
    assert result["state"] == "processed"
    assert "Submission valid verified with MusicBrainz and published" in caplog.text
    caplog.clear()
    missing = document()
    missing["fingerprints"] = [("1" * 20, 0)]
    missing["metadata"].pop("isrc", None)
    missing["metadata"]["recording_mbid"] = OTHER
    store.submit("missing", json.dumps(missing), 10)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as client:
        result = await run_until_idle(
            Worker(store, online_settings(tmp_path), client), store, "missing"
        )
    assert result["state"] == "pending_metadata"
    assert "verified with MusicBrainz and published" not in caplog.text
    assert f"api_call=GET /recording/{OTHER}" in caplog.text


async def test_conflict_cannot_replace_verified_acoustic_duplicate(tmp_path: Path) -> None:
    store = online_store(tmp_path)
    first = document()
    first["metadata"]["recording_mbid"] = MBID
    store.submit("first", json.dumps(first), 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        assert (await run_until_idle(worker, store, "first"))["state"] == "processed"
        second = document()
        second["metadata"]["recording_mbid"] = OTHER
        store.submit("second", json.dumps(second), 10)
        task = asyncio.create_task(worker.run())
        try:
            async with asyncio.timeout(5):
                while store.get_submission("second") is not None:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert store.match([("0" * 20, 0)])[0]["metadata"]["recording_mbid"] == MBID


@pytest.mark.parametrize("corrupt", ["not-json", "[]"])
async def test_corrupt_retained_metadata_is_not_replaced(tmp_path: Path, corrupt: str) -> None:
    store = online_store(tmp_path)
    payload = document()
    store.submit("first", json.dumps(payload), 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        assert (await run_until_idle(worker, store, "first"))["state"] == "processed"
        with store.connect() as db:
            db.execute("UPDATE reference_tracks SET metadata=?,publication_ready=0", (corrupt,))
        store.submit("second", json.dumps(payload), 10)
        result = await run_until_idle(worker, store, "second")
    assert result["state"] == "pending_metadata"
    assert result["error"] == "Retained reference metadata is invalid"
    assert not store.match([("0" * 20, 0)])
    with store.connect() as db:
        assert db.execute("SELECT metadata FROM reference_tracks").fetchone()[0] == corrupt


async def test_verification_version_upgrade_withholds_once(tmp_path: Path) -> None:
    store = online_store(tmp_path)
    store.submit("first", json.dumps(document()), 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        assert (
            await run_until_idle(Worker(store, online_settings(tmp_path), client), store, "first")
        )["state"] == "processed"
    with store.connect() as db:
        db.execute("UPDATE reference_tracks SET verification_version=0")
        db.execute("UPDATE service_state SET value='0' WHERE key='verification_version'")
    store.initialize()
    assert not store.match([("0" * 20, 0)])
    assert store.get_submission("first")["state"] == "pending_metadata"
    store.initialize()
    assert store.get_submission("first")["state"] == "pending_metadata"
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        assert (
            await run_until_idle(Worker(store, online_settings(tmp_path), client), store, "first")
        )["state"] == "processed"
    store.initialize()
    assert store.get_submission("first")["state"] == "processed"
    assert store.match([("0" * 20, 0)])


def test_version_three_wal_backup_includes_committed_rows(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    store = Store(path)
    store.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("DROP INDEX references_recording")
        connection.execute("ALTER TABLE reference_tracks DROP COLUMN recording_key")
        connection.execute("ALTER TABLE reference_tracks DROP COLUMN verification_version")
        connection.execute("DROP TABLE service_state")
        connection.execute("DROP INDEX submissions_fifo")
        connection.execute("DROP INDEX submissions_pending_due")
        for column in (
            "pending_rechecks",
            "pending_due_at",
            "pending_check",
            "enqueued_at",
            "background_kind",
        ):
            connection.execute(f"ALTER TABLE submissions DROP COLUMN {column}")
        connection.execute("PRAGMA user_version=3")
        connection.execute(
            "INSERT INTO submissions(id,document,state,created_at) VALUES('wal','{}','queued',0)"
        )
        connection.commit()
        assert path.with_name(path.name + "-wal").stat().st_size > 0
        store.initialize()
    backup = next(tmp_path.glob("*.bak"))
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT id FROM submissions").fetchall() == [("wal",)]
    assert store.get_submission("wal")["state"] == "queued"
