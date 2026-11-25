"""SQLite integer submission states and lossless legacy upgrades."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from fingerprint_server.database.codes import SubmissionState
from fingerprint_server.database.review_schema import REVIEW_TABLE
from fingerprint_server.database.sqlite import Store
from fingerprint_server.database.sqlite.metadata import TABLE, TRIGGERS
from fingerprint_server.database.sqlite.migrations import SCHEMA, initialize_database


def legacy_v9(path: Path) -> None:
    """Create a real text-affinity schema with dependent rows and custom objects."""
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(
            SCHEMA.replace("state INTEGER NOT NULL, state_raw TEXT", "state TEXT NOT NULL")
        )
        db.execute(
            TABLE.replace("state INTEGER NOT NULL,\n    state_raw TEXT,", "state TEXT NOT NULL,")
        )
        for trigger in TRIGGERS:
            db.execute(
                trigger.replace("state,state_raw,error", "state,error")
                .replace("NEW.state,NEW.state_raw,NEW.error", "NEW.state,NEW.error")
                .replace(
                    "state=NEW.state,state_raw=NEW.state_raw,error=NEW.error",
                    "state=NEW.state,error=NEW.error",
                )
            )
        db.execute(REVIEW_TABLE)
        db.execute("CREATE INDEX custom_submission_error ON submissions(error)")
        db.execute("CREATE INDEX custom_projection_error ON submission_metadata(error)")
        db.execute(
            "CREATE TRIGGER custom_submission_update AFTER UPDATE OF attempts ON submissions "
            "BEGIN UPDATE submissions SET retry_at=NEW.retry_at WHERE id=NEW.id; END"
        )
        for rowid, state, error in (
            (42, "pending_metadata", "Incomplete metadata"),
            (99, "0", "~0"),
            (120, "~future", "TheAudioDB: dynamic detail"),
            (121, "", "TheAudioDB HTTP 503"),
            (122, "~0", None),
        ):
            db.execute(
                "INSERT INTO submissions(rowid,id,document,state,error,created_at) "
                "VALUES(?,?,?, ?,?,0)",
                (rowid, str(rowid), "{}", state, error),
            )
        db.execute("INSERT INTO musicbrainz_reviews VALUES ('42','[]',NULL)")
        db.execute("PRAGMA user_version=9")


def test_v9_migration_preserves_rows_reviews_indexes_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    legacy_v9(path)
    store = Store(path)
    store.initialize()
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT rowid,id,state,typeof(state),state_raw,error "
                "FROM submissions ORDER BY rowid"
            )
        ] == [
            (42, "42", SubmissionState.PENDING_METADATA, "integer", None, "~0"),
            (99, "99", SubmissionState.UNKNOWN, "integer", "0", "~~0"),
            (
                120,
                "120",
                SubmissionState.UNKNOWN,
                "integer",
                "~future",
                "TheAudioDB: dynamic detail",
            ),
            (121, "121", SubmissionState.UNKNOWN, "integer", "", "~18"),
            (122, "122", SubmissionState.UNKNOWN, "integer", "~0", None),
        ]
        assert db.execute("SELECT candidates FROM musicbrainz_reviews").fetchone()[0] == "[]"
        assert db.execute("SELECT state FROM submission_metadata WHERE id='42'").fetchone()[0] == 4
        assert {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'custom_%'")
        } == {"custom_submission_error", "custom_projection_error", "custom_submission_update"}
        db.execute("UPDATE submissions SET attempts=3 WHERE id='42'")
        assert (
            db.execute("SELECT attempts FROM submission_metadata WHERE id='42'").fetchone()[0] == 3
        )
    assert store.get_submission("42")["error"] == "Incomplete metadata"
    assert store.get_submission("99")["state"] == "0"
    assert store.get_submission("99")["error"] == "~0"
    assert store.get_submission("120")["state"] == "~future"
    assert store.get_submission("120")["error"] == "TheAudioDB: dynamic detail"
    assert store.get_submission("121")["state"] == ""
    assert store.get_submission("121")["error"] == "TheAudioDB HTTP 503"
    assert store.get_submission("122")["state"] == "~0"
    backups = list(tmp_path.glob("*.pre-v9-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert db.execute("SELECT state,error FROM submissions WHERE id='99'").fetchone() == (
            "0",
            "~0",
        )
    store.initialize()
    assert list(tmp_path.glob("*.bak")) == backups


@pytest.mark.parametrize("error", (sqlite3.OperationalError, TypeError))
def test_v9_migration_failure_rolls_back(tmp_path: Path, error: type[Exception]) -> None:
    from fingerprint_server.database.sqlite.migrations import _migrate_submission_codes

    path = tmp_path / "legacy.db"
    legacy_v9(path)
    store = Store(path)

    def fail_after_rebuild(db: sqlite3.Connection) -> None:
        _migrate_submission_codes(db)
        raise error("simulated failure")

    with (
        patch(
            "fingerprint_server.database.sqlite.migrations._migrate_submission_codes",
            fail_after_rebuild,
        ),
        pytest.raises(error, match="simulated failure"),
    ):
        store.initialize()
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert db.execute("SELECT state,error FROM submissions WHERE id='99'").fetchone()[:] == (
            "0",
            "~0",
        )
        assert db.execute("SELECT count(*) FROM musicbrainz_reviews").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("cache_size", [-2000, -131072, 512, 32768])
@pytest.mark.parametrize("orphan", [False, True])
def test_v9_upgrade_preserves_cache_and_checks_foreign_keys(
    tmp_path: Path, cache_size: int, orphan: bool
) -> None:
    path = tmp_path / "legacy.db"
    legacy_v9(path)
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA cache_size={cache_size}")
        if orphan:
            db.execute("INSERT INTO landmarks VALUES(?,999,0)", (bytes(10),))
            db.commit()
        db.execute("PRAGMA foreign_keys=ON")
        observed: list[int] = []

        def trace(statement: str) -> None:
            if statement == "PRAGMA foreign_key_check":
                observed.append(db.execute("PRAGMA cache_size").fetchone()[0])

        db.set_trace_callback(trace)
        if orphan:
            with pytest.raises(RuntimeError, match="orphaned"):
                initialize_database(db, path, create_backup=False)
        else:
            initialize_database(db, path, create_backup=False)
        assert len(observed) == 1
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        original_bytes = -cache_size * 1024 if cache_size < 0 else cache_size * page_size
        actual = observed[0]
        actual_bytes = -actual * 1024 if actual < 0 else actual * page_size
        assert actual_bytes >= max(original_bytes, 64 * 1024 * 1024)
        assert db.execute("PRAGMA cache_size").fetchone()[0] == cache_size
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA user_version").fetchone()[0] == (9 if orphan else 10)
