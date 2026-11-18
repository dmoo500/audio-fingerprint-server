"""Verify lossless rollback into a new SQLite file, never an in-place merge."""

import json
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_postgres_migration import migration as migration
from test_postgres_migration import postgres as postgres
from test_postgres_migration import source as source

from fingerprint_server.database.migrations import DB_SCHEMA_VERSION
from fingerprint_server.database.sqlite import Store


@pytest.fixture
def reverse(monkeypatch: pytest.MonkeyPatch) -> dict:
    pytest.importorskip("psycopg")
    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_postgres_to_sqlite.py"
    monkeypatch.syspath_prepend(str(script.parent))
    return runpy.run_path(str(script))


def imported(source: Path, postgres, migration: dict) -> None:
    with sqlite3.connect(source) as db:
        migration["migrate"](db, postgres, "reverse_test")


def test_roundtrip_preserves_all_fields_and_new_changes(
    source: Path, postgres, migration: dict, reverse: dict, tmp_path: Path
) -> None:
    imported(source, postgres, migration)
    with postgres.transaction():
        postgres.execute("SET LOCAL search_path TO reverse_test,pg_catalog")
        postgres.execute("DELETE FROM submissions WHERE id='first'")
        postgres.execute(
            "INSERT INTO submissions(id,document,state,created_at,enqueued_at,queue_order) "
            "VALUES ('new','{}','queued',1,5,50)"
        )
        postgres.execute(
            "INSERT INTO reference_tracks(id,reference_key,metadata,created_at) "
            "VALUES (5000000000,'new-reference','{}',1)"
        )
        postgres.execute(
            'INSERT INTO landmarks(hash,reference_id,"offset") VALUES (%s,5000000000,123)',
            (b"\xff" * 10,),
        )
        postgres.execute("INSERT INTO service_state VALUES ('api_rate:read','[123]')")
        postgres.execute(
            "INSERT INTO enrichment_cache VALUES ('CHTEST123456',%s,42)", (json.dumps("ü"),)
        )
    output = tmp_path / "restored.db"
    result = reverse["export_sqlite"](postgres, "reverse_test", output)
    assert result["counts"]["landmarks"] == 2
    assert result["counts"]["submissions"] == 2
    assert output.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(output) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        assert db.execute(
            "SELECT id,rowid FROM submissions ORDER BY enqueued_at,rowid"
        ).fetchall() == [("second", 2), ("new", 50)]
        assert db.execute(
            "SELECT state,pending_check,background_kind,pending_due_at FROM submissions "
            "WHERE id='second'"
        ).fetchone() == ("processing", 1, 1, 42)
        assert db.execute("SELECT max(reference_id) FROM landmarks").fetchone()[0] == 5000000000
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    # Export back to PostgreSQL proves all mapped fields, landmarks and FIFO survive both ways.
    with sqlite3.connect(output) as db:
        again = migration["migrate"](db, postgres, "second_import")
    assert again == result["counts"]
    store = Store(output)
    store.initialize()
    assert store.get_submission("first") is None
    # Existing SQLite startup pauses background rechecks when verification is disabled.
    assert store.get_submission("second")["state"] == "pending_metadata"
    with store.connect() as db:
        db.execute(
            "INSERT INTO submissions(id,document,state,created_at) VALUES ('next','{}','queued',2)"
        )
        assert db.execute("SELECT rowid FROM submissions WHERE id='next'").fetchone()[0] == 51


@pytest.mark.parametrize(
    "role,shared", [("api", False), ("api", True), ("worker", False), ("write", False)]
)
def test_active_service_is_rejected(
    source: Path, postgres, migration: dict, reverse: dict, tmp_path: Path, role: str, shared: bool
) -> None:
    psycopg = pytest.importorskip("psycopg")
    imported(source, postgres, migration)
    with psycopg.connect("", autocommit=True) as owner:
        function = "pg_advisory_lock_shared" if shared else "pg_advisory_lock"
        owner.execute(
            f"SELECT {function}(hashtextextended(%s,0))",
            (f"fingerprint:reverse_test:{role}",),
        )
        with pytest.raises(RuntimeError, match="Stop all"):
            reverse["export_sqlite"](postgres, "reverse_test", tmp_path / "output.db")
    assert not list(tmp_path.glob(".*.partial"))
    assert not (tmp_path / "output.db").exists()
    reverse["export_sqlite"](postgres, "reverse_test", tmp_path / "output.db")


def test_failure_never_publishes_partial_file(
    source: Path,
    postgres,
    migration: dict,
    reverse: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported(source, postgres, migration)
    original = reverse["export_sqlite"].__globals__["copy_landmarks"]

    def fail(db, target):
        original(db, target)
        raise ValueError("simulated failure")

    monkeypatch.setitem(reverse["export_sqlite"].__globals__, "copy_landmarks", fail)
    with pytest.raises(ValueError, match="simulated failure"):
        reverse["export_sqlite"](postgres, "reverse_test", tmp_path / "failed.db")
    assert not (tmp_path / "failed.db").exists()
    assert not list(tmp_path.glob(".*.partial"))
    assert postgres.info.transaction_status.name == "IDLE"


def test_existing_destination_and_unknown_format_are_rejected(
    source: Path, postgres, migration: dict, reverse: dict, tmp_path: Path
) -> None:
    imported(source, postgres, migration)
    output = tmp_path / "keep.db"
    output.write_bytes(b"untouched")
    with pytest.raises(FileExistsError):
        reverse["export_sqlite"](postgres, "reverse_test", output)
    assert output.read_bytes() == b"untouched"
    with postgres.transaction():
        postgres.execute("UPDATE reverse_test.migration_info SET format_version=999")
    with pytest.raises(ValueError, match="Unsupported"):
        reverse["export_sqlite"](postgres, "reverse_test", tmp_path / "unsupported.db")
    assert not (tmp_path / "unsupported.db").exists()


def test_reverse_cli(source: Path, postgres, migration: dict, tmp_path: Path) -> None:
    imported(source, postgres, migration)
    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_postgres_to_sqlite.py"
    output = tmp_path / "cli.db"
    result = subprocess.run(
        [sys.executable, str(script), str(output), "--schema", "reverse_test"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "SQLite export verified and published" in result.stderr
    assert Store(output).get_submission("second")["state"] == "processing"
