"""Exercise the actual PostgreSQL backend and split runtime in disposable storage."""

from __future__ import annotations

import asyncio
import json
import os
import runpy
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp_client import AsyncClient
from audio_fingerprint import HOP_SIZE, SAMPLE_RATE
from pydantic import ValidationError
from test_admin_history import assert_hourly_sampling
from test_availability import MBID, publish
from test_metadata_gate import document
from test_postgres_migration import migration as migration
from test_postgres_migration import postgres as postgres
from test_postgres_migration import source as source
from test_server import READ, UPLOAD, audio, canonical, recognize, settings, submit
from test_server import document as acoustic_document

from fingerprint_server.api_state import OWNERSHIP_TASK, STORE, WORKER_TASK
from fingerprint_server.app import create_app
from fingerprint_server.database import create_store
from fingerprint_server.database.codes import SubmissionState
from fingerprint_server.database.sqlite import QueryTooBroadError, QueueFullError, Store
from fingerprint_server.runtime import runtime
from fingerprint_server.schemas import acoustic_reference_id, decode_submission


def test_artwork_additive_migration_and_queue(store) -> None:
    from fingerprint_server.artwork import ArtworkStore

    queue = ArtworkStore(store)
    queue.initialize()
    payload = document()
    payload["metadata"]["recording_mbid"] = MBID
    store.submit("cover-job", json.dumps(payload), 10)
    store.finish("cover-job", "a" * 64, payload["metadata"], [("0" * 20, 0)])
    assert queue.request("a" * 64)["status"] == "pending"
    store.initialize()
    queue.initialize()
    assert queue.next()["reference_key"] == "a" * 64
    queue.finish("a" * 64, "https://archive.org/example.jpg")
    assert queue.request("a" * 64) == {
        "status": "ready",
        "url": "https://archive.org/example.jpg",
    }


@pytest.mark.parametrize("legacy_state", ["unexpected", "", "~0"])
def test_legacy_postgres_artwork_state_is_preserved(
    configuration, postgres, legacy_state: str
) -> None:
    from fingerprint_server.artwork import ArtworkStore
    from fingerprint_server.database.codes import (
        ARTWORK_REASONS,
        ArtworkState,
        decode_artwork_state,
    )
    from fingerprint_server.database.postgres import PostgresStore

    store = PostgresStore(configuration)
    store.initialize()
    with store.connect(write=True) as db:
        db.execute(
            "CREATE TABLE artwork_requests(reference_key text PRIMARY KEY,state text NOT NULL,"
            "url text,due_at double precision NOT NULL,expires_at double precision NOT NULL,"
            "reason text)"
        )
        db.execute(
            "INSERT INTO artwork_requests VALUES('legacy',%s,NULL,0,100,'~0')", (legacy_state,)
        )
        db.execute("INSERT INTO service_state VALUES('artwork_schema','3')")
    queue = ArtworkStore(store)
    queue.initialize()
    queue.initialize()
    with store.connect() as db:
        row = db.execute("SELECT state,state_raw,reason FROM artwork_requests").fetchone()
    assert row["state"] == ArtworkState.UNKNOWN
    assert decode_artwork_state(row["state"], row["state_raw"]) == legacy_state
    assert ARTWORK_REASONS.decode(row["reason"]) == "~0"


@pytest.fixture
def configuration(tmp_path: Path, postgres: object):
    return settings(tmp_path, database_backend="postgres", postgres_schema=f"test_{uuid4().hex}")


@pytest.fixture(params=[1, 2])
def store(configuration, request, monkeypatch):
    from fingerprint_server.database.postgres import PostgresStore

    result = PostgresStore(configuration)
    result.initialize()
    if request.param == 2:
        import psycopg

        scripts = Path(__file__).resolve().parents[1] / "scripts"
        monkeypatch.syspath_prepend(str(scripts))
        migrate = runpy.run_path(str(scripts / "migrate_postgres_layout.py"))["migrate"]
        with psycopg.connect("") as db:
            migrate(db, configuration.postgres_schema, 2)
        result.initialize()
    return result


def test_postgres_hourly_sampling(store) -> None:
    assert_hourly_sampling(store)


