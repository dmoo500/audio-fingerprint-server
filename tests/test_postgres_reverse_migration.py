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

from fingerprint_server.artwork import ArtworkStore
from fingerprint_server.database.application_keys import ApplicationKeys, initialize
from fingerprint_server.database.migrations import DB_SCHEMA_VERSION
from fingerprint_server.database.sqlite import Store


@pytest.fixture
def reverse(monkeypatch: pytest.MonkeyPatch) -> dict:
    pytest.importorskip("psycopg")
    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_postgres_to_sqlite.py"
    monkeypatch.syspath_prepend(str(script.parent))
    return runpy.run_path(str(script))


def test_rejects_unknown_application_registry_version(source: Path, migration: dict) -> None:
    store = Store(source)
    with ApplicationKeys(store).connection(write=True) as db:
        initialize(db)
        db.execute("UPDATE application_key_schema SET version=999")
    with (
        sqlite3.connect(source) as db,
        pytest.raises(ValueError, match="Unsupported application key registry version"),
    ):
        migration["validate_source"](db)


def imported(source: Path, postgres, migration: dict) -> None:
    with sqlite3.connect(source) as db:
        migration["migrate"](db, postgres, "reverse_test")


@pytest.mark.parametrize("damage", ["missing", "unknown"])
def test_rejects_damaged_registry_before_export(
    source: Path, postgres, migration: dict, reverse: dict, tmp_path: Path, damage: str
) -> None:
    store = Store(source)
    with ApplicationKeys(store).connection(write=True) as db:
        initialize(db)
    imported(source, postgres, migration)
    with postgres.transaction():
        postgres.execute("SET LOCAL search_path TO reverse_test,pg_catalog")
        if damage == "missing":
            postgres.execute("DROP TABLE application_key_schema")
        else:
            postgres.execute("UPDATE application_key_schema SET version=999")
    output = tmp_path / "output.db"
    with pytest.raises(ValueError, match="application key registry"):
        reverse["export_sqlite"](postgres, "reverse_test", output)
    assert not output.exists()
    assert not list(tmp_path.glob(".*.partial"))


def test_roundtrip_preserves_all_fields_and_new_changes(
    source: Path, postgres, migration: dict, reverse: dict, tmp_path: Path
) -> None:
    store = Store(source)
    ArtworkStore(store).initialize()
    keys = ApplicationKeys(store)
    with keys.connection(write=True) as db:
        initialize(db)
    active_id, active_key = keys.issue("active")
    revoked_id, revoked_key = keys.issue("revoked")
    keys.revoke(revoked_id)
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO admin_hourly_metrics VALUES (1000,1,2,3,4,1,17)")
        db.execute("INSERT INTO recognition_metrics VALUES (1000,9,3,2,1,2)")
        db.execute("INSERT INTO recognition_identity_metrics VALUES (1000,'personal','app-id',9)")
        db.execute("INSERT INTO musicbrainz_reviews VALUES ('second','[]',NULL)")
        db.execute(
            "INSERT INTO artwork_requests VALUES "
            "('reference','ready','https://example.com',1,2,'ready_release')"
        )
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
        assert db.execute("SELECT * FROM admin_hourly_metrics").fetchall() == [
            (1000, 1, 2, 3, 4, 1, 17)
        ]
        assert db.execute("SELECT * FROM recognition_metrics").fetchall() == [(1000, 9, 3, 2, 1, 2)]
        assert db.execute("SELECT * FROM recognition_identity_metrics").fetchall() == [
            (1000, "personal", "app-id", 9)
        ]
        assert db.execute("SELECT * FROM musicbrainz_reviews").fetchall() == [
            ("second", "[]", None)
        ]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT * FROM artwork_requests").fetchall() == [
            ("reference", "ready", "https://example.com", 1, 2, "ready_release")
        ]
    # Export back to PostgreSQL proves all mapped fields, landmarks and FIFO survive both ways.
    with sqlite3.connect(output) as db:
        again = migration["migrate"](db, postgres, "second_import")
    assert again == result["counts"]
    store = Store(output)
    store.initialize()
    exported_keys = ApplicationKeys(store)
    assert exported_keys.authenticate(active_key) == "app-" + active_id
    assert exported_keys.authenticate(revoked_key) is None
    assert store.get_submission("first") is None
    # Existing SQLite startup pauses background rechecks when verification is disabled.
    assert store.get_submission("second")["state"] == "pending_metadata"
    with store.connect() as db:
        db.execute(
            "INSERT INTO submissions(id,document,state,created_at) VALUES ('next','{}','queued',2)"
        )
        assert db.execute("SELECT rowid FROM submissions WHERE id='next'").fetchone()[0] == 51


