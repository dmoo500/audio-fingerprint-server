"""Verify the SQLite admin projection survives upgrades and all document mutations."""

import json
import sqlite3
from pathlib import Path

import pytest

from fingerprint_server.database.migrations import initialize_database
from fingerprint_server.database.sqlite import Store


def test_upgrade_projection_tracks_inserts_updates_and_deletes(tmp_path: Path) -> None:
    store = Store(tmp_path / "old.db")
    store.initialize()
    with store.connect() as db:
        for trigger in (
            "submission_metadata_insert",
            "submission_metadata_update",
            "submission_metadata_fields",
            "submission_metadata_delete",
        ):
            db.execute(f"DROP TRIGGER {trigger}")
        db.execute("DROP TABLE submission_metadata")
        db.execute("PRAGMA user_version=8")
        db.executemany(
            "INSERT INTO submissions(id,document,state,created_at) VALUES(?,?,'failed',0)",
            [
                ("valid", json.dumps({"metadata": {"title": "Original"}})),
                ("missing", "{}"),
                ("broken", "not-json"),
            ],
        )
    store.initialize()
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        rows = dict(db.execute("SELECT id,metadata FROM submission_metadata"))
        assert json.loads(rows["valid"]) == {"title": "Original"}
        assert rows["missing"] is None and rows["broken"] is None
        db.execute(
            "UPDATE submissions SET document=? WHERE id='valid'",
            (json.dumps({"metadata": {"title": "Changed"}}),),
        )
        db.execute("UPDATE submissions SET state='queued',attempts=4 WHERE id='valid'")
        db.execute(
            "INSERT INTO submissions(id,document,state,created_at) VALUES('new',?,'failed',0)",
            (json.dumps({"metadata": {"title": "New"}}),),
        )
        db.execute("DELETE FROM submissions WHERE id='broken'")
    store.initialize()
    with store.connect() as db:
        rows = dict(db.execute("SELECT id,metadata FROM submission_metadata"))
        assert json.loads(rows["valid"]) == {"title": "Changed"}
        assert tuple(
            db.execute("SELECT state,attempts FROM submission_metadata WHERE id='valid'").fetchone()
        ) == ("queued", 4)
        assert json.loads(rows["new"]) == {"title": "New"}
        assert "broken" not in rows
    backups = list(tmp_path.glob("*.pre-v8-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 8
        assert backup.execute("SELECT count(*) FROM submissions").fetchone()[0] == 3


def test_missing_projection_trigger_is_not_silently_accepted(tmp_path: Path) -> None:
    store = Store(tmp_path / "damaged.db")
    store.initialize()
    with store.connect() as db:
        db.execute("DROP TRIGGER submission_metadata_update")
    with pytest.raises(RuntimeError, match="projection triggers"):
        store.initialize()


def test_explicit_upgrade_without_backup(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = Store(tmp_path / "old.db")
    store.initialize()
    with store.connect() as db:
        for trigger in (
            "submission_metadata_insert",
            "submission_metadata_update",
            "submission_metadata_fields",
            "submission_metadata_delete",
        ):
            db.execute(f"DROP TRIGGER {trigger}")
        db.execute("DROP TABLE submission_metadata")
        db.execute("PRAGMA user_version=8")
    with store.owner(), store.connect() as db:
        initialize_database(db, store.path, create_backup=False)
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
    assert not list(tmp_path.glob("*.bak"))
    assert "without a backup" in caplog.text


def test_combined_update_and_rollback_keep_projection_in_sync(tmp_path: Path) -> None:
    store = Store(tmp_path / "rollback.db")
    store.initialize()
    with store.connect() as db:
        db.execute(
            "INSERT INTO submissions(id,document,state,created_at) VALUES('item',?,'queued',0)",
            (json.dumps({"metadata": {"title": "Original"}}),),
        )
    with store.connect() as db:
        db.execute(
            "UPDATE submissions SET document=?,state='failed',attempts=2 WHERE id='item'",
            (json.dumps({"metadata": {"title": "Changed"}}),),
        )
        row = db.execute(
            "SELECT metadata,state,attempts FROM submission_metadata WHERE id='item'"
        ).fetchone()
        assert json.loads(row["metadata"]) == {"title": "Changed"}
        assert (row["state"], row["attempts"]) == ("failed", 2)
        db.rollback()
    with store.connect() as db:
        row = db.execute(
            "SELECT metadata,state,attempts FROM submission_metadata WHERE id='item'"
        ).fetchone()
        assert json.loads(row["metadata"]) == {"title": "Original"}
        assert (row["state"], row["attempts"]) == ("queued", 0)