async def test_committed_notifications_include_recognition_and_exclude_rollbacks(
    configuration, postgres
) -> None:
    import psycopg
    from psycopg import sql

    from fingerprint_server.admin.data import AdminData
    from fingerprint_server.admin.live import AdminLive
    from fingerprint_server.database.notifications import postgres_channel
    from fingerprint_server.database.postgres import PostgresStore
    from fingerprint_server.database.recognition_metrics import record_attempt

    store = PostgresStore(configuration)
    await asyncio.to_thread(store.initialize)
    listener = await psycopg.AsyncConnection.connect("", autocommit=True)
    async with listener:
        await listener.execute(
            sql.SQL("LISTEN {}").format(
                sql.Identifier(postgres_channel(configuration.postgres_schema))
            )
        )
        await asyncio.to_thread(record_attempt, store, 100)
        messages = listener.notifies(timeout=2, stop_after=1)
        message = await anext(messages)
        await messages.aclose()
        assert message.payload == "recognition"

        def rollback() -> None:
            with store.connect(write=True) as db:
                db.execute("UPDATE recognition_metrics SET attempts=4")
                raise ValueError("rollback")

        with pytest.raises(ValueError):
            await asyncio.to_thread(rollback)
        messages = listener.notifies(timeout=0.2, stop_after=1)
        assert [message async for message in messages] == []
        with store.connect(write=True) as db:
            db.execute("UPDATE recognition_metrics SET attempts=5")
        messages = listener.notifies(timeout=2, stop_after=1)
        message = await anext(messages)
        await messages.aclose()
        assert message.payload == "all"
    live = AdminLive(AdminData(configuration))
    listener_task = asyncio.create_task(live._listen_postgres())
    try:
        await asyncio.wait_for(live.pending.wait(), timeout=3)
        live.pending.clear()
        live._recognition_pending = live._overview_pending = False
        await asyncio.to_thread(record_attempt, store, 101)
        await asyncio.wait_for(live.pending.wait(), timeout=3)
        assert live._recognition_pending
        assert not live._overview_pending
    finally:
        listener_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener_task


def test_shared_hash_publication_and_reference_removal(store) -> None:
    payload = document()
    pairs = [("0" * 20, 0), ("0" * 20, 1024), ("f" * 20, 30000)]
    for index in range(4):
        store.submit(f"shared-{index}", json.dumps(payload), 10)

    def publish_shared(index: int) -> None:
        store.finish(f"shared-{index}", f"reference-{index}", payload["metadata"], pairs)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(publish_shared, range(4)))
    with store.connect() as db:
        assert db.execute("SELECT count(*) AS count FROM landmarks").fetchone()["count"] == 12
    for index in range(4):
        assert store.get_submission(f"shared-{index}")["state"] == "processed"
    from fingerprint_server.database.postgres.postings import POSTINGS_VERSION, delete

    with store.connect(write=True) as db:
        reference = db.execute(
            "SELECT id FROM reference_tracks WHERE reference_key='reference-1'"
        ).fetchone()["id"]
        if store._format_version == POSTINGS_VERSION:
            assert delete(db, reference) == 3
        else:
            assert (
                db.execute("DELETE FROM landmarks WHERE reference_id=%s", (reference,)).rowcount
                == 3
            )
        db.execute("DELETE FROM reference_tracks WHERE id=%s", (reference,))
        assert db.execute("SELECT count(*) AS count FROM landmarks").fetchone()["count"] == 9


