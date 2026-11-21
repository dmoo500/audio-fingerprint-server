"""Verify storage facade compatibility after splitting queue operations."""

import inspect
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_server import settings

import fingerprint_server.database.postgres as postgres
from fingerprint_server.database.sqlite import Store


def test_queue_modules_can_be_imported_first() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import fingerprint_server.database.postgres_queue, "
            "fingerprint_server.database.sqlite_queue",
        ],
        check=True,
    )


def test_queue_methods_keep_unannotated_self() -> None:
    for store in (Store, postgres.PostgresStore):
        assert (
            inspect.signature(store.submit).parameters["self"].annotation is inspect.Parameter.empty
        )


def test_postgres_queue_uses_facade_patches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = postgres.PostgresStore(settings(tmp_path))
    db = Mock()
    db.execute.return_value.fetchone.return_value = None
    connect = Mock(return_value=nullcontext(db))
    monkeypatch.setattr(store, "connect", connect)
    monkeypatch.setattr(store, "_one", Mock(return_value={"count": 0, "bytes": 0}))
    monkeypatch.setattr(postgres, "time", SimpleNamespace(time=lambda: 123.0))
    monkeypatch.setattr(postgres, "initial_submission_state", lambda *_: ("queued", None))

    assert store.submit("first", "{}", 10) == {"id": "first", "state": "queued"}
    connect.assert_called_once_with(write=True)
    assert db.execute.call_args.args[1] == ("first", "{}", "queued", None, 123.0, 0, 123.0)

    monkeypatch.setattr(store, "_one", Mock(return_value={"count": 10, "bytes": 0}))
    with pytest.raises(postgres.QueueFullError):
        store.submit("second", "{}", 10)
