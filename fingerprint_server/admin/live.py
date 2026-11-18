"""Shared, bounded sampling for authenticated administration event streams."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import psycopg

from fingerprint_server.admin.data import AdminData
from fingerprint_server.async_utils import run_sync

LOGGER = logging.getLogger(__name__)


class AdminLive:
    """Share one current snapshot across browsers without buffering event histories."""

    def __init__(self, data: AdminData) -> None:
        self.data = data
        self.snapshot: dict[str, Any] = {}
        self.failed = False
        self.subscribers = 0
        self.changed = asyncio.Event()

    async def sample(self) -> None:
        """Publish fresh counts and advisory table revisions, or report a database error."""
        try:
            overview = await run_sync(self.data.overview)
            counters = await run_sync(self.data.revisions)
        except (psycopg.Error, OSError):
            LOGGER.exception("Admin live data sampling failed")
            self.failed = True
        else:
            revisions = {}
            for name in ("submissions", "references"):
                value = [counters[name], overview[name]]
                revisions[name] = hashlib.sha256(
                    json.dumps(value, sort_keys=True).encode()
                ).hexdigest()
            revisions["unresolved"] = revisions["submissions"]
            self.snapshot = {
                "overview": overview,
                "revisions": revisions,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            self.failed = False
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()

    async def run(self) -> None:
        """Sample once every five seconds while at least one browser is connected."""
        while True:
            await asyncio.sleep(5)
            if self.subscribers:
                await self.sample()