@pytest.mark.parametrize("layout", [1, 2])
def test_legacy_postgres_submission_codes_preserve_unknowns(
    configuration, postgres, monkeypatch, layout: int
) -> None:
    from fingerprint_server.admin.data import AdminData
    from fingerprint_server.database.postgres import PostgresStore
    from fingerprint_server.database.postgres.migrations import PostgresMigrationMixin

    store = PostgresStore(configuration)
    store.initialize()
    if layout == 2:
        import psycopg

        scripts = Path(__file__).resolve().parents[1] / "scripts"
        monkeypatch.syspath_prepend(str(scripts))
        migrate = runpy.run_path(str(scripts / "migrate_postgres_layout.py"))["migrate"]
        with psycopg.connect("") as db:
            migrate(db, configuration.postgres_schema, 2)
        store.initialize()
    payload = json.dumps(document())
    store.submit("known", payload, 10)
    store.submit("unknown", payload, 10)
    with store.connect(write=True) as db:
        db.execute("ALTER TABLE submissions ALTER COLUMN state TYPE text USING state::text")
        db.execute("ALTER TABLE submissions DROP COLUMN state_raw")
        db.execute(
            "UPDATE submissions SET state='pending_metadata',"
            "error='MusicBrainz identifier unavailable' WHERE id='known'"
        )
        db.execute("UPDATE submissions SET state='~0',error='~0' WHERE id='unknown'")
        db.execute("INSERT INTO musicbrainz_reviews(submission_id,candidates) VALUES('known','[]')")
        db.execute("DELETE FROM service_state WHERE key='submission_codes_version'")
        db.execute("UPDATE migration_info SET format_version=%s", (layout,))
    with store.owner("api"), pytest.raises(RuntimeError, match="Stop all PostgreSQL"):
        store.initialize()
    original = PostgresMigrationMixin._migrate_submission_codes

    def fail_after_migration(self, db) -> None:
        original(self, db)
        raise RuntimeError("simulated failure")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresMigrationMixin, "_migrate_submission_codes", fail_after_migration)
        with pytest.raises(RuntimeError, match="simulated failure"):
            store.initialize()
    with store.connect() as db:
        version = db.execute("SELECT format_version FROM migration_info").fetchone()
        assert version["format_version"] == layout
        row = db.execute(
            "SELECT state,pg_typeof(state)::text AS type FROM submissions WHERE id='known'"
        ).fetchone()
        assert (row["state"], row["type"]) == ("pending_metadata", "text")
    store.initialize()
    store.initialize()
    assert store.get_submission("known")["state"] == "pending_metadata"
    assert store.get_submission("known")["error"] == "MusicBrainz identifier unavailable"
    assert store.get_submission("unknown")["state"] == "~0"
    assert store.get_submission("unknown")["error"] == "~0"
    with store.connect() as db:
        rows = db.execute(
            "SELECT id,state,state_raw,pg_typeof(state)::text AS type FROM submissions ORDER BY id"
        ).fetchall()
        assert [(row["state"], row["state_raw"], row["type"]) for row in rows] == [
            (4, None, "smallint"),
            (5, "~0", "smallint"),
        ]
        reviews = db.execute("SELECT count(*) AS count FROM musicbrainz_reviews").fetchone()
        assert reviews["count"] == 1
        version = db.execute("SELECT format_version FROM migration_info").fetchone()
        assert version["format_version"] == layout + 2
    admin = AdminData(configuration)
    rows = admin.read("submissions", {"state": {"eq": "~0"}})
    assert isinstance(rows, list) and [row["id"] for row in rows] == ["unknown"]


def test_backend_configuration(tmp_path: Path) -> None:
    config = settings(tmp_path)
    assert isinstance(create_store(config), Store)
    for options in (
        {"service_role": "api"},
        {"postgres_dsn": "password=not-for-sqlite"},
        {"database_backend": "mysql"},
        {"postgres_schema": "unsafe;schema"},
    ):
        with pytest.raises(ValidationError):
            settings(tmp_path, **options)


def test_sqlite_does_not_require_optional_driver() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys

class NoPostgres(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "psycopg" or fullname.startswith("psycopg."):
            raise ModuleNotFoundError("Optional driver unavailable", name="psycopg")

sys.meta_path.insert(0, NoPostgres())
from fingerprint_server.app import create_app
from fingerprint_server.settings import Settings
from fingerprint_server.database import create_store
from fingerprint_server.database.sqlite import Store
settings = Settings(upload_key="test-upload-key-at-least-24-characters",
                    read_key="test-reading-key-at-least-24-characters")
assert isinstance(create_store(settings), Store)
create_app(settings)
try:
    create_store(settings.model_copy(update={"database_backend": "postgres"}))
except RuntimeError as err:
    assert "[postgres]" in str(err)
else:
    raise AssertionError("Missing PostgreSQL driver must fail explicitly")
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_standalone_worker_command(store, configuration) -> None:
    store.submit("cli", json.dumps(document()), 10)
    env = {key: value for key, value in os.environ.items() if not key.startswith("FP_")}
    env.update(
        FP_DATABASE_BACKEND="postgres",
        FP_SERVICE_ROLE="worker",
        FP_POSTGRES_SCHEMA=configuration.postgres_schema,
        FP_UPLOAD_KEY=UPLOAD,
        FP_READ_KEY=READ,
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "fingerprint_server.worker_runtime"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while store.get_submission("cli")["state"] != "processed":
            assert process.poll() is None, process.communicate()[0]
            assert time.monotonic() < deadline, "Standalone worker did not process its queue"
            time.sleep(0.05)
        assert store.worker_available()
    finally:
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise


def test_fifo_idempotence_and_atomic_admission(store) -> None:
    payload = json.dumps(document())
    with ThreadPoolExecutor(max_workers=8) as executor:

        def attempt(index: int) -> bool:
            try:
                store.submit(str(index), payload, 3)
            except QueueFullError:
                return False
            return True

        assert sum(executor.map(attempt, range(16))) == 3
    with store.connect(write=True) as db:
        ids = [row["id"] for row in db.execute("SELECT id FROM submissions ORDER BY queue_order")]
        db.execute("UPDATE submissions SET enqueued_at=1")
    assert store.submit(ids[0], payload, 1)["id"] == ids[0]
    assert [store.claim()["id"] for _ in range(3)] == ids
    assert store.claim() is None
    assert store.statistics()["submissions"]["processing"] == 3


def test_byte_budget_is_utf8_and_concurrent_idempotence(store) -> None:
    payload = json.dumps({"metadata": {"title": "\u00e4" * 100}}, ensure_ascii=False)
    with pytest.raises(QueueFullError):
        store.submit("too-big", payload, 10, len(payload))
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: store.submit("one", payload, 1), range(8)))
    assert store.statistics()["submissions"]["pending_metadata"] == 1


