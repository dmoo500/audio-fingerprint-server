"""Import a consistent SQLite snapshot into a new isolated PostgreSQL schema."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import logging
import sqlite3
import struct
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

from fingerprint_server.database.application_keys import REGISTRY_VERSION
from fingerprint_server.database.codes import ARTWORK_REASONS, ArtworkState
from fingerprint_server.database.postgres.schema import (
    EXTENSION_TABLES,
    SQLITE_SCHEMA_VERSION,
    TABLES,
    create_tables,
    finish_schema,
)
from fingerprint_server.database.sqlite.migrations import _validate_schema

LOGGER = logging.getLogger(__name__)
COPY_HEADER = b"PGCOPY\n\xff\r\n\x00" + struct.pack("!ii", 0, 0)
COPY_TRAILER = struct.pack("!h", -1)
LANDMARK_ROW = struct.Struct("!hi10siqii")


def create_snapshot(source: Path, destination: Path) -> None:
    """Create a consistent read-only snapshot, including any committed WAL."""
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    uri = f"file:{quote(str(source.resolve()))}?mode=ro"
    with (
        closing(sqlite3.connect(uri, uri=True)) as original,
        closing(sqlite3.connect(destination)) as snapshot,
    ):
        original.backup(snapshot)


def validate_source(source: sqlite3.Connection) -> None:
    """Reject unsupported or structurally corrupt source snapshots."""
    version = source.execute("PRAGMA user_version").fetchone()[0]
    if version != SQLITE_SCHEMA_VERSION:
        raise ValueError(f"Expected SQLite schema {SQLITE_SCHEMA_VERSION}, found {version}")
    if _validate_schema(source, version):
        raise ValueError("Empty SQLite source")
    registry = source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_key_schema'"
    ).fetchone()
    if registry:
        versions = source.execute("SELECT id,version FROM application_key_schema").fetchall()
        if [tuple(row) for row in versions] != [(1, REGISTRY_VERSION)]:
            raise ValueError("Unsupported application key registry version")
        source.execute(
            "SELECT id,name,key_hash,created_at,revoked_at FROM application_keys LIMIT 0"
        )
    result = source.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise ValueError(f"SQLite integrity check failed: {result}")
    orphan = source.execute("PRAGMA foreign_key_check").fetchone()
    if orphan is not None:
        raise ValueError(f"SQLite foreign key check failed: {tuple(orphan)}")


def source_rows(source: sqlite3.Connection, name: str) -> sqlite3.Cursor:
    """Read rows in stable source order, preserving the SQLite FIFO tie-breaker."""
    spec = (TABLES | EXTENSION_TABLES)[name]
    columns = [column for column in spec["columns"] if column != "queue_order"]
    if name == "submissions":
        columns.append("rowid")
    fields = ",".join("rowid" if name == "rowid" else f'"{name}"' for name in columns)
    if name == "artwork_requests":
        installed = {row[1] for row in source.execute("PRAGMA table_info(artwork_requests)")}
        if "state_raw" not in installed:
            known = [state for state in ArtworkState if state.name != "UNKNOWN"]
            cases = " ".join(f"WHEN '{state.name.lower()}' THEN {state.value}" for state in known)
            names = ",".join(f"'{state.name.lower()}'" for state in known)
            fields = fields.replace(
                '"state"', f'(CASE state {cases} ELSE {ArtworkState.UNKNOWN.value} END) AS "state"'
            ).replace(
                '"state_raw"',
                f'(CASE WHEN state IN ({names}) THEN NULL ELSE state END) AS "state_raw"',
            )
            if "reason" in installed:
                fields = fields.replace(
                    '"reason"', f'({ARTWORK_REASONS.encoded_sql("reason")}) AS "reason"'
                )
        if "reason" not in installed:
            fields = fields.replace('"reason"', 'NULL AS "reason"')
    if name == "admin_hourly_metrics":
        installed = {row[1] for row in source.execute("PRAGMA table_info(admin_hourly_metrics)")}
        if "cached_artwork" not in installed:
            fields = fields.replace('"cached_artwork"', 'NULL AS "cached_artwork"')
    return source.execute(f'SELECT {fields} FROM "{name}" ORDER BY {spec["source_order"]}')


def copy_small_table(source: sqlite3.Connection, target: psycopg.Connection, name: str) -> int:
    """Copy a metadata table and compare every imported value."""
    spec = (TABLES | EXTENSION_TABLES)[name]
    columns = spec["columns"]
    fields = sql.SQL(",").join(map(sql.Identifier, columns))
    with target.cursor() as cur:
        with cur.copy(
            sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(name), fields)
        ) as cp:
            count = 0
            for row in source_rows(source, name):
                cp.write_row(row)
                count += 1
    with target.cursor(name=f"verify_{name}") as cur:
        cur.execute(
            sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                fields, sql.Identifier(name), sql.SQL(spec["target_order"])
            )
        )
        sentinel = object()
        for before, after in itertools.zip_longest(
            source_rows(source, name), cur, fillvalue=sentinel
        ):
            if before != after:
                raise ValueError(f"Data mismatch in {name}")
    return count


def copy_landmarks(source: sqlite3.Connection, target: psycopg.Connection) -> int:
    """Copy and verify all landmark rows using PostgreSQL binary COPY."""
    digest = hashlib.sha256()
    digest.update(COPY_HEADER)
    count = 0
    with target.cursor() as cur:
        with cur.copy(
            'COPY landmarks(hash,reference_id,"offset") FROM STDIN (FORMAT binary)'
        ) as cp:
            cp.write(COPY_HEADER)
            buffer = bytearray()
            for fingerprint, reference_id, offset in source.execute(
                'SELECT hash,reference_id,"offset" FROM landmarks '
                'ORDER BY hash,reference_id,"offset"'
            ):
                if not isinstance(fingerprint, bytes) or len(fingerprint) != 10:
                    raise ValueError("Invalid landmark fingerprint")
                buffer.extend(LANDMARK_ROW.pack(3, 10, fingerprint, 8, reference_id, 4, offset))
                count += 1
                if count % 10000 == 0:
                    digest.update(buffer)
                    cp.write(buffer)
                    buffer.clear()
            if buffer:
                digest.update(buffer)
                cp.write(buffer)
            digest.update(COPY_TRAILER)
            cp.write(COPY_TRAILER)
        cur.execute('ALTER TABLE landmarks ADD PRIMARY KEY(hash, reference_id, "offset")')
        verification = hashlib.sha256()
        with cur.copy(
            'COPY (SELECT hash,reference_id,"offset" FROM landmarks '
            'ORDER BY hash,reference_id,"offset") TO STDOUT (FORMAT binary)'
        ) as cp:
            for chunk in cp:
                verification.update(chunk)
        if digest.digest() != verification.digest():
            raise ValueError("Landmark content checksum mismatch")
        cur.execute("SELECT count(*) FROM landmarks")
        if cur.fetchone()[0] != count:
            raise ValueError("Landmark row count mismatch")
    return count


def reset_identity(target: psycopg.Connection, table: str, column: str) -> None:
    """Set an imported identity sequence above its maximum value."""
    with target.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT setval(pg_get_serial_sequence(%s,%s),"
                "greatest(coalesce(max({}),0),1),coalesce(max({}),0)>=1) FROM {}"
            ).format(sql.Identifier(column), sql.Identifier(column), sql.Identifier(table)),
            (table, column),
        )


def migrate(source: sqlite3.Connection, target: psycopg.Connection, schema: str) -> dict[str, int]:
    """Atomically import and verify a source into a new PostgreSQL schema."""
    if not schema or len(schema.encode()) > 63 or "\x00" in schema:
        raise ValueError("Schema name must contain 1–63 UTF-8 bytes and no NUL")
    if target.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("PostgreSQL connection must not have an active transaction")
    validate_source(source)
    counts = {}
    with target.transaction():
        with target.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cur.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema))
            )
        create_tables(target)
        for name in TABLES:
            counts[name] = copy_small_table(source, target, name)
            LOGGER.info("Verified %s: %s rows", name, counts[name])
        present = {
            row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for name, spec in EXTENSION_TABLES.items():
            if name in present:
                target.execute(spec["definition"])
                counts[name] = copy_small_table(source, target, name)
        counts["landmarks"] = copy_landmarks(source, target)
        LOGGER.info("Verified landmarks: %s rows", counts["landmarks"])
        finish_schema(target, imported=True)
        reset_identity(target, "reference_tracks", "id")
        reset_identity(target, "submissions", "queue_order")
    return counts


def main() -> None:
    """Create and verify an import from a SQLite snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="SQLite database or consistent offline backup")
    parser.add_argument("--schema", required=True, help="New, not-yet-existing PostgreSQL schema")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    with tempfile.TemporaryDirectory(prefix="fingerprint-migration-") as directory:
        path = Path(directory) / "snapshot.db"
        LOGGER.info("Creating consistent SQLite snapshot")
        create_snapshot(args.source, path)
        with sqlite3.connect(path) as source, psycopg.connect("") as target:
            counts = migrate(source, target, args.schema)
        LOGGER.info("Migration committed; verified %s total rows", sum(counts.values()))


if __name__ == "__main__":
    main()
