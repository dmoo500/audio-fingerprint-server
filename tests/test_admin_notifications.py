"""Verify committed SQLite writes signal the local administrator."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from fingerprint_server.admin.live import AdminLive
from fingerprint_server.admin.sqlite_data import SQLiteAdminData
from fingerprint_server.database.notifications import socket_name
from fingerprint_server.database.recognition_metrics import record_attempt, record_outcome
from fingerprint_server.database.sqlite import Store
from fingerprint_server.settings import Settings


async def test_sqlite_socket_signals_commits_but_not_rollbacks() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        path = Path(directory) / "collection.db"
        store = Store(path)
        store.initialize()
        settings = Settings.model_validate(
            {"database": path, "upload_key": "u" * 32, "read_key": "r" * 32}
        )
        data = SQLiteAdminData(settings, "http://127.0.0.1/health")
        live = AdminLive(data)
        task = asyncio.create_task(live._listen_sqlite())
        try:
            async with asyncio.timeout(2):
                while not (path.parent / socket_name(path)).exists():
                    await asyncio.sleep(0.01)
            live.pending.clear()
            live._overview_pending = live._recognition_pending = False
            await asyncio.to_thread(record_attempt, store, 100)
            await asyncio.wait_for(live.pending.wait(), timeout=2)
            assert live._recognition_pending
            assert not live._overview_pending
            live.pending.clear()
            live._recognition_pending = False
            await asyncio.to_thread(record_outcome, store, 100, "matched")
            await asyncio.wait_for(live.pending.wait(), timeout=2)
            live.pending.clear()
            with store.connect() as db:
                db.execute(
                    "INSERT INTO submissions(id,document,state,created_at) "
                    "VALUES('example','{}','failed',0)"
                )
            await asyncio.wait_for(live.pending.wait(), timeout=2)
            assert live._overview_pending
            live.pending.clear()
            with pytest.raises(ValueError):
                with store.connect() as db:
                    db.execute("UPDATE recognition_metrics SET attempts=4")
                    raise ValueError("rollback")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(live.pending.wait(), timeout=0.1)
            with store.connect() as db:
                db.execute("UPDATE recognition_metrics SET attempts=4")
                db.rollback()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(live.pending.wait(), timeout=0.1)
            with store.connect() as db:
                assert db.execute("SELECT attempts FROM recognition_metrics").fetchone()[0] == 1
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            data.close()
        assert not (path.parent / socket_name(path)).exists()


async def test_sqlite_commit_updates_live_snapshot(monkeypatch) -> None:
    from fingerprint_server.admin import live as module

    monkeypatch.setattr(module, "UPDATE_INTERVAL", 0.05)
    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 60)
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        path = Path(directory) / "collection.db"
        store = Store(path)
        store.initialize()
        settings = Settings.model_validate(
            {"database": path, "upload_key": "u" * 32, "read_key": "r" * 32}
        )
        data = SQLiteAdminData(settings, "http://127.0.0.1/health")

        def overview():
            with store.connect() as db:
                count = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
            return {"submissions": {"total": count}, "references": {"total": 0}}

        monkeypatch.setattr(data, "overview", overview)
        live = AdminLive(data)
        await live.sample()
        live.subscribers = 1
        listener = asyncio.create_task(live._listen_sqlite())
        runner = asyncio.create_task(live.run())
        try:
            async with asyncio.timeout(2):
                while not (path.parent / socket_name(path)).exists():
                    await asyncio.sleep(0.01)
                while live.recognition_revision < 1:
                    await asyncio.sleep(0.01)
            old_revision = live.snapshot["revisions"]["submissions"]
            old_recognition = live.recognition_revision
            await asyncio.to_thread(record_attempt, store, 100)
            async with asyncio.timeout(2):
                while live.recognition_revision == old_recognition:
                    await asyncio.sleep(0.01)
            assert live.snapshot["revisions"]["submissions"] == old_revision
            with store.connect() as db:
                db.execute(
                    "INSERT INTO submissions(id,document,state,created_at) "
                    "VALUES('example','{}','failed',0)"
                )
            async with asyncio.timeout(2):
                while live.snapshot["revisions"]["submissions"] == old_revision:
                    await asyncio.sleep(0.01)
            assert live.snapshot["overview"]["submissions"]["total"] == 1
            old_revision = live.snapshot["revisions"]["submissions"]
            with store.connect() as db:
                db.execute("UPDATE submissions SET error='changed' WHERE id='example'")
            async with asyncio.timeout(2):
                while live.snapshot["revisions"]["submissions"] == old_revision:
                    await asyncio.sleep(0.01)
            assert live.snapshot["overview"]["submissions"]["total"] == 1
        finally:
            listener.cancel()
            runner.cancel()
            for task in (listener, runner):
                with pytest.raises(asyncio.CancelledError):
                    await task
            data.close()