def test_publication_is_atomic_and_duplicates_preserve_reference(store) -> None:
    payload = document()
    store.submit("first", json.dumps(payload), 10)
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.finish("first", "r", payload["metadata"], [("0" * 20, 0)] * 2)
    assert store.statistics()["references"]["total"] == 0
    assert store.get_submission("first")["state"] == "queued"
    store.finish("first", "r", payload["metadata"], [("0" * 20, 0)])
    store.submit("second", json.dumps(payload), 10)
    store.finish("second", "r", {**payload["metadata"], "title": "different"}, [("1" * 20, 5)])
    assert store.reference_metadata("r") == payload["metadata"]
    assert store.match([("1" * 20, 0)]) == []
    assert store.get_submission("second")["reference_id"] == "r"


def test_recognition_ranking_and_statistics_equal_sqlite(store, tmp_path: Path) -> None:
    sqlite = Store(tmp_path / "comparison.db")
    sqlite.initialize()
    query = [(f"{index:020x}", index * 10) for index in range(60)]
    for target in (sqlite, store):
        for key, count, mbid, delta in (
            ("best", 45, MBID, 300),
            ("variant", 44, MBID, 100),
            ("competitor", 30, "a61ad03c-8df0-44bf-b999-25d9997d2a24", 50),
        ):
            payload = document()
            payload["metadata"]["recording_mbid"] = mbid
            target.submit(key, json.dumps(payload), 10)
            target.finish(
                key,
                key,
                payload["metadata"],
                [(digest, offset + delta) for digest, offset in query[:count]],
                verified=True,
            )
    assert store.match(query) == sqlite.match(query)
    assert [item["reference_id"] for item in store.match(query)] == ["best", "competitor"]
    assert store.statistics() == sqlite.statistics()
    assert store.match([]) == []


def test_offset_vote_ties_equal_sqlite(store, tmp_path: Path) -> None:
    sqlite = Store(tmp_path / "ties.db")
    sqlite.initialize()
    query = [("1" * 20, 7), ("0" * 20, 0)]
    for target in (sqlite, store):
        payload = document()
        target.submit("first", json.dumps(payload), 10)
        target.finish("first", "r", payload["metadata"], [("1" * 20, 30), ("0" * 20, 1)])
    assert store.match(query) == sqlite.match(query)
    assert store.match(query)[0]["offset_seconds"] > 0


@pytest.mark.parametrize("count", [10000, 10001])
def test_candidate_limit(store, count: int) -> None:
    store.submit("first", "{}", 10)
    store.finish(
        "first", "r", document()["metadata"], [("0" * 20, offset) for offset in range(count)]
    )
    if count == 10001:
        with pytest.raises(QueryTooBroadError):
            store.match([("0" * 20, 0)] * 100)
    else:
        assert store.match([("0" * 20, 0)] * 100)[0]["matched_landmarks"] == 100