def test_legacy_artwork_cache_roundtrip_and_postgres_upgrade(
    source: Path, postgres, migration: dict, reverse: dict, tmp_path: Path
) -> None:
    from fingerprint_server.database.postgres import PostgresStore
    from fingerprint_server.settings import Settings

    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE artwork_requests(reference_key text PRIMARY KEY,state text NOT NULL,"
            "url text,due_at double precision NOT NULL,expires_at double precision NOT NULL)"
        )
        db.execute("INSERT INTO artwork_requests VALUES ('old','missing',NULL,1,9999999999)")
        db.execute("INSERT INTO service_state VALUES ('artwork_schema','1')")
    imported(source, postgres, migration)
    with postgres.transaction():
        postgres.execute("SET LOCAL search_path TO reverse_test,pg_catalog")
        assert postgres.execute("SELECT reason FROM artwork_requests").fetchone() == (None,)
        postgres.execute("ALTER TABLE artwork_requests DROP COLUMN reason")
    output = tmp_path / "legacy.db"
    reverse["export_sqlite"](postgres, "reverse_test", output)
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT * FROM artwork_requests").fetchone() == (
            "old",
            "missing",
            None,
            1,
            9999999999,
            None,
        )
    config = Settings(
        database_backend="postgres",
        postgres_dsn="",
        postgres_schema="reverse_test",
        upload_key="synthetic-upload-key-for-tests",
        read_key="synthetic-read-key-for-tests",
    )
    queue = ArtworkStore(PostgresStore(config))
    queue.initialize()
    queue.initialize()
    with queue.store.connect() as db:
        row = db.execute("SELECT reason,expires_at FROM artwork_requests").fetchone()
        assert row["reason"] == "legacy_retry"
        assert row["expires_at"] == 0
        assert (
            db.execute("SELECT value FROM service_state WHERE key='artwork_schema'").fetchone()[
                "value"
            ]
            == "3"
        )


def test_legacy_metrics_roundtrip(
    source: Path, postgres, migration: dict, reverse: dict, tmp_path: Path
) -> None:
    with sqlite3.connect(source) as db:
        db.execute("ALTER TABLE admin_hourly_metrics DROP COLUMN cached_artwork")
        db.execute("INSERT INTO admin_hourly_metrics VALUES (1000,1,2,3,4,5)")
    imported(source, postgres, migration)
    with postgres.transaction():
        postgres.execute("SET LOCAL search_path TO reverse_test,pg_catalog")
        assert postgres.execute("SELECT cached_artwork FROM admin_hourly_metrics").fetchone() == (
            None,
        )
        postgres.execute("ALTER TABLE admin_hourly_metrics DROP COLUMN cached_artwork")
    output = tmp_path / "legacy-metrics.db"
    reverse["export_sqlite"](postgres, "reverse_test", output)
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT * FROM admin_hourly_metrics").fetchone() == (
            1000,
            1,
            2,
            3,
            4,
            5,
            None,
        )


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


def test_export_accepts_previous_sqlite_provenance(
    source: Path, postgres, migration: dict, reverse: dict, tmp_path: Path
) -> None:
    imported(source, postgres, migration)
    with postgres.transaction():
        postgres.execute("UPDATE reverse_test.migration_info SET sqlite_schema_version=8")
    output = tmp_path / "previous-provenance.db"
    reverse["export_sqlite"](postgres, "reverse_test", output)
    with sqlite3.connect(output) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        assert db.execute("SELECT count(*) FROM submission_metadata").fetchone()[0] == 2


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
