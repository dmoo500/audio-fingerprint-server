"""Versioned upgrades preserve stored content and the external recognition contract."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from test_metadata_gate import LEGACY_SCHEMA, legacy_store, metadata

from fingerprint_server.database.logic import MAX_MATCH_CANDIDATES
from fingerprint_server.database.migrations import DB_SCHEMA_VERSION, _migrate_binary_landmarks
from fingerprint_server.database.sqlite import MATCH_QUERY, QueryTooBroadError, Store


def snapshot(path: Path) -> list[tuple[str, list[tuple[object, ...]]]]:
    """Capture every user table and its rows without modifying the database."""
    with sqlite3.connect(path) as db:
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        return [(name, db.execute(f'SELECT * FROM "{name}"').fetchall()) for (name,) in tables]


def test_fresh_database_is_current_and_compact(tmp_path: Path) -> None:
    store = Store(tmp_path / "fresh.db")
    store.initialize()
    store.initialize()
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='landmarks'").fetchone()[0]
        assert "WITHOUT ROWID" in sql
    assert list(tmp_path.glob("*.bak")) == []


def test_schema_seven_upgrade_creates_reference_index_and_backup(tmp_path: Path) -> None:
    store = Store(tmp_path / "legacy.db")
    store.initialize()
    with store.connect() as db:
        db.execute("DROP INDEX landmarks_reference")
        db.execute("PRAGMA user_version=7")
    before = snapshot(store.path)
    store.initialize()
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        plan = db.execute(
            "EXPLAIN QUERY PLAN DELETE FROM landmarks WHERE reference_id=?", (1,)
        ).fetchone()
        assert "landmarks_reference" in plan[3]
    assert snapshot(store.path) == before
    backups = list(tmp_path.glob("*.pre-v7-*.bak"))
    assert len(backups) == 1
    assert snapshot(backups[0]) == before
    store.initialize()
    assert list(tmp_path.glob("*.bak")) == backups


def test_schema_seven_index_failure_rolls_back(tmp_path: Path) -> None:
    store = Store(tmp_path / "legacy.db")
    store.initialize()
    with store.connect() as db:
        db.execute("DROP INDEX landmarks_reference")
        db.execute("CREATE VIEW landmarks_reference AS SELECT 1")
        db.execute("PRAGMA user_version=7")
    before = snapshot(store.path)
    with pytest.raises(sqlite3.OperationalError, match="landmarks_reference"):
        store.initialize()
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
    assert snapshot(store.path) == before
    backup = next(tmp_path.glob("*.pre-v7-*.bak"))
    assert snapshot(backup) == before


@pytest.mark.parametrize(("version", "existing_gate"), [(0, False), (0, True), (1, True)])
def test_legacy_upgrade_preserves_content_and_external_keys(
    tmp_path: Path, version: int, existing_gate: bool
) -> None:
    payload = '{"original":"document"}'
    store = legacy_store(tmp_path / "legacy.db", json.dumps(metadata()), payload)
    with store.connect() as db:
        if existing_gate:
            db.execute(
                "ALTER TABLE reference_tracks ADD COLUMN publication_ready "
                "INTEGER NOT NULL DEFAULT 0"
            )
        db.execute(f"PRAGMA user_version={version}")
        if version == 1:
            db.execute("UPDATE reference_tracks SET publication_ready=1")
        db.execute(
            "INSERT INTO submissions(id,document,state,attempts,retry_at,error,created_at) "
            "VALUES('claimed','{}','processing',2,42,'retry',5)"
        )
        db.execute("INSERT INTO enrichment_cache VALUES('ISRC','{}',123)")
    before = snapshot(store.path)
    store.initialize()
    assert store.match([("0" * 20, 0)])[0]["reference_id"] == "legacy"
    original = store.get_submission("old")
    claimed = store.get_submission("claimed")
    assert original is not None and original["reference_id"] == "legacy"
    assert claimed is not None and claimed["state"] == "queued"
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        assert tuple(
            db.execute("SELECT reference_key,metadata,created_at FROM reference_tracks").fetchone()
        ) == ("legacy", json.dumps(metadata()), 0)
        assert tuple(
            db.execute(
                "SELECT typeof(hash),length(hash),hex(hash),typeof(reference_id),offset "
                "FROM landmarks"
            ).fetchone()
        ) == ("blob", 10, "0" * 20, "integer", 0)
        assert (
            db.execute("SELECT document FROM submissions WHERE id='old'").fetchone()[0] == payload
        )
        assert tuple(
            db.execute(
                "SELECT attempts,retry_at,error,created_at FROM submissions WHERE id='claimed'"
            ).fetchone()
        ) == (2, 42, "retry", 5)
        assert tuple(db.execute("SELECT * FROM enrichment_cache").fetchone()) == ("ISRC", "{}", 123)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1
    assert snapshot(backups[0]) == before
    after = snapshot(store.path)
    store.initialize()
    assert snapshot(store.path) == after
    assert list(tmp_path.glob("*.bak")) == backups


@pytest.mark.parametrize("failure", ["invalid_hash", "orphan", "interrupted"])
def test_failed_migration_rolls_back_without_reset(tmp_path: Path, failure: str) -> None:
    store = legacy_store(tmp_path / "legacy.db", json.dumps(metadata()))
    with store.connect() as db:
        if failure == "invalid_hash":
            db.execute("UPDATE landmarks SET hash='not a valid hash'")
        if failure == "orphan":
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("UPDATE landmarks SET reference_id='missing'")
    before = snapshot(store.path)
    if failure == "interrupted":

        def fail_after_rebuild(db: sqlite3.Connection) -> None:
            _migrate_binary_landmarks(db)
            raise RuntimeError("interrupted")

        with (
            patch(
                "fingerprint_server.database.migrations._migrate_binary_landmarks",
                side_effect=fail_after_rebuild,
            ),
            pytest.raises(RuntimeError, match="interrupted"),
        ):
            store.initialize()
    else:
        with pytest.raises((sqlite3.Error, RuntimeError)):
            store.initialize()
    assert snapshot(store.path) == before
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert snapshot(next(tmp_path.glob("*.bak"))) == before


@pytest.mark.parametrize("version", [-1, DB_SCHEMA_VERSION + 1])
def test_unsupported_version_is_rejected_without_writes(tmp_path: Path, version: int) -> None:
    store = legacy_store(tmp_path / "legacy.db", json.dumps(metadata()))
    with store.connect() as db:
        db.execute(f"PRAGMA user_version={version}")
    before = store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Unsupported fingerprint DB schema"):
        store.initialize()
    assert store.path.read_bytes() == before
    assert list(tmp_path.glob("*.bak")) == []


def test_unknown_unversioned_schema_is_not_stamped_current(tmp_path: Path) -> None:
    path = tmp_path / "unknown.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(RuntimeError, match="Unrecognized fingerprint database"):
        Store(path).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == [("unrelated",)]


def test_binary_match_plan_and_reference_order_are_preserved(tmp_path: Path) -> None:
    store = Store(tmp_path / "matches.db")
    store.initialize()
    hashes = [(f"{index:020x}", index) for index in range(50)]
    for key in ("z-reference", "a-reference"):
        store.submit(key, "{}", 10)
        store.finish(key, key, {**metadata(), "title": key}, hashes)
    result = store.match(hashes)
    assert [row["reference_id"] for row in result] == ["a-reference", "z-reference"]
    assert all(row["matched_landmarks"] == 50 for row in result)
    assert all(row["offset_seconds"] == 0 for row in result)
    with store.connect() as db:
        db.execute("CREATE TEMP TABLE query(hash BLOB,offset INTEGER,idx INTEGER)")
        db.executemany(
            "INSERT INTO query VALUES(?,?,?)",
            (
                (bytes.fromhex(digest), offset, index)
                for index, (digest, offset) in enumerate(hashes)
            ),
        )
        plan = [row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + MATCH_QUERY)]
        assert any("SEARCH l USING PRIMARY KEY (hash=?)" in step for step in plan)
        assert not any("SCAN l" in step for step in plan)


def test_versioned_legacy_missing_gate_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA)
        db.execute("PRAGMA user_version=1")
    with pytest.raises(RuntimeError, match="Unrecognized fingerprint database columns"):
        Store(path).initialize()


def test_binary_lookup_retains_candidate_limit(tmp_path: Path) -> None:
    store = Store(tmp_path / "broad.db")
    store.initialize()
    store.submit("broad", "{}", 10)
    store.finish(
        "broad",
        "broad",
        metadata(),
        [("0" * 20, offset) for offset in range(MAX_MATCH_CANDIDATES + 1)],
    )
    with pytest.raises(QueryTooBroadError):
        store.match([("0" * 20, 0)])


def test_failed_migration_can_be_retried_from_original_data(tmp_path: Path) -> None:
    store = legacy_store(tmp_path / "retry.db", json.dumps(metadata()))
    with (
        patch(
            "fingerprint_server.database.migrations._migrate_binary_landmarks",
            side_effect=RuntimeError("interrupted"),
        ),
        pytest.raises(RuntimeError, match="interrupted"),
    ):
        store.initialize()
    store.initialize()
    assert store.match([("0" * 20, 0)])[0]["reference_id"] == "legacy"
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION


def test_upgrade_backup_includes_committed_wal_data(tmp_path: Path) -> None:
    path = tmp_path / "wal.db"
    source = sqlite3.connect(path)
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.executescript(LEGACY_SCHEMA)
        source.execute(
            "INSERT INTO reference_tracks VALUES('from-wal',?,0)", (json.dumps(metadata()),)
        )
        source.execute("INSERT INTO landmarks VALUES(?,'from-wal',0)", ("0" * 20,))
        source.commit()
        assert path.with_name(f"{path.name}-wal").stat().st_size > 0
        before = snapshot(path)
        Store(path).initialize()
        assert snapshot(next(tmp_path.glob("*.bak"))) == before
        assert Store(path).match([("0" * 20, 0)])[0]["reference_id"] == "from-wal"
    finally:
        source.close()


def test_wrong_column_types_are_not_guessed_during_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "wrong-types.db"
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA.replace("hash TEXT", "hash INTEGER"))
    with pytest.raises(RuntimeError, match="Unrecognized fingerprint database column types"):
        Store(path).initialize()
    assert list(tmp_path.glob("*.bak")) == []