def test_availability_matches_sqlite(store, tmp_path: Path) -> None:
    sqlite = Store(tmp_path / "availability.db")
    sqlite.initialize()
    item = {
        "format_id": "ma-dejavu-landmarks-v2",
        "recording_mbid": MBID,
        "isrc": "USAAA2600001",
        "title": "Synthetic recording",
        "artists": ["Test artist"],
        "duration": 2,
    }
    items = [
        item,
        {**item, "recording_mbid": None},
        {**item, "isrc": "USAAA2600002"},
        {**item, "recording_mbid": None, "isrc": None},
    ]
    for seconds in (1, 2):
        for target in (sqlite, store):
            publish(target, str(seconds), MBID, seconds)
        assert store.availability(items) == sqlite.availability(items)
    assert store.availability(items)[0]["status"] == "full"


@pytest.mark.parametrize(
    "dependency", ["none", "linked", "unlinked", "invalid", "published", "identifier"]
)
def test_housekeeping_preserves_dependencies(store, dependency: str) -> None:
    payload = document()
    key = acoustic_reference_id(decode_submission(json.dumps(payload)))
    store.submit("old", json.dumps(payload), 10)
    store.finish("old", key, payload["metadata"], [("0" * 20, 0)])
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE reference_tracks SET metadata=%s,publication_ready=0",
            (json.dumps({"duration": 12}),),
        )
        db.execute("UPDATE submissions SET state=%s", (SubmissionState.PENDING_METADATA,))
        if dependency == "published":
            db.execute("UPDATE reference_tracks SET publication_ready=1")
        if dependency == "identifier":
            db.execute(
                "UPDATE reference_tracks SET metadata=%s", (json.dumps({"isrc": "USAAA2600001"}),)
            )
    if dependency in ("linked", "unlinked", "invalid"):
        store.submit("other", json.dumps(payload), 10)
        with store.connect(write=True) as db:
            if dependency == "linked":
                db.execute("UPDATE submissions SET reference_id=%s WHERE id='other'", (key,))
            elif dependency == "invalid":
                db.execute("UPDATE submissions SET document='not-json' WHERE id='other'")
    assert store.delete_missing_identifier_submission("old") == (
        True,
        dependency == "none",
        1 if dependency == "none" else 0,
    )


def test_retries_cache_manual_pass_and_deadline(store) -> None:
    payload = json.dumps(document())
    store.submit("first", payload, 10)
    assert store.claim()["attempts"] == 1
    store.defer_musicbrainz("first", 0)
    assert store.claim()["attempts"] == 1
    store.defer_pending_recheck("first", "outage", 60)
    assert store.get_submission("first")["state"] == "pending_metadata"
    store.request_pending_recheck()
    assert store.take_pending_recheck() == ["first"]
    assert store.take_pending_recheck() is None
    assert store.claim(["first"])["pending_recheck"]
    store.defer_musicbrainz("first", 60, pending_recheck=True)
    store.complete_pending_check("first", "unresolved", False)
    with store.connect() as db:
        row = db.execute("SELECT pending_due_at,pending_rechecks FROM submissions").fetchone()
        assert row["pending_due_at"] > time.time()
        assert row["pending_rechecks"] == 0
    store.cache_enrichment("lookup", {"recordings": []})
    assert store.cached_enrichment("lookup") == {"recordings": []}
    store.cache_enrichment("lookup", {"recordings": [1]})
    assert store.cached_enrichment("lookup") == {"recordings": [1]}
    store.fail("first", "temporary", 60)
    assert store.claim() is None
    store.fail("first", "terminal", None)
    assert store.get_submission("first")["state"] == "failed"
    assert store.delete_submission("first")
    assert not store.delete_submission("first")


def test_worker_ownership_recovery_and_fencing(store, configuration) -> None:
    from fingerprint_server.database.postgres import PostgresStore

    other = PostgresStore(configuration)
    store.submit("first", json.dumps(document()), 10)
    with store.owner("worker"):
        assert store.worker_available()
        assert store.claim()["id"] == "first"
        other.initialize()
        assert other.get_submission("first")["state"] == "processing"
        with pytest.raises(RuntimeError, match="already active"), other.owner("worker"):
            pass
        token = store._worker_token
    assert not store.worker_available()
    with other.owner("worker"):
        assert other.get_submission("first")["state"] == "queued"
        store._worker_token = token
        with pytest.raises(RuntimeError, match="ownership was lost"):
            store.fail("first", "stale worker", None)
        store._worker_token = None
    with store.owner("api"):
        with pytest.raises(RuntimeError, match="already active"), other.owner("api"):
            pass


