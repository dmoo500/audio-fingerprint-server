"""Single-process SQLite admission before startup recovery."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from test_server import settings

from fingerprint_server.database.sqlite import Store
from fingerprint_server.runtime import runtime
from fingerprint_server.worker import Worker


async def test_second_runtime_cannot_recover_an_active_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def idle_worker(self: Worker) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Worker, "run", idle_worker)
    config = settings(tmp_path)
    async with runtime(config) as (store, _, _, _):
        assert isinstance(store, Store)
        with store.connect() as db:
            db.execute(
                "INSERT INTO submissions(id,document,state,created_at) "
                "VALUES('active','{}','processing',0)"
            )
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import asyncio,sys; "
                "from pathlib import Path; "
                "from fingerprint_server.runtime import runtime; "
                "from fingerprint_server.settings import Settings; "
                "config=Settings(database=Path(sys.argv[1]),"
                "upload_key='synthetic-upload-key-for-tests',"
                "read_key='synthetic-read-key-for-tests'); "
                "exec('async def start():\\n"
                "    async with runtime(config):\\n"
                "        pass\\n'); "
                "asyncio.run(start())",
                str(config.database),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert child.returncode != 0
        assert "SQLite database is already owned" in child.stderr
        with store.connect() as db:
            assert (
                db.execute("SELECT state FROM submissions WHERE id='active'").fetchone()["state"]
                == "processing"
            )


async def test_runtime_releases_owner_and_does_not_block_admin_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def idle_worker(self: Worker) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Worker, "run", idle_worker)
    config = settings(tmp_path)
    async with runtime(config):
        admin = Store(config.database)
        with admin.connect() as db:
            db.execute("INSERT INTO service_state VALUES('admin-test','ok')")
            assert (
                db.execute("SELECT value FROM service_state WHERE key='admin-test'").fetchone()[
                    "value"
                ]
                == "ok"
            )
        with pytest.raises(RuntimeError, match="SQLite database is already owned"):
            with admin.owner():
                pass
    with Store(config.database).owner():
        pass


def test_owner_is_released_when_process_exits(tmp_path: Path) -> None:
    path = tmp_path / "owned.db"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sys; from pathlib import Path; "
            "from fingerprint_server.database.sqlite import Store; "
            "owner=Store(Path(sys.argv[1])).owner(); owner.__enter__(); os._exit(0)",
            str(path),
        ],
        check=True,
        timeout=10,
    )
    with Store(path).owner():
        pass
    assert path.with_name(path.name + ".lock").exists()
