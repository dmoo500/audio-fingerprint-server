"""Export an offline PostgreSQL collection into a new, verified SQLite database."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import logging
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import psycopg
from migrate_sqlite_to_postgres import COPY_HEADER, COPY_TRAILER, LANDMARK_ROW, validate_source
from psycopg import sql

from fingerprint_server.database.migrations import SCHEMA
from fingerprint_server.database.postgres_postings import POSTINGS_VERSION
from fingerprint_server.database.postgres_schema import (
    FORMAT_VERSION,
    SQLITE_SCHEMA_VERSION,
    TABLES,
)

LOGGER = logging.getLogger(__name__)


@contextmanager
def offline_snapshot(
    db: psycopg.Connection, schema: str, *, read_only: bool = True
) -> Iterator[None]:
    """Reject running service roles and hold their locks over a read-only snapshot."""
    if not schema or len(schema.encode()) > 63 or "\x00" in schema:
        raise ValueError("Schema name must contain 1–63 UTF-8 bytes and no NUL")
    if db.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("PostgreSQL connection must not have an active transaction")
    previous_autocommit = db.autocommit
    acquired: list[str] = []
    db.autocommit = True
    try:
        for role in ("api", "worker", "write"):
            key = f"fingerprint:{schema}:{role}"
            row = db.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,)
            ).fetchone()
            if row is None or not row[0]:
                raise RuntimeError(f"Stop all PostgreSQL API/worker processes first: {role} active")
            acquired.append(key)
        with db.transaction():
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            if read_only:
                db.execute("SET TRANSACTION READ ONLY")
            db.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema))
            )
            yield
    finally:
        try:
            for key in reversed(acquired):
                db.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
        finally:
            db.autocommit = previous_autocommit


def validate_postgres(db: psycopg.Connection, schema: str) -> None:
    """Require the exact supported PostgreSQL source layout and provenance."""
    versions = db.execute(
        "SELECT format_version,sqlite_schema_version FROM migration_info"
    ).fetchall()
    if versions not in (
        [(FORMAT_VERSION, SQLITE_SCHEMA_VERSION)],
        [(POSTINGS_VERSION, SQLITE_SCHEMA_VERSION)],
    ):
        raise ValueError(f"Unsupported PostgreSQL format/provenance: {versions}")
    expected = {name: set(spec["columns"]) for name, spec in TABLES.items()}
    expected["landmarks"] = {"hash", "reference_id", "offset"}
    expected["migration_info"] = {"format_version", "sqlite_schema_version"}
    if versions[0][0] == POSTINGS_VERSION:
        expected["landmark_postings"] = {
            "hash",
            "reference_block",
            "offset_block",
            "reference_ids",
            "offsets",
        }
    actual: dict[str, set[str]] = {}
    for table, column in db.execute(
        "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=%s",
        (schema,),
    ):
        actual.setdefault(table, set()).add(column)
    if actual != expected:
        raise ValueError("Unexpected PostgreSQL source layout")


def copy_metadata(db: psycopg.Connection, target: sqlite3.Connection, name: str) -> int:
    """Copy and compare every metadata field, preserving submission rowids and FIFO."""
    spec = TABLES[name]
    columns = spec["columns"]
    fields = sql.SQL(",").join(map(sql.Identifier, columns))
    target_fields = ",".join(
        '"rowid"' if field == "queue_order" else f'"{field}"' for field in columns
    )
    statement = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        fields, sql.Identifier(name), sql.SQL(spec["target_order"])
    )
    count = 0
    with db.cursor(name=f"export_{name}") as cursor:
        cursor.execute(statement)
        while rows := cursor.fetchmany(1000):
            target.executemany(
                f'INSERT INTO "{name}" ({target_fields}) VALUES ({",".join("?" for _ in columns)})',
                rows,
            )
            count += len(rows)
    with db.cursor(name=f"verify_{name}") as cursor:
        cursor.execute(statement)
        sentinel = object()
        after = target.execute(
            f'SELECT {target_fields} FROM "{name}" ORDER BY {spec["source_order"]}'
        )
        for before, exported in itertools.zip_longest(cursor, after, fillvalue=sentinel):
            if before != exported:
                raise ValueError(f"SQLite content mismatch in {name}")
    return count


def copy_landmarks(db: psycopg.Connection, target: sqlite3.Connection) -> tuple[int, str]:
    """Stream landmarks and verify a canonical binary checksum after SQLite insertion."""
    digest = hashlib.sha256(COPY_HEADER)
    count = 0
    with db.cursor(name="export_landmarks") as cursor:
        cursor.execute(
            'SELECT hash,reference_id,"offset" FROM landmarks ORDER BY hash,reference_id,"offset"'
        )
        while rows := cursor.fetchmany(10000):
            target.executemany("INSERT INTO landmarks VALUES (?,?,?)", rows)
            for fingerprint, reference_id, offset in rows:
                digest.update(LANDMARK_ROW.pack(3, 10, fingerprint, 8, reference_id, 4, offset))
            count += len(rows)
            if count % 1000000 == 0:
                LOGGER.info("Exported %s landmarks", count)
    digest.update(COPY_TRAILER)
    exported_digest = hashlib.sha256(COPY_HEADER)
    exported_count = 0
    for fingerprint, reference_id, offset in target.execute(
        'SELECT hash,reference_id,"offset" FROM landmarks ORDER BY hash,reference_id,"offset"'
    ):
        exported_digest.update(LANDMARK_ROW.pack(3, 10, fingerprint, 8, reference_id, 4, offset))
        exported_count += 1
    exported_digest.update(COPY_TRAILER)
    if count != exported_count or digest.digest() != exported_digest.digest():
        raise ValueError("SQLite landmark count/checksum mismatch")
    return count, digest.hexdigest()


def export_sqlite(db: psycopg.Connection, schema: str, destination: Path) -> dict:
    """
    Create a verified SQLite export without replacing any existing path.

    :param db: Dedicated idle PostgreSQL connection using tuple rows.
    :param schema: Offline source schema; service ownership locks must be available.
    :param destination: New SQLite file in an existing directory.
    """
    if os.path.lexists(destination):
        raise FileExistsError(f"SQLite destination already exists: {destination}")
    with offline_snapshot(db, schema):
        validate_postgres(db, schema)
        fd, filename = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
        )
        staging = Path(filename)
        os.close(fd)
        try:
            with closing(sqlite3.connect(staging)) as target:
                target.execute("PRAGMA foreign_keys=ON")
                target.execute("BEGIN")
                indexes = []
                for statement in SCHEMA.split(";"):
                    if not statement.strip():
                        continue
                    if statement.strip().startswith("CREATE INDEX"):
                        indexes.append(statement)
                    else:
                        target.execute(statement)
                counts = {}
                for name in TABLES:
                    counts[name] = copy_metadata(db, target, name)
                    LOGGER.info("Verified %s: %s rows", name, counts[name])
                counts["landmarks"], checksum = copy_landmarks(db, target)
                LOGGER.info("Building SQLite secondary indexes")
                for statement in indexes:
                    target.execute(statement)
                target.execute(f"PRAGMA user_version={SQLITE_SCHEMA_VERSION}")
                target.commit()
                validate_source(target)
                target.execute("ANALYZE")
                target.commit()
            with staging.open("rb") as file:
                os.fsync(file.fileno())
            # A same-directory hard link publishes the checked file without overwrite races.
            os.link(staging, destination)
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                destination.unlink()
                raise
            return {"counts": counts, "landmarks_sha256": checksum}
        finally:
            staging.unlink()
            for suffix in ("-journal", "-wal", "-shm"):
                staging.with_name(staging.name + suffix).unlink(missing_ok=True)


def main() -> None:
    """Export a stopped PostgreSQL service using private libpq environment credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New SQLite file; never overwritten")
    parser.add_argument("--schema", required=True, help="Offline PostgreSQL source schema")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    with psycopg.connect("") as db:
        result = export_sqlite(db, args.schema, args.destination)
    LOGGER.info("SQLite export verified and published: %s", result)


if __name__ == "__main__":
    main()
