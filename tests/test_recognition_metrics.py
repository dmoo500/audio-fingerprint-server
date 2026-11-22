"""Verify durable anonymous counters for recognition requests."""

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from aiohttp_client import AsyncClient
from test_landmark_recognition import HEADERS, PATH, query
from test_postgres_migration import postgres as postgres
from test_server import audio, recognize, settings

from fingerprint_server.api_state import STORE
from fingerprint_server.app import create_app
from fingerprint_server.database.recognition_metrics import (
    recognition_history,
    record_attempt,
    record_outcome,
)
from fingerprint_server.database.sqlite import QueryTooBroadError, Store


async def test_core_counts_both_routes_and_errors(tmp_path: Path, monkeypatch) -> None:
    application = create_app(settings(tmp_path))
    async with AsyncClient(application) as client:
        empty = query() | {"fingerprints": []}
        assert (await client.post(PATH, headers=HEADERS, json=empty)).json()["status"] == "no_match"
        assert (await recognize(client, audio(3) * 0)).json()["status"] == "no_match"
        assert (await client.post(PATH, headers=HEADERS, content=b"{")).status_code == 422
        assert (await client.post(PATH, headers={"X-API-Key": "wrong"})).status_code == 401
        assert (await client.post("/api/v1/dejavu/recognize")).status_code == 422
        candidate = {
            "matched_landmarks": 100,
            "match_fraction": 0.9,
            "matched_span_seconds": 3,
            "offset_seconds": 1,
        }
        monkeypatch.setattr(application[STORE], "match", lambda pairs: [candidate])
        assert (await client.post(PATH, headers=HEADERS, json=query())).json()[
            "status"
        ] == "matched"
        monkeypatch.setattr(application[STORE], "match", lambda pairs: [candidate, candidate])
        result = await client.post(PATH, headers=HEADERS, json=query())
        assert result.json()["status"] == "ambiguous"

        def too_broad(pairs):
            raise QueryTooBroadError

        monkeypatch.setattr(application[STORE], "match", too_broad)
        assert (await client.post(PATH, headers=HEADERS, json=query())).status_code == 503
        history = recognition_history(application[STORE], "all")
        bucket = history["buckets"][0]
        assert {
            k: bucket[k] for k in ("attempts", "matched", "no_match", "ambiguous", "failed")
        } == {
            "attempts": 8,
            "matched": 1,
            "no_match": 2,
            "ambiguous": 1,
            "failed": 4,
        }
    restarted = Store(tmp_path / "test.db")
    restarted.initialize()
    assert recognition_history(restarted, "all") == history


@pytest.mark.parametrize("operation", ["record_attempt", "record_outcome"])
async def test_persistence_failure_does_not_break_recognition(
    tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture, operation: str
) -> None:
    application = create_app(settings(tmp_path))

    def fail(*args):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(f"fingerprint_server.app.{operation}", fail)
    with caplog.at_level(logging.ERROR):
        async with AsyncClient(application) as client:
            response = await client.post(PATH, headers=HEADERS, json=query() | {"fingerprints": []})
            assert response.json()["status"] == "no_match"
    assert "statistics are incomplete" in caplog.text


def test_concurrent_updates_and_window_boundaries(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    now = int(time.time()) // 60
    monkeypatch.setattr(
        "fingerprint_server.database.recognition_metrics.time.time", lambda: now * 60
    )

    def count(_: int) -> None:
        record_attempt(store, now)
        record_outcome(store, now, "matched")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(count, range(40)))
    record_attempt(store, now - 60)
    record_outcome(store, now - 60, "failed")
    record_attempt(store, now - 59)
    hourly = recognition_history(store, "hour")
    assert sum(row["attempts"] for row in hourly["buckets"]) == 41
    assert sum(row["matched"] for row in hourly["buckets"]) == 40
    assert sum(row["failed"] for row in hourly["buckets"]) == 0
    assert sum(row["attempts"] for row in recognition_history(store, "all")["buckets"]) == 42
    assert sum(row["failed"] for row in recognition_history(store, "all")["buckets"]) == 1
    with pytest.raises(ValueError):
        recognition_history(store, "invalid")


def test_old_database_has_explicit_unavailable_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    with store.connect() as db:
        db.execute("DROP TABLE recognition_metrics")
    assert recognition_history(store, "all") == {"available": False, "buckets": []}
    store.initialize()
    assert recognition_history(store, "all")["available"]


def test_wal_readers_continue_while_a_writer_waits(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    started = threading.Event()

    def writer() -> None:
        started.set()
        record_attempt(store, 123)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO recognition_metrics(minute,attempts) VALUES (456,1)")
            pending = executor.submit(writer)
            assert started.wait(2)
            assert recognition_history(store, "all")["buckets"] == []
            assert not pending.done()
        pending.result(timeout=5)
    assert sum(row["attempts"] for row in recognition_history(store, "all")["buckets"]) == 2


def test_postgres_counters_and_json_values(postgres, tmp_path: Path) -> None:
    from fingerprint_server.database.postgres import PostgresStore

    store = PostgresStore(
        settings(tmp_path, database_backend="postgres", postgres_schema="metrics")
    )
    store.initialize()
    now = int(time.time()) // 60
    record_attempt(store, now)
    record_outcome(store, now, "matched")
    bucket = recognition_history(store, "day")["buckets"][0]
    assert bucket["attempts"] == bucket["matched"] == 1
    assert all(type(value) is int for value in bucket.values())
