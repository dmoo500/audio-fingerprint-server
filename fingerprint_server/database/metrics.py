"""Hourly, bounded operational measurements without reading landmark payloads."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import TYPE_CHECKING

from fingerprint_server.async_utils import run_sync

if TYPE_CHECKING:
    from fingerprint_server.database import Storage

LOGGER = logging.getLogger(__name__)
HOUR = 3600
HISTORY_HOURS = 30 * 24
METRICS_TABLE = """
CREATE TABLE IF NOT EXISTS admin_hourly_metrics (
    hour bigint PRIMARY KEY,
    recordings bigint NOT NULL,
    queued bigint NOT NULL,
    processing bigint NOT NULL,
    pending_metadata bigint NOT NULL,
    manual_review bigint NOT NULL
)
"""
REVIEW_COUNT = """
SELECT count(*) AS count FROM musicbrainz_reviews r
JOIN submissions s ON s.id=r.submission_id
WHERE s.state='pending_metadata' AND r.selected_mbid IS NULL
"""


def capture_hour(store: Storage, hour: int) -> None:
    """Keep the first consistent measurement for a UTC hour and expire older buckets."""
    with store.connect() as db:
        marker = "?" if isinstance(db, sqlite3.Connection) else "%s"
        if not isinstance(db, sqlite3.Connection):
            db.execute("SET LOCAL statement_timeout='5s'")
        if db.execute(
            f"SELECT 1 FROM admin_hourly_metrics WHERE hour={marker}", (hour,)
        ).fetchone():
            return
        db.execute(
            f"""
            INSERT INTO admin_hourly_metrics
            SELECT {marker},
                (SELECT count(DISTINCT coalesce(nullif(recording_key,''), 'reference:' || id))
                 FROM reference_tracks WHERE publication_ready=1),
                count(*) FILTER (WHERE state='queued'),
                count(*) FILTER (WHERE state='processing'),
                count(*) FILTER (WHERE state='pending_metadata'),
                ({REVIEW_COUNT})
            FROM submissions
            WHERE state IN ('queued','processing','pending_metadata')
            ON CONFLICT(hour) DO NOTHING
            """,
            (hour,),
        )
        db.execute(
            f"DELETE FROM admin_hourly_metrics WHERE hour < {marker}",
            (hour - HISTORY_HOURS + 1,),
        )


async def sample_history(store: Storage) -> None:
    """Record the current UTC hour independently of ingestion and MusicBrainz cooldowns."""
    from fingerprint_server.database.sqlite import Store

    database_error: type[Exception] = sqlite3.Error
    if not isinstance(store, Store):
        from psycopg import Error

        database_error = Error

    while True:
        hour = int(time.time()) // HOUR
        try:
            await run_sync(capture_hour, store, hour)
        except database_error:
            LOGGER.exception("Hourly admin metrics capture failed")
        await asyncio.sleep(max(0, (hour + 1) * HOUR - time.time()))
