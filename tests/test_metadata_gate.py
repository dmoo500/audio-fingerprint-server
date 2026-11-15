"""Metadata publication gate, durable pending work and safe legacy migration."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_server import READ, UPLOAD, canonical, settings

from fingerprint_server.app import create_app
from fingerprint_server.schemas import Submission, metadata_complete
from fingerprint_server.store import QueueFullError, Store
from fingerprint_server.worker import Worker

LEGACY_SCHEMA = """
CREATE TABLE submissions (
    id TEXT PRIMARY KEY, document TEXT NOT NULL, state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
    error TEXT, reference_id TEXT, created_at REAL NOT NULL
);
CREATE INDEX submissions_pending ON submissions(state,retry_at,created_at);
CREATE TABLE reference_tracks (
    id TEXT PRIMARY KEY, metadata TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE landmarks (
    hash TEXT NOT NULL, reference_id TEXT NOT NULL REFERENCES reference_tracks(id),
    offset INTEGER NOT NULL, PRIMARY KEY(hash,reference_id,offset)
);
CREATE TABLE enrichment_cache (
    isrc TEXT PRIMARY KEY, document TEXT NOT NULL, expires_at REAL NOT NULL
);
"""


def metadata() -> dict[str, Any]:
    """Complete, deliberately untrimmed supplied source metadata."""
    return {
        "title": " Title ",
        "artists": [" ", " Artist "],
        "album": " Album ",
        "duration": 12.0,
        "isrc": "USAAA2600001",
    }


def document() -> dict[str, Any]:
    """Small valid landmark document without running acoustic extraction."""
    return {
        "format_id": "ma-dejavu-landmarks-v2",
        "sample_rate": 44100,
        "offset_unit": "fft_hops",
        "windows": [{"start_sample": 0, "sample_count": 12 * 44100}],
        "fingerprints": [["0" * 20, 0]],
        "metadata": metadata(),
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", None),
        ("title", ""),
        ("title", " \t\n"),
        ("artists", []),
        ("artists", ["", " \t"]),
        ("artists", None),
        ("artists", "Artist"),
        ("album", None),
        ("album", ""),
        ("album", " \n"),
        ("duration", None),
        ("duration", 0),
        ("duration", -1),
        ("duration", float("nan")),
        ("duration", float("inf")),
        ("duration", float("-inf")),
        ("duration", True),
        ("duration", "12"),
        ("duration", 10**1000),
        ("isrc", None),
        ("isrc", ""),
        ("isrc", "US-AAA-26-00001"),
        ("isrc", "USAAA2600001\n"),
    ],
)
def test_incomplete_metadata(field: str, value: Any) -> None:
    """Each required field independently gates publication without altering its value."""
    supplied = metadata()
    supplied[field] = value
    assert not metadata_complete(supplied)
    assert supplied[field] is value


@pytest.mark.parametrize("value", [None, [], "", 12, {}])
def test_missing_legacy_metadata_is_safe(value: Any) -> None:
    """Malformed historical metadata cannot throw or qualify for recognition."""
    assert not metadata_complete(value)


@pytest.mark.parametrize(
    ("isrc", "mbid", "complete"),
    [
        ("usaaa2600001", None, True),
        (None, "00000000-0000-4000-8000-000000000001", True),
        ("invalid", "00000000-0000-4000-8000-000000000001", True),
        ("USAAA2600001", "invalid", True),
        (None, "invalid", False),
        (None, "", False),
        (None, None, False),
    ],
)
def test_either_valid_identifier_is_sufficient(
    isrc: str | None, mbid: str | None, complete: bool
) -> None:
    """Publication requires an ISRC or recording UUID, not both."""
    supplied = {**metadata(), "isrc": isrc, "recording_mbid": mbid}
    assert metadata_complete(supplied) is complete


@pytest.mark.parametrize("missing", ["title", "artists", "album", "isrc", "duration"])
async def test_pending_api_state_survives_restart(tmp_path: Path, missing: str) -> None:
    """Valid incomplete documents stay durable, idle, explicitly pending and unrecognized."""
    payload = document()
    del payload["metadata"][missing]
    body, identifier = canonical(payload)
    configuration = settings(tmp_path)
    for _ in range(2):
        application = create_app(configuration)
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/dejavu/submissions",
                    content=body,
                    headers={
                        "X-API-Key": UPLOAD,
                        "Idempotency-Key": identifier,
                        "Content-Type": "application/json",
                    },
                )
                assert response.status_code == 202
                assert response.json()["status"] == "pending_metadata"
                status = await client.get(
                    f"/api/v1/dejavu/submissions/{identifier}", headers={"X-API-Key": UPLOAD}
                )
                assert status.json()["state"] == "pending_metadata"
                assert status.json()["attempts"] == 0
                assert status.json()["error"] == "Incomplete metadata"
                assert status.json()["reference_id"] is None
                assert application.state.store.claim() is None
                assert application.state.store.match([("0" * 20, 0)]) == []
                statistics = await client.get(
                    "/api/v1/dejavu/statistics", headers={"X-API-Key": READ}
                )
                assert statistics.json()["submissions"]["pending_metadata"] == 1
                assert statistics.json()["references"]["total"] == 0
                with application.state.store.connect() as db:
                    assert (
                        db.execute("SELECT document FROM submissions").fetchone()[0]
                        == body.decode()
                    )
                    assert db.execute("SELECT count(*) FROM reference_tracks").fetchone()[0] == 0


def legacy_store(path: Path, supplied: str, payload: str = "{}") -> Store:
    """Populate only an isolated fixture with the schema from before the publication gate."""
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA)
        db.execute("INSERT INTO reference_tracks VALUES('legacy',?,0)", (supplied,))
        db.execute("INSERT INTO landmarks VALUES(?,'legacy',0)", ("0" * 20,))
        db.execute(
            "INSERT INTO submissions(id,document,state,reference_id,created_at) "
            "VALUES('old',?,'processed','legacy',0)",
            (payload,),
        )
    return Store(path)


@pytest.mark.parametrize(
    "supplied",
    [
        "{}",
        "null",
        "[]",
        '"text"',
        "not JSON",
        '{"title":"only title"}',
        json.dumps({**metadata(), "album": " "}),
        json.dumps({**metadata(), "duration": float("nan")}),
    ],
)
def test_legacy_incomplete_references_excluded_without_deletion(
    tmp_path: Path, supplied: str
) -> None:
    """Migration is repeatable, retains all old content and excludes it before matching."""
    store = legacy_store(tmp_path / "legacy.db", supplied)
    for _ in range(2):
        store.initialize()
        assert store.match([("0" * 20, 0)]) == []
        assert store.get_submission("old")["state"] == "pending_metadata"
        assert store.statistics()["references"] == {
            "total": 0,
            "partial": 0,
            "full": 0,
            "excluded": 1,
        }
        with store.connect() as db:
            assert db.execute("SELECT metadata FROM reference_tracks").fetchone()[0] == supplied
            assert db.execute("SELECT count(*) FROM landmarks").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM submissions").fetchone()[0] == 1


def test_legacy_complete_reference_remains_searchable(tmp_path: Path) -> None:
    """A missing historical submission document does not hide a complete reference."""
    supplied = metadata()
    supplied["musicbrainz"] = {"status": "not_found"}
    store = legacy_store(tmp_path / "legacy.db", json.dumps(supplied), "not JSON")
    store.initialize()
    assert store.match([("0" * 20, 0)])[0]["metadata"] == supplied
    assert store.get_submission("old")["state"] == "processed"
    assert store.statistics()["references"] == {"total": 1, "partial": 1, "full": 0, "excluded": 0}


def test_interrupted_publication_backfill_recovers(tmp_path: Path) -> None:
    """An existing column without its backfill does not permanently hide complete references."""
    store = legacy_store(tmp_path / "legacy.db", json.dumps(metadata()))
    with store.connect() as db:
        db.execute(
            "ALTER TABLE reference_tracks ADD COLUMN publication_ready INTEGER NOT NULL DEFAULT 0"
        )
    for _ in range(2):
        store.initialize()
        assert store.match([("0" * 20, 0)])[0]["metadata"] == metadata()
        assert store.get_submission("old")["state"] == "processed"


def test_store_publish_defense_and_pending_capacity(tmp_path: Path) -> None:
    """Incomplete metadata cannot merge into a published reference or bypass queue limits."""
    store = Store(tmp_path / "gate.db")
    store.initialize()
    payload = document()
    store.submit("complete", json.dumps(payload), 1)
    store.finish("complete", "reference", payload["metadata"], [("0" * 20, 0)])
    incomplete = {**payload["metadata"], "title": " "}
    store.submit("pending", json.dumps({**payload, "metadata": incomplete}), 1)
    store.finish("pending", "reference", incomplete, [("0" * 20, 0)])
    assert store.get_submission("pending")["state"] == "pending_metadata"
    assert store.get_submission("pending")["reference_id"] is None
    assert store.match([("0" * 20, 0)])[0]["metadata"] == payload["metadata"]
    assert store.submit("pending", "ignored", 1)["state"] == "pending_metadata"
    with pytest.raises(QueueFullError):
        store.submit("another", json.dumps(payload), 1)
    with pytest.raises(QueueFullError):
        store.submit("another", json.dumps(payload), 10, byte_limit=1)


def test_exact_duplicate_promotes_excluded_reference_without_acoustic_merge(tmp_path: Path) -> None:
    """A complete resubmission repairs only the exact reference, keeping original rows."""
    store = legacy_store(tmp_path / "legacy.db", "{}")
    store.initialize()
    payload = document()
    store.submit("new", json.dumps(payload), 10)
    store.finish("new", "legacy", payload["metadata"], [("0" * 20, 0)])
    assert store.get_submission("new")["state"] == "processed"
    assert store.get_submission("old")["state"] == "pending_metadata"
    assert store.match([("0" * 20, 0)])[0]["metadata"] == payload["metadata"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM reference_tracks").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM landmarks").fetchone()[0] == 1
        assert db.execute("SELECT document FROM submissions WHERE id='old'").fetchone()[0] == "{}"
    store.initialize()
    assert store.match([("0" * 20, 0)])
    store.submit("later", json.dumps(payload), 10)
    store.finish("later", "legacy", {**payload["metadata"], "title": "Conflict"}, [])
    assert store.match([("0" * 20, 0)])[0]["metadata"] == payload["metadata"]


async def test_worker_defers_unresolved_legacy_incomplete_queue(tmp_path: Path) -> None:
    """Pre-gate queued work is looked up and retained when the identifier is not found."""
    configuration = settings(
        tmp_path, musicbrainz_enabled=True, musicbrainz_contact="test@example.invalid"
    )
    store = Store(configuration.database)
    store.initialize()
    payload = document()
    payload["metadata"]["album"] = " "
    with store.connect() as db:
        db.execute(
            "INSERT INTO submissions(id,document,state,created_at) VALUES('old',?,'queued',0)",
            (json.dumps(payload),),
        )

    def no_match(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_match)) as client:
        worker = Worker(store, configuration, client)
        task = asyncio.create_task(worker.run())
        try:
            async with asyncio.timeout(5):
                while store.get_submission("old")["state"] != "pending_metadata":
                    await asyncio.sleep(0.01)
            assert store.get_submission("old")["attempts"] == 1
            assert store.claim() is None
            assert store.match([("0" * 20, 0)]) == []
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_worker_publishes_supplied_metadata_unchanged(tmp_path: Path) -> None:
    """The shared gate checks whitespace without trimming the published source fields."""
    store = Store(tmp_path / "source.db")
    store.initialize()
    payload = document()
    store.submit("new", json.dumps(payload), 10)
    async with httpx.AsyncClient() as client:
        worker = Worker(store, settings(tmp_path), client)
        submission = Submission.model_validate_json(json.dumps(payload))
        worker.publish("new", submission, submission.metadata.model_dump(exclude_none=True))
    assert store.match([("0" * 20, 0)])[0]["metadata"] == payload["metadata"]
