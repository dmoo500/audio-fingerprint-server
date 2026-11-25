"""Hourly, bounded operational measurements without reading landmark payloads."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from fingerprint_server.async_utils import run_sync
from fingerprint_server.database.codes import ArtworkState, SubmissionState
from fingerprint_server.database.recognition_metrics import (
    RECOGNITION_IDENTITY_TABLE,
    RECOGNITION_TABLE,
)

if TYPE_CHECKING:
    from psycopg import Connection

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
    manual_review bigint NOT NULL,
    cached_artwork bigint
)
"""
REVIEW_COUNT = f"""
SELECT count(*) AS count FROM musicbrainz_reviews r
JOIN submissions s ON s.id=r.submission_id
WHERE s.state={SubmissionState.PENDING_METADATA.value} AND r.selected_mbid IS NULL
"""
SEARCHABLE_RECORDINGS_COUNT = """
count(DISTINCT coalesce(nullif(recording_key,''), 'reference:' || id))
FILTER (WHERE publication_ready=1)
"""
REFERENCE_COUNTS = f"""
SELECT count(*) AS stored,count(*) FILTER (WHERE publication_ready=1) AS published,
       {SEARCHABLE_RECORDINGS_COUNT} AS searchable_recordings
FROM reference_tracks
"""


def initialize_metrics(db: sqlite3.Connection | Connection[dict[str, Any]]) -> None:
    """Create the metrics table and preserve older samples during its additive upgrade."""
    if not isinstance(db, sqlite3.Connection):
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || "
            "':fingerprint:metrics',0))"
        )
    db.execute(METRICS_TABLE)
    db.execute(RECOGNITION_TABLE)
    db.execute(RECOGNITION_IDENTITY_TABLE)
    if isinstance(db, sqlite3.Connection):
        columns = {row[1] for row in db.execute("PRAGMA table_info(admin_hourly_metrics)")}
    else:
        columns = {
            row["column_name"]
            for row in db.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='admin_hourly_metrics'"
            )
        }
    if "cached_artwork" not in columns:
        db.execute("ALTER TABLE admin_hourly_metrics ADD COLUMN cached_artwork bigint")


def capture_hour(store: Storage, hour: int) -> None:
    """Keep the first consistent measurement for a UTC hour and expire older buckets."""
    with store.connect() as db:
        marker = "?" if isinstance(db, sqlite3.Connection) else "%s"
        if not isinstance(db, sqlite3.Connection):
            db.execute("SET LOCAL statement_timeout='5s'")
        is_sqlite = isinstance(db, sqlite3.Connection)
        if is_sqlite:
            artwork_installed = (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artwork_requests'"
                ).fetchone()
                is not None
            )
        else:
            installed_row = db.execute(
                "SELECT to_regclass('artwork_requests') IS NOT NULL AS installed"
            ).fetchone()
            if installed_row is None:
                raise RuntimeError("Cannot determine whether artwork cache is installed")
            artwork_installed = installed_row["installed"]
        album = "json_extract(t.metadata,'$.album')" if is_sqlite else "t.metadata::jsonb->>'album'"
        mbid = (
            "json_extract(t.metadata,'$.recording_mbid')"
            if is_sqlite
            else "t.metadata::jsonb->>'recording_mbid'"
        )
        cached_artwork = None
        if artwork_installed:
            count_row = db.execute(
                "SELECT count(*) AS count FROM artwork_requests a "
                "JOIN reference_tracks t ON t.reference_key=a.reference_key "
                f"WHERE t.publication_ready=1 AND a.state={ArtworkState.READY.value} "
                f"AND a.expires_at>{marker} AND trim({album})<>'' AND trim({mbid})<>''",
                (time.time(),),
            ).fetchone()
            if count_row is None:
                raise RuntimeError("Cannot measure cached artwork")
            cached_artwork = count_row["count"]
        queued = SubmissionState.QUEUED.value
        processing = SubmissionState.PROCESSING.value
        pending = SubmissionState.PENDING_METADATA.value
        db.execute(
            f"""
            INSERT INTO admin_hourly_metrics
                (hour,recordings,queued,processing,pending_metadata,manual_review,cached_artwork)
            SELECT {marker},
                (SELECT {SEARCHABLE_RECORDINGS_COUNT} FROM reference_tracks),
                count(*) FILTER (WHERE state={queued}),
                count(*) FILTER (WHERE state={processing}),
                count(*) FILTER (WHERE state={pending}),
                ({REVIEW_COUNT}),
                {marker}
            FROM submissions
            WHERE state IN ({queued},{processing},{pending})
            ON CONFLICT(hour) DO UPDATE SET cached_artwork=excluded.cached_artwork
            WHERE admin_hourly_metrics.cached_artwork IS NULL
            """,
            (hour, cached_artwork),
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
