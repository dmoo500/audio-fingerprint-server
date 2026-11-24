"""Persistent aggregate recognition counters."""

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
type Identity = tuple[str, str]

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

RECOGNITION_IDENTITY_TABLE = """
CREATE TABLE IF NOT EXISTS recognition_identity_metrics (
    minute bigint NOT NULL,
    personal text NOT NULL,
    application text NOT NULL,
    attempts bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (minute, personal, application)
)
"""

PERIODS = {
    "hour": (60, 1),
    "day": (24 * 60, 60),
    "week": (7 * 24 * 60, 24 * 60),
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


def record_outcome(
    store: Storage, minute: int, outcome: Outcome, identity: Identity | None = None
) -> None:
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
        if identity is not None:
            db.execute(
                "INSERT INTO recognition_identity_metrics "
                "(minute, personal, application, attempts) "
                f"VALUES ({marker}, {marker}, {marker}, 1) "
                "ON CONFLICT (minute, personal, application) DO UPDATE "
                "SET attempts=recognition_identity_metrics.attempts+1",
                (minute, *identity),
            )


def recognition_history(store: Storage, period: str, *, include_identities: bool = False) -> dict:
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
        identity_installed = False
        identities: list[dict[str, Any]] = []
        if include_identities and is_sqlite:
            identity_installed = (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='recognition_identity_metrics'"
                ).fetchone()
                is not None
            )
        elif include_identities:
            installed_row = db.execute(
                "SELECT to_regclass('recognition_identity_metrics') AS installed"
            ).fetchone()
            identity_installed = bool(installed_row is not None and installed_row["installed"])
        if identity_installed:
            marker = "?" if is_sqlite else "%s"
            statement = (
                "SELECT personal, application, sum(attempts) AS attempts "
                "FROM recognition_identity_metrics"
            )
            identity_parameters: tuple[int, ...] = ()
            if duration:
                statement += f" WHERE minute >= {marker} AND minute <= {marker}"
                identity_parameters = (minute - duration + 1, minute)
            for row in db.execute(
                statement + " GROUP BY personal, application "
                "ORDER BY attempts DESC, personal, application",
                identity_parameters,
            ):
                identities.append(
                    {
                        "personal": row["personal"],
                        "application": row["application"],
                        "attempts": int(row["attempts"]),
                    }
                )
        return {
            "available": True,
            "identities_available": identity_installed,
            "identities": identities,
            "bucket_minutes": bucket_size,
            "start_minute": minute - duration + 1
            if duration
            else (buckets[0]["bucket"] if buckets else None),
            "end_minute": minute + 1
            if duration
            else (buckets[-1]["bucket"] + bucket_size if buckets else None),
            "buckets": buckets,
        }
