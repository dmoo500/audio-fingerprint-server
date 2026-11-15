"""Transactional SQLite upgrades independent of API and fingerprint format versions."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from music_assistant_fingerprint.codec import encode_document

from fingerprint_server.schemas import decode_submission, metadata_complete

LOGGER = logging.getLogger(__name__)
DB_SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE submissions (
    id TEXT PRIMARY KEY, document TEXT NOT NULL, state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
    error TEXT, reference_id TEXT, created_at REAL NOT NULL
);
CREATE INDEX submissions_pending ON submissions(state, retry_at, created_at);
CREATE TABLE reference_tracks (
    id INTEGER PRIMARY KEY, reference_key TEXT NOT NULL UNIQUE,
    metadata TEXT NOT NULL, created_at REAL NOT NULL,
    publication_ready INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX references_ready ON reference_tracks(publication_ready,id);
CREATE TABLE landmarks (
    hash BLOB NOT NULL CHECK(typeof(hash)='blob' AND length(hash)=10),
    reference_id INTEGER NOT NULL REFERENCES reference_tracks(id),
    offset INTEGER NOT NULL, PRIMARY KEY(hash, reference_id, offset)
) WITHOUT ROWID;
CREATE TABLE enrichment_cache (
    isrc TEXT PRIMARY KEY, document TEXT NOT NULL, expires_at REAL NOT NULL
);
"""

LEGACY_COLUMNS = {
    "submissions": {
        "id",
        "document",
        "state",
        "attempts",
        "retry_at",
        "error",
        "reference_id",
        "created_at",
    },
    "reference_tracks": {"id", "metadata", "created_at"},
    "landmarks": {"hash", "reference_id", "offset"},
    "enrichment_cache": {"isrc", "document", "expires_at"},
}


def initialize_database(db: sqlite3.Connection, path: Path) -> None:
    """
    Upgrade a recognized database before the service starts.

    :param db: Connection with foreign keys enabled and no active transaction.
    :param path: Database path used for a pre-upgrade SQLite backup.
    """
    version = db.execute("PRAGMA user_version").fetchone()[0]
    empty = _validate_schema(db, version)
    if not empty and version < DB_SCHEMA_VERSION:
        backup_path = path.with_name(f"{path.name}.pre-v{version}-{time.time_ns()}.bak")
        with backup_path.open("xb"):
            pass
        backup_path.chmod(0o600)
        try:
            with closing(sqlite3.connect(backup_path)) as backup:
                db.backup(backup)
        except sqlite3.Error:
            backup_path.unlink()
            raise
        LOGGER.warning("Backing up fingerprint database before upgrade: %s", backup_path)
        LOGGER.info("Upgrading fingerprint DB schema from %s to %s", version, DB_SCHEMA_VERSION)

    db.execute("PRAGMA journal_mode=WAL")
    db.execute("BEGIN IMMEDIATE")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        empty = _validate_schema(db, version)
        if empty:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION}")
        else:
            if version < 1:
                _migrate_publication_gate(db)
                db.execute("PRAGMA user_version=1")
            if version < 2:
                _migrate_binary_landmarks(db)
                db.execute("PRAGMA user_version=2")
            if version < 3:
                _migrate_compact_documents(db)
                db.execute("PRAGMA user_version=3")
        if (empty or version < DB_SCHEMA_VERSION) and db.execute(
            "PRAGMA foreign_key_check"
        ).fetchone() is not None:
            raise RuntimeError("Fingerprint database contains orphaned landmark references")
        db.execute("UPDATE submissions SET state='queued' WHERE state='processing'")
        db.commit()
    except (sqlite3.Error, RuntimeError, ValueError):
        db.rollback()
        LOGGER.exception("Fingerprint database upgrade failed; original data retained: %s", path)
        raise


def _validate_schema(db: sqlite3.Connection, version: int) -> bool:
    if version < 0 or version > DB_SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported fingerprint DB schema version {version}; "
            f"this server supports up to {DB_SCHEMA_VERSION}. "
            "Use a compatible server or restore a pre-upgrade backup."
        )
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables and version == 0:
        return True
    if tables != LEGACY_COLUMNS.keys():
        raise RuntimeError("Unrecognized fingerprint database tables; refusing to migrate")
    for table, legacy in LEGACY_COLUMNS.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        expected = legacy.copy()
        if table == "reference_tracks":
            if version >= 1 or "publication_ready" in columns:
                expected.add("publication_ready")
            if version >= 2:
                expected.add("reference_key")
        if columns != expected:
            raise RuntimeError(
                f"Unrecognized fingerprint database columns in {table}; refusing to migrate"
            )
    reference_columns = {
        row[1]: row[2].upper() for row in db.execute("PRAGMA table_info(reference_tracks)")
    }
    landmark_columns = {
        row[1]: row[2].upper() for row in db.execute("PRAGMA table_info(landmarks)")
    }
    compact = version >= 2
    if (
        reference_columns["id"] != ("INTEGER" if compact else "TEXT")
        or landmark_columns["hash"] != ("BLOB" if compact else "TEXT")
        or landmark_columns["reference_id"] != ("INTEGER" if compact else "TEXT")
    ):
        raise RuntimeError("Unrecognized fingerprint database column types; refusing to migrate")
    if compact:
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='landmarks'"
        ).fetchone()[0]
        if "WITHOUT ROWID" not in sql.upper():
            raise RuntimeError("Unrecognized fingerprint landmark layout; refusing to migrate")
    return False


