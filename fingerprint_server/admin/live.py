"""Shared, bounded sampling for authenticated administration event streams."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import socket
import sqlite3
import stat
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql

from fingerprint_server.admin.data import AdminData
from fingerprint_server.admin.sqlite_data import SQLiteAdminData
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database.notifications import postgres_channel, socket_name

LOGGER = logging.getLogger(__name__)
UPDATE_INTERVAL = 5.0
RECONCILE_INTERVAL = 15.0


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, live: AdminLive) -> None:
        self.live = live

    def datagram_received(self, data: bytes, addr: object) -> None:
        if data in (b"all", b"recognition"):
            self.live.invalidate(data.decode())

    def error_received(self, exc: Exception) -> None:
        LOGGER.warning("Admin notification socket error: %s", exc)


class AdminLive:
    """Share one current snapshot across browsers without buffering event histories."""

    def __init__(self, data: AdminData | SQLiteAdminData) -> None:
        self.data = data
        self.snapshot: dict[str, Any] = {}
        self.failed = False
        self.subscribers = 0
        self.changed = asyncio.Event()
        self.pending = asyncio.Event()
        self.recognition_revision = 0
        self._recognition_pending = False
        self._overview_pending = False
        self._table_generation = uuid4().hex

    def invalidate(self, category: str, *, reconcile: bool = False) -> None:
        """Schedule a bounded refresh for a committed change."""
        self._recognition_pending = True
        if category != "recognition":
            self._overview_pending = True
            if not reconcile:
                self._table_generation = uuid4().hex
        self.pending.set()

    async def sample(self, *, overview: bool = True, recognition: bool = False) -> None:
        """Publish fresh counts and advisory table revisions, or report a database error."""
        generation = self._table_generation
        try:
            if overview:
                values = await run_sync(self.data.overview)
                counters = await run_sync(self.data.revisions)
        except (psycopg.Error, sqlite3.Error, OSError):
            LOGGER.exception("Admin live data sampling failed")
            self.failed = True
        else:
            revisions = dict(self.snapshot.get("revisions", {}))
            if overview:
                for name in ("submissions", "references"):
                    value = [counters[name], values[name], generation]
                    revisions[name] = hashlib.sha256(
                        json.dumps(value, sort_keys=True).encode()
                    ).hexdigest()
                revisions["unresolved"] = revisions["submissions"]
            if recognition:
                self.recognition_revision += 1
            revisions["recognition"] = str(self.recognition_revision)
            self.snapshot = {
                "overview": values if overview else self.snapshot["overview"],
                "revisions": revisions,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            self.failed = False
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()

    async def run(self) -> None:
        """Coalesce notifications and periodically reconcile missed changes."""
        last_sample = 0.0
        next_reconcile = time.monotonic() + RECONCILE_INTERVAL
        while True:
            try:
                await asyncio.wait_for(
                    self.pending.wait(), timeout=max(0, next_reconcile - time.monotonic())
                )
            except TimeoutError:
                if self.subscribers:
                    self.invalidate("all", reconcile=True)
                next_reconcile = time.monotonic() + RECONCILE_INTERVAL
            if not self.subscribers:
                self.pending.clear()
                self._overview_pending = self._recognition_pending = False
                continue
            await asyncio.sleep(max(0, last_sample + UPDATE_INTERVAL - time.monotonic()))
            overview = self._overview_pending or self.failed
            recognition = self._recognition_pending
            self.pending.clear()
            self._overview_pending = self._recognition_pending = False
            await self.sample(overview=overview, recognition=recognition)
            last_sample = time.monotonic()

    async def listen(self) -> None:
        """Listen for committed changes, retrying failed notification transports."""
        while True:
            try:
                if isinstance(self.data, SQLiteAdminData):
                    await self._listen_sqlite()
                else:
                    await self._listen_postgres()
                LOGGER.warning(
                    "Admin notification listener stopped; periodic reconciliation remains active"
                )
            except (OSError, psycopg.Error):
                LOGGER.exception(
                    "Admin notifications unavailable; periodic reconciliation remains active"
                )
            await asyncio.sleep(10)

    async def _listen_sqlite(self) -> None:
        path = self.data.settings.database.resolve().parent
        socket_path = path / socket_name(self.data.settings.database)
        lock_path = socket_path.with_suffix(".lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if socket_path.exists():
                if not stat.S_ISSOCK(socket_path.lstat().st_mode):
                    raise OSError("Admin notification path is not a socket")
                socket_path.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            bound = False
            try:
                sock.setblocking(False)
                sock.bind(str(socket_path))
                bound = True
                os.chmod(socket_path, 0o660)
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: _Datagrams(self), sock=sock
                )
                self.invalidate("all")
                try:
                    await asyncio.Future()
                finally:
                    transport.close()
            finally:
                sock.close()
                if bound:
                    socket_path.unlink(missing_ok=True)

    async def _listen_postgres(self) -> None:
        settings = self.data.settings
        dsn = settings.postgres_dsn
        db = await psycopg.AsyncConnection.connect(
            dsn.get_secret_value() if dsn is not None else "", autocommit=True, connect_timeout=5
        )
        async with db:
            channel = sql.Identifier(postgres_channel(settings.postgres_schema))
            await db.execute(sql.SQL("LISTEN {}").format(channel))
            self.invalidate("all")
            async for notification in db.notifies():
                if notification.payload in ("all", "recognition"):
                    self.invalidate(notification.payload)
