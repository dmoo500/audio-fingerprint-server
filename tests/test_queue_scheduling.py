"""Shared FIFO admission for background rechecks and new contributions."""

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_metadata_gate import document
from test_musicbrainz_validation import online_settings

import fingerprint_server.store as storage
from fingerprint_server.migrations import DB_SCHEMA_VERSION
from fingerprint_server.store import Store


def fill_pending(store: Store, count: int = 6) -> None:
    for index in range(count):
        identifier = f"pending-{index}"
        store.submit(identifier, json.dumps(document()), 100)
        store.pending_metadata(identifier, "not found")
    with store.connect() as db:
        db.execute("UPDATE submissions SET pending_due_at=0 WHERE background_kind=0")


def background_count(store: Store) -> int:
    with store.connect() as db:
        return db.execute(
            "SELECT count(*) FROM submissions WHERE state IN ('queued','processing') "
            "AND background_kind!=0"
        ).fetchone()[0]


@pytest.mark.parametrize("window", [1, 2, 4])
def test_shared_fifo_limits_backlog_and_retains_upload_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: int
) -> None:
    now = [1_800_000_000.0]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: now[0]))
    store = Store(tmp_path / "test.db", True, background_recheck_queue_window=window)
    store.initialize()
    fill_pending(store, 12)
    now[0] += 10
    store.submit("new-first", "{}", 100)
    assert store.claim()["id"] == "new-first"
    assert background_count(store) == window
    store.fail("new-first", "done", None)
    now[0] += 10
    store.submit("new-second", "{}", 100)
    for _ in range(window):
        row = store.claim()
        assert row["pending_recheck"]
        assert background_count(store) == window
        store.complete_pending_check(row["id"], "still unresolved", True)
        now[0] += 1
    assert store.claim()["id"] == "new-second"


def test_restart_lower_window_keeps_admitted_fifo_and_recheck_identity(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db", True, background_recheck_queue_window=4)
    store.initialize()
    fill_pending(store)
    first = store.claim()
    assert first["pending_recheck"]
    with store.connect() as db:
        before = db.execute(
            "SELECT id,enqueued_at FROM submissions WHERE background_kind=1 ORDER BY enqueued_at"
        ).fetchall()
    restarted = Store(store.path, True, background_recheck_queue_window=1)
    restarted.initialize()
    assert background_count(restarted) == 4
    with restarted.connect() as db:
        after = db.execute(
            "SELECT id,enqueued_at FROM submissions WHERE background_kind=1 ORDER BY enqueued_at"
        ).fetchall()
    assert [tuple(row) for row in before] == [tuple(row) for row in after]
    claimed = restarted.claim()
    assert claimed["id"] == first["id"]
    assert claimed["pending_recheck"] and claimed["pending_rechecks"] == 0
    assert background_count(restarted) == 4


def test_reverification_and_pending_share_window_without_spending_recheck(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    payload = document()
    for index in range(5):
        identifier = f"verified-{index}"
        store.submit(identifier, json.dumps(payload), 100)
        store.finish(identifier, f"reference-{index}", payload["metadata"], [])
    online = Store(store.path, True, background_recheck_queue_window=2)
    online.initialize()
    assert background_count(online) == 0
    assert online.statistics()["submissions"]["pending_metadata"] == 5
    fill_pending(online, 1)
    row = online.claim()
    assert row["pending_recheck"]
    assert background_count(online) == 2
    online.complete_pending_check(row["id"], "not found", True)
    row = online.claim()
    assert row["background_kind"] == 2 and not row["pending_recheck"]
    online.defer_musicbrainz(row["id"], 60)
    assert background_count(online) == 2
    online.initialize()
    assert background_count(online) == 2


def test_disabled_housekeeping_parks_admitted_pending_only(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    fill_pending(store)
    store.claim()
    disabled = Store(store.path, True, housekeeping_enabled=False)
    disabled.initialize()
    assert background_count(disabled) == 0
    assert disabled.claim() is None


def test_reverification_remains_required_with_housekeeping_disabled(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    payload = document()
    store.submit("legacy", json.dumps(payload), 100)
    store.finish("legacy", "reference", payload["metadata"], [])
    online = Store(store.path, True, housekeeping_enabled=False)
    online.initialize()
    row = online.claim()
    assert row["id"] == "legacy"
    assert row["background_kind"] == 2 and not row["pending_recheck"]
    assert row["pending_rechecks"] == 0


def test_503_releases_pending_slot_without_spending_check(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db", True, background_recheck_queue_window=1)
    store.initialize()
    fill_pending(store)
    row = store.claim()
    assert background_count(store) == 1
    store.defer_musicbrainz(row["id"], 60, True)
    assert background_count(store) == 0
    next_row = store.claim()
    assert next_row["id"] != row["id"]
    assert next_row["pending_recheck"]
    with store.connect() as db:
        retained = db.execute(
            "SELECT pending_rechecks,pending_due_at FROM submissions WHERE id=?", (row["id"],)
        ).fetchone()
    assert retained["pending_rechecks"] == 0
    assert retained["pending_due_at"] >= time.time() + 55


def test_schema_five_migration_preserves_queue_and_pending_deadlines(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    fill_pending(store, 1)
    store.submit("first", "{}", 100)
    store.submit("second", "{}", 100)
    with store.connect() as db:
        deadline = db.execute(
            "SELECT pending_due_at FROM submissions WHERE id='pending-0'"
        ).fetchone()[0]
        db.execute("UPDATE submissions SET created_at=100 WHERE id='first'")
        db.execute("UPDATE submissions SET created_at=200 WHERE id='second'")
        db.execute("DROP INDEX submissions_fifo")
        db.execute("ALTER TABLE submissions DROP COLUMN enqueued_at")
        db.execute("ALTER TABLE submissions DROP COLUMN background_kind")
        db.execute("PRAGMA user_version=5")
    store.initialize()
    assert store.claim()["id"] == "first"
    store.fail("first", "done", None)
    assert store.claim()["id"] == "second"
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        assert (
            db.execute("SELECT pending_due_at FROM submissions WHERE id='pending-0'").fetchone()[0]
            == deadline
        )
    with sqlite3.connect(next(tmp_path.glob("*.pre-v5-*.bak"))) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    store.initialize()
    assert store.statistics()["submissions"]["failed"] == 1


@pytest.mark.parametrize("value", [0, -1, "invalid", 1001])
def test_window_setting_validated(tmp_path: Path, value: object) -> None:
    config = online_settings(tmp_path).model_dump()
    config["background_recheck_queue_window"] = value
    with pytest.raises(ValidationError):
        type(online_settings(tmp_path))(**config)


def test_manual_pass_bypasses_window_without_admitting_background(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db", True, background_recheck_queue_window=1)
    store.initialize()
    fill_pending(store)
    row = store.claim(["pending-5"])
    assert row["id"] == "pending-5"
    assert row["pending_recheck"]
    assert background_count(store) == 0
    assert store.claim([]) is None
    assert row["created_at"] <= time.time()