def _migrate_publication_gate(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(reference_tracks)")}
    if "publication_ready" not in columns:
        db.execute(
            "ALTER TABLE reference_tracks ADD COLUMN publication_ready INTEGER NOT NULL DEFAULT 0"
        )
    for row in db.execute(
        "SELECT id,metadata FROM reference_tracks WHERE publication_ready=0"
    ).fetchall():
        try:
            complete = metadata_complete(json.loads(row[1]))
        except (TypeError, ValueError, UnicodeError):
            complete = False
        if complete:
            db.execute("UPDATE reference_tracks SET publication_ready=1 WHERE id=?", (row[0],))
    db.execute(
        "UPDATE submissions SET state='pending_metadata',error='Incomplete metadata' "
        "WHERE state='processed' AND reference_id IN "
        "(SELECT id FROM reference_tracks WHERE publication_ready=0)"
    )


def _migrate_binary_landmarks(db: sqlite3.Connection) -> None:
    if (
        db.execute(
            "SELECT 1 FROM landmarks WHERE typeof(hash)!='text' OR length(hash)!=20 "
            "OR hash GLOB '*[^0-9a-f]*' LIMIT 1"
        ).fetchone()
        is not None
    ):
        raise RuntimeError("Invalid stored landmark hash; refusing to upgrade")
    db.execute(
        "CREATE TABLE reference_tracks_new ("
        "id INTEGER PRIMARY KEY, reference_key TEXT NOT NULL UNIQUE, metadata TEXT NOT NULL, "
        "created_at REAL NOT NULL, publication_ready INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute(
        "INSERT INTO reference_tracks_new(reference_key,metadata,created_at,publication_ready) "
        "SELECT id,metadata,created_at,publication_ready FROM reference_tracks ORDER BY id"
    )
    db.execute(
        "CREATE TABLE landmarks_new ("
        "hash BLOB NOT NULL CHECK(typeof(hash)='blob' AND length(hash)=10), "
        "reference_id INTEGER NOT NULL REFERENCES reference_tracks_new(id), "
        "offset INTEGER NOT NULL, PRIMARY KEY(hash,reference_id,offset)) WITHOUT ROWID"
    )
    db.create_function("fingerprint_hash_bytes", 1, _hash_bytes, deterministic=True)
    try:
        db.execute(
            "INSERT INTO landmarks_new(hash,reference_id,offset) "
            "SELECT fingerprint_hash_bytes(l.hash),t.id,l.offset FROM landmarks l "
            "JOIN reference_tracks_new t ON t.reference_key=l.reference_id"
        )
    finally:
        db.create_function("fingerprint_hash_bytes", 1, None)
    if (
        db.execute("SELECT count(*) FROM landmarks").fetchone()[0]
        != db.execute("SELECT count(*) FROM landmarks_new").fetchone()[0]
    ):
        raise RuntimeError("Landmark migration would lose rows; refusing to upgrade")
    db.execute("DROP TABLE landmarks")
    db.execute("DROP TABLE reference_tracks")
    db.execute("ALTER TABLE reference_tracks_new RENAME TO reference_tracks")
    db.execute("ALTER TABLE landmarks_new RENAME TO landmarks")
    db.execute("CREATE INDEX references_ready ON reference_tracks(publication_ready,id)")


def _hash_bytes(digest: str) -> bytes:
    if not isinstance(digest, str) or len(digest) != 20:
        raise ValueError("Invalid stored landmark hash")
    result = bytes.fromhex(digest)
    if len(result) != 10:
        raise ValueError("Invalid stored landmark hash")
    return result


def _migrate_compact_documents(db: sqlite3.Connection) -> None:
    for row in db.execute("SELECT id,document FROM submissions"):
        try:
            document = json.loads(row[1])
            decode_submission(row[1])
            compact = encode_document(document)
            encoded = json.dumps(compact, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, UnicodeError) as err:
            LOGGER.warning(
                "Retaining unconvertible fingerprint submission %s: %s", row[0], type(err).__name__
            )
            continue
        db.execute("UPDATE submissions SET document=? WHERE id=?", (encoded, row[0]))
