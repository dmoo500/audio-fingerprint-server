"""Persistent, anonymous recognition counters."""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from typing import Any

    from psycopg import Connection, Cursor

    from fingerprint_server.database import Storage

type Outcome = Literal["matched", "no_match", "ambiguous", "failed"]

RECOGNITION_TABLE = """
CREATE TABLE IF NOT EXISTS recognition_metrics (
    minute bigint PRIMARY KEY,
    attempts bigint NOT NULL DEFAULT 0,
    matched bigint NOT NULL DEFAULT 0,
    no_match bigint NOT NULL DEFAULT 0,
    ambiguous bigint NOT NULL DEFAULT 0,
    failed bigint NOT NULL DEFAULT 0
)
"""

PERIODS = {
    "hour": (60, 1),
    "day": (24 * 60, 60),
    "month": (30 * 24 * 60, 24 * 60),
    "year": (365 * 24 * 60, 7 * 24 * 60),
    "all": (None, 30 * 24 * 60),
}


def record_attempt(store: Storage, minute: int) -> None:
    """Persist an attempt before processing it."""
    from fingerprint_server.database.sqlite import Store

    context: AbstractContextManager[sqlite3.Connection | Connection[dict[str, Any]]]
    if isinstance(store, Store):
        context = store.connect()
    else:
        context = store.connect(write=True, notification="recognition")
    with context as db:
        marker = "?" if isinstance(db, sqlite3.Connection) else "%s"
        db.execute(
            f"INSERT INTO recognition_metrics (minute, attempts) VALUES ({marker}, 1) "
            "ON CONFLICT (minute) DO UPDATE SET attempts=recognition_metrics.attempts+1",
            (minute,),
        )


def record_outcome(store: Storage, minute: int, outcome: Outcome) -> None:
    """Persist the outcome of an already counted attempt."""
    from fingerprint_server.database.sqlite import Store

    if outcome not in ("matched", "no_match", "ambiguous", "failed"):
        raise ValueError("Invalid recognition outcome")
    context: AbstractContextManager[sqlite3.Connection | Connection[dict[str, Any]]]
    if isinstance(store, Store):
        context = store.connect()
    else:
        context = store.connect(write=True, notification="recognition")
    with context as db:
        marker = "?" if isinstance(db, sqlite3.Connection) else "%s"
        db.execute(
            f"UPDATE recognition_metrics SET {outcome}={outcome}+1 WHERE minute={marker}",
            (minute,),
        )


def recognition_history(store: Storage, period: str) -> dict:
    """Return aggregated UTC counters for a supported period."""
    if period not in PERIODS:
        raise ValueError("Invalid recognition period")
    duration, bucket_size = PERIODS[period]
    minute = int(time.time()) // 60
    with store.connect() as db:
        is_sqlite = isinstance(db, sqlite3.Connection)
        if is_sqlite:
            installed = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recognition_metrics'"
            ).fetchone()
        else:
            installed_row = db.execute(
                "SELECT to_regclass('recognition_metrics') AS installed"
            ).fetchone()
            installed = installed_row is not None and installed_row["installed"]
        if not installed:
            return {"available": False, "buckets": []}
        parameters = (bucket_size, bucket_size) + (
            (minute - duration + 1, minute) if duration else ()
        )
        rows: sqlite3.Cursor | Cursor[dict[str, Any]]
        if isinstance(db, sqlite3.Connection):
            statement = (
                "SELECT (minute / ?) * ? AS bucket, "
                "sum(attempts) AS attempts, sum(matched) AS matched, "
                "sum(no_match) AS no_match, sum(ambiguous) AS ambiguous, sum(failed) AS failed "
                "FROM recognition_metrics"
            )
            if duration:
                statement += " WHERE minute >= ? AND minute <= ?"
            rows = db.execute(statement + " GROUP BY bucket ORDER BY bucket", parameters)
        else:
            from psycopg import sql

            pg_statement: sql.Composable = sql.SQL(
                "SELECT (minute / %s) * %s AS bucket, "
                "sum(attempts) AS attempts, sum(matched) AS matched, "
                "sum(no_match) AS no_match, sum(ambiguous) AS ambiguous, sum(failed) AS failed "
                "FROM recognition_metrics"
            )
            if duration:
                pg_statement += sql.SQL(" WHERE minute >= %s AND minute <= %s")
            rows = db.execute(
                pg_statement + sql.SQL(" GROUP BY bucket ORDER BY bucket"), parameters
            )
        buckets = [{key: int(row[key]) for key in row.keys()} for row in rows]
        return {
            "available": True,
            "bucket_minutes": bucket_size,
            "start_minute": minute - duration + 1
            if duration
            else (buckets[0]["bucket"] if buckets else None),
            "end_minute": minute + 1
            if duration
            else (buckets[-1]["bucket"] + bucket_size if buckets else None),
            "buckets": buckets,
        }
