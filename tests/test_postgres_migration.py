"""Verify offline migration without touching a running installation."""

import json
import os
import runpy
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from test_metadata_gate import document

from fingerprint_server.database.sqlite import Store


@pytest.fixture
def migration() -> dict:
    pytest.importorskip("psycopg")
    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_sqlite_to_postgres.py"
    return runpy.run_path(str(script))


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "source.db"
    store = Store(path)
    store.initialize()
    payload = document()
    store.submit("first", json.dumps(payload), 10)
    store.finish("first", "reference", payload["metadata"], [("0" * 20, 0)])
    store.submit("second", json.dumps(payload), 10)
    with store.connect() as db:
        db.execute(
            "UPDATE submissions SET state='processing',pending_check=1,background_kind=1,"
            "pending_rechecks=2,pending_due_at=42,enqueued_at=5 WHERE id='second'"
        )
        db.execute("UPDATE submissions SET enqueued_at=5 WHERE id='first'")
    return path


@pytest.fixture
def postgres(monkeypatch: pytest.MonkeyPatch) -> object:
    if os.environ.get("FP_TEST_POSTGRES") != "1":
        pytest.skip("Set FP_TEST_POSTGRES=1 to run disposable PostgreSQL integration tests")
    psycopg = pytest.importorskip("psycopg")
    path = Path(__file__).resolve().parents[1] / "scripts" / "postgres_benchmark.py"
    docker = runpy.run_path(str(path))["docker"]
    name = f"fingerprint-migration-test-{uuid.uuid4().hex[:12]}"
    password = secrets.token_urlsafe(32)
    docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-p",
        "127.0.0.1::5432",
        "-e",
        "POSTGRES_PASSWORD",
        "postgres:17",
        env=os.environ | {"POSTGRES_PASSWORD": password},
    )
    try:
        port = docker("port", name, "5432/tcp").rsplit(":", 1)[1]
        deadline = time.monotonic() + 60
        while True:
            try:
                conn = psycopg.connect(
                    host="127.0.0.1",
                    port=port,
                    user="postgres",
                    password=password,
                    dbname="postgres",
                    connect_timeout=3,
                )
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        with conn:
            monkeypatch.setenv("PGHOST", "127.0.0.1")
            monkeypatch.setenv("PGPORT", port)
            monkeypatch.setenv("PGUSER", "postgres")
            monkeypatch.setenv("PGPASSWORD", password)
            monkeypatch.setenv("PGDATABASE", "postgres")
            yield conn
    finally:
        docker("stop", name)


def test_snapshot_copies_committed_wal(source: Path, tmp_path: Path, migration: dict) -> None:
    snapshot = tmp_path / "snapshot.db"
    db = sqlite3.connect(source)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE submissions SET error='from wal' WHERE id='second'")
        db.commit()
        migration["create_snapshot"](source, snapshot)
        with sqlite3.connect(snapshot) as copy:
            assert (
                copy.execute("SELECT error FROM submissions WHERE id='second'").fetchone()[0]
                == "from wal"
            )
    finally:
        db.close()


def test_command_line(source: Path, postgres: object) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_sqlite_to_postgres.py"
    result = subprocess.run(
        [sys.executable, str(script), str(source), "--schema", "cli_test"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "Migration committed" in result.stderr
    with postgres.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM cli_test.landmarks")
        assert cur.fetchone()[0] == 1
    postgres.rollback()


def test_refuses_wrong_schema(source: Path, migration: dict) -> None:
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA user_version=7")
        with pytest.raises(ValueError, match="schema 8"):
            migration["validate_source"](db)


def test_round_trip_and_identity(source: Path, postgres: object, migration: dict) -> None:
    psycopg = pytest.importorskip("psycopg")
    with sqlite3.connect(source) as db:
        counts = migration["migrate"](db, postgres, "migration_test")
    assert counts["submissions"] == 2 and counts["landmarks"] == 1
    with postgres.cursor() as cur:
        cur.execute(
            "SELECT id,state,pending_check,pending_rechecks,pending_due_at "
            "FROM migration_test.submissions ORDER BY enqueued_at,queue_order"
        )
        rows = cur.fetchall()
        assert [row[0] for row in rows] == ["first", "second"]
        assert rows[1][1:] == ("processing", 1, 2, 42)
        cur.execute(
            "INSERT INTO migration_test.reference_tracks "
            "(reference_key,metadata,created_at) VALUES ('new','{}',0) RETURNING id"
        )
        assert cur.fetchone()[0] == 2
        cur.execute(
            "INSERT INTO migration_test.submissions(id,document,state,created_at) "
            "VALUES ('third','{}','queued',0) RETURNING queue_order"
        )
        assert cur.fetchone()[0] == 3
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with postgres.transaction():
                cur.execute("INSERT INTO migration_test.landmarks VALUES (%s,999,0)", (bytes(10),))
    postgres.rollback()


def test_failure_rolls_back_new_schema(
    source: Path, postgres: object, migration: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = migration["migrate"].__globals__["copy_landmarks"]

    def fail_after_copy(db: sqlite3.Connection, target: object) -> int:
        original(db, target)
        raise ValueError("simulated validation failure")

    monkeypatch.setitem(migration["migrate"].__globals__, "copy_landmarks", fail_after_copy)
    with sqlite3.connect(source) as db, pytest.raises(ValueError, match="simulated"):
        migration["migrate"](db, postgres, "rollback_test")
    with postgres.cursor() as cur:
        cur.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='rollback_test')")
        assert cur.fetchone()[0] is False
    postgres.rollback()


def test_existing_schema_is_not_changed(source: Path, postgres: object, migration: dict) -> None:
    psycopg = pytest.importorskip("psycopg")
    with postgres.cursor() as cur:
        cur.execute("CREATE SCHEMA already_exists")
        cur.execute("CREATE TABLE already_exists.keep (value integer)")
        cur.execute("INSERT INTO already_exists.keep VALUES (123)")
    postgres.commit()
    with sqlite3.connect(source) as db, pytest.raises(psycopg.errors.DuplicateSchema):
        migration["migrate"](db, postgres, "already_exists")
    with postgres.cursor() as cur:
        cur.execute("SELECT value FROM already_exists.keep")
        assert cur.fetchone()[0] == 123
    postgres.rollback()


def test_active_destination_transaction_is_rejected(
    source: Path, postgres: object, migration: dict
) -> None:
    with postgres.cursor() as cur:
        cur.execute("SELECT 1")
    with sqlite3.connect(source) as db, pytest.raises(ValueError, match="active transaction"):
        migration["migrate"](db, postgres, "nested_test")
    postgres.rollback()


def test_value_mismatch_rolls_back(
    source: Path, postgres: object, migration: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = migration["migrate"].__globals__["source_rows"]
    calls = 0

    def mismatched_rows(db: sqlite3.Connection, name: str) -> object:
        nonlocal calls
        if name == "reference_tracks":
            calls += 1
            if calls == 2:
                rows = list(original(db, name))
                changed = list(rows[0])
                changed[2] = "corrupted"
                return iter([tuple(changed)])
        return original(db, name)

    monkeypatch.setitem(migration["migrate"].__globals__, "source_rows", mismatched_rows)
    with sqlite3.connect(source) as db, pytest.raises(ValueError, match="Data mismatch"):
        migration["migrate"](db, postgres, "mismatch_test")
    with postgres.cursor() as cur:
        cur.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='mismatch_test')")
        assert cur.fetchone()[0] is False
    postgres.rollback()