def test_imported_schema_runtime_preserves_data(
    source: Path, postgres: object, migration: dict, tmp_path: Path
) -> None:
    from fingerprint_server.database.postgres import PostgresStore

    schema = f"import_{uuid4().hex}"
    with sqlite3.connect(source) as sqlite:
        migration["migrate"](sqlite, postgres, schema)
    postgres.commit()
    store = PostgresStore(settings(tmp_path, database_backend="postgres", postgres_schema=schema))
    store.initialize()
    assert store.match([("0" * 20, 0)])[0]["reference_id"] == "reference"
    assert store.get_submission("second")["state"] == "processing"
    with store.owner("worker"):
        assert store.get_submission("second")["state"] == "pending_metadata"
        with store.connect() as db:
            row = db.execute(
                "SELECT pending_rechecks,pending_due_at FROM submissions WHERE id='second'"
            ).fetchone()
            assert row == {"pending_rechecks": 2, "pending_due_at": 42}


def test_unknown_version_is_not_modified(store) -> None:
    with store.connect(write=True) as db:
        db.execute("UPDATE migration_info SET format_version=999")
    with pytest.raises(ValueError, match="Unsupported"):
        store.initialize()
    with store.connect() as db:
        assert (
            db.execute("SELECT format_version FROM migration_info").fetchone()["format_version"]
            == 999
        )


async def test_real_api_ingestion_and_pcm_recognition(configuration) -> None:
    application = create_app(configuration)
    async with AsyncClient(application) as client:
        assert (await client.get("/health")).status_code == 200
        await submit(client, acoustic_document())
        result = await recognize(client, audio()[50 * HOP_SIZE : 50 * HOP_SIZE + 5 * SAMPLE_RATE])
        assert result.status_code == 200
        assert result.json()["status"] == "matched"
        statistics = await client.get("/api/v1/dejavu/statistics", headers={"X-API-Key": READ})
        assert statistics.json()["references"]["total"] == 1


async def test_separate_api_and_worker(configuration) -> None:
    application = create_app(configuration.model_copy(update={"service_role": "api"}))
    async with AsyncClient(application) as client:
        health = await client.get("/health")
        assert health.status_code == 503
        assert health.json() == {"status": "failed"}
        assert health.headers["Cache-Control"] == "no-store"
        body, identifier = canonical(document())
        headers = {
            "X-API-Key": UPLOAD,
            "Content-Type": "application/json",
            "Idempotency-Key": identifier,
        }
        assert (
            await client.post("/api/v1/dejavu/submissions", content=body, headers=headers)
        ).status_code == 503
        async with runtime(configuration.model_copy(update={"service_role": "worker"})):
            assert (await client.get("/health")).status_code == 200
            assert application[WORKER_TASK] is None
            response = await client.post(
                "/api/v1/dejavu/submissions", content=body, headers=headers
            )
            assert response.status_code == 202
            async with asyncio.timeout(5):
                while application[STORE].get_submission(identifier)["state"] != "processed":
                    await asyncio.sleep(0.05)
        assert (await client.get("/health")).status_code == 503


async def test_failed_startup_releases_api_ownership(store, configuration) -> None:
    with store.owner("worker"):
        with pytest.raises(RuntimeError, match="already active"):
            async with runtime(configuration):
                pytest.fail("A second worker must not start")
        with store.owner("api"):
            pass


async def test_lost_ownership_stops_worker_and_http(configuration) -> None:
    psycopg = pytest.importorskip("psycopg")
    application = create_app(configuration)
    with pytest.raises(psycopg.OperationalError):
        async with AsyncClient(application) as client:
            with application[STORE].connect(write=True) as db:
                db.execute(
                    "SELECT pg_terminate_backend(l.pid) FROM pg_locks l "
                    "JOIN pg_stat_activity a ON a.pid=l.pid WHERE l.locktype='advisory' "
                    "AND l.granted AND l.pid!=pg_backend_pid() AND a.application_name=%s",
                    (f"fingerprint:{configuration.postgres_schema}",),
                )
            monitor = application[OWNERSHIP_TASK]
            assert monitor is not None
            async with asyncio.timeout(8):
                while not monitor.done():
                    await asyncio.sleep(0.05)
            assert (await client.get("/health")).status_code == 503
            assert (
                await client.get("/api/v1/dejavu/statistics", headers={"X-API-Key": READ})
            ).status_code == 503
