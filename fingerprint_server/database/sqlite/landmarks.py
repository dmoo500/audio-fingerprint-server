"""Batch insertion and safe removal of SQLite landmark postings."""

import logging
import sqlite3
from collections.abc import Iterable

LANDMARK_SCHEMA = """
CREATE TABLE landmark_hashes (
    id INTEGER PRIMARY KEY,
    hash BLOB NOT NULL UNIQUE CHECK(typeof(hash)='blob' AND length(hash)=10)
);
CREATE TABLE landmark_postings (
    hash_id INTEGER NOT NULL REFERENCES landmark_hashes(id),
    reference_id INTEGER NOT NULL REFERENCES reference_tracks(id),
    offset INTEGER NOT NULL, PRIMARY KEY(hash_id, reference_id, offset)
) WITHOUT ROWID;
CREATE INDEX landmark_postings_reference ON landmark_postings(reference_id);
CREATE VIEW landmarks AS
    SELECT h.hash,p.reference_id,p.offset
    FROM landmark_hashes h CROSS JOIN landmark_postings p ON p.hash_id=h.id;
"""

LOGGER = logging.getLogger(__name__)


def validate_landmark_dictionary(db: sqlite3.Connection) -> None:
    """Reject an incomplete or unrecognized dictionary layout."""
    expected_columns = {
        "landmark_hashes": {"id": "INTEGER", "hash": "BLOB"},
        "landmark_postings": {
            "hash_id": "INTEGER",
            "reference_id": "INTEGER",
            "offset": "INTEGER",
        },
    }
    columns = {}
    for table, expected in expected_columns.items():
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
        columns[table] = {row[1]: row for row in rows}
        if {row[1]: row[2].upper() for row in rows} != expected:
            raise RuntimeError(f"Unrecognized fingerprint database columns in {table}")
    if columns["landmark_hashes"]["id"][5] != 1 or [
        columns["landmark_postings"][name][5] for name in ("hash_id", "reference_id", "offset")
    ] != [1, 2, 3]:
        raise RuntimeError("Unrecognized fingerprint landmark primary keys")
    unique_hash = any(
        index[2]
        and not index[4]
        and [
            row[0]
            for row in db.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],)
            )
        ]
        == ["hash"]
        for index in db.execute("PRAGMA index_list(landmark_hashes)")
    )
    reference_index = any(
        not index[4]
        and [
            row[0]
            for row in db.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],)
            )
        ][:1]
        == ["reference_id"]
        for index in db.execute("PRAGMA index_list(landmark_postings)")
    )
    foreign_keys = {
        (row[2], row[3], row[4]) for row in db.execute("PRAGMA foreign_key_list(landmark_postings)")
    }
    if (
        not unique_hash
        or not reference_index
        or foreign_keys
        != {
            ("landmark_hashes", "hash_id", "id"),
            ("reference_tracks", "reference_id", "id"),
        }
    ):
        raise RuntimeError("Incomplete fingerprint landmark indexes or foreign keys")
    view = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='landmarks'"
    ).fetchone()
    if view is None or {row[1] for row in db.execute("PRAGMA table_info(landmarks)")} != {
        "hash",
        "reference_id",
        "offset",
    }:
        raise RuntimeError("Unrecognized fingerprint landmark view; refusing to migrate")


def migrate_landmark_dictionary(db: sqlite3.Connection) -> None:
    """Convert all existing landmarks in the caller's transaction."""
    LOGGER.info("Building the SQLite landmark hash dictionary")
    db.execute(
        "CREATE TABLE landmark_hashes (id INTEGER PRIMARY KEY,"
        "hash BLOB NOT NULL UNIQUE CHECK(typeof(hash)='blob' AND length(hash)=10))"
    )
    db.execute(
        "INSERT INTO landmark_hashes(hash) SELECT DISTINCT hash FROM landmarks ORDER BY hash"
    )
    db.execute(
        "CREATE TABLE landmark_postings ("
        "hash_id INTEGER NOT NULL REFERENCES landmark_hashes(id),"
        "reference_id INTEGER NOT NULL REFERENCES reference_tracks(id),"
        "offset INTEGER NOT NULL,PRIMARY KEY(hash_id,reference_id,offset)) WITHOUT ROWID"
    )
    LOGGER.info("Converting SQLite landmark postings")
    db.execute(
        "INSERT INTO landmark_postings "
        "SELECT dense_rank() OVER (ORDER BY hash),reference_id,offset FROM landmarks"
    )
    if (
        db.execute("SELECT count(*) FROM landmarks").fetchone()[0]
        != db.execute("SELECT count(*) FROM landmark_postings").fetchone()[0]
    ):
        raise RuntimeError("Landmark migration would lose rows; refusing to upgrade")
    LOGGER.info("Building the SQLite landmark reference index")
    db.execute("CREATE INDEX landmark_postings_reference ON landmark_postings(reference_id)")
    db.execute("DROP TABLE landmarks")
    db.execute(
        "CREATE VIEW landmarks AS SELECT h.hash,p.reference_id,p.offset "
        "FROM landmark_hashes h CROSS JOIN landmark_postings p ON p.hash_id=h.id"
    )


def insert_landmarks(db: sqlite3.Connection, rows: Iterable[tuple[bytes, int, int]]) -> None:
    """Insert complete landmarks in the caller's transaction."""
    db.execute(
        "CREATE TEMP TABLE incoming_landmarks ("
        "hash BLOB NOT NULL,reference_id INTEGER NOT NULL,offset INTEGER NOT NULL,"
        "PRIMARY KEY(hash,reference_id,offset)) WITHOUT ROWID"
    )
    try:
        db.executemany("INSERT INTO incoming_landmarks VALUES(?,?,?)", rows)
        db.execute(
            "INSERT INTO landmark_hashes(hash) "
            "SELECT DISTINCT hash FROM incoming_landmarks WHERE true "
            "ON CONFLICT(hash) DO NOTHING"
        )
        db.execute(
            "INSERT INTO landmark_postings(hash_id,reference_id,offset) "
            "SELECT h.id,l.reference_id,l.offset FROM incoming_landmarks l "
            "CROSS JOIN landmark_hashes h ON h.hash=l.hash"
        )
    finally:
        db.execute("DROP TABLE incoming_landmarks")


def delete_reference_landmarks(db: sqlite3.Connection, reference_id: int) -> int:
    """Delete a reference's postings and dictionary entries no longer used."""
    db.execute("CREATE TEMP TABLE removed_hashes(id INTEGER PRIMARY KEY)")
    try:
        db.execute(
            "INSERT INTO removed_hashes SELECT DISTINCT hash_id FROM landmark_postings "
            "WHERE reference_id=?",
            (reference_id,),
        )
        count = db.execute(
            "DELETE FROM landmark_postings WHERE reference_id=?", (reference_id,)
        ).rowcount
        db.execute(
            "DELETE FROM landmark_hashes WHERE id IN (SELECT id FROM removed_hashes) "
            "AND NOT EXISTS (SELECT 1 FROM landmark_postings p WHERE p.hash_id=landmark_hashes.id)"
        )
        return count
    finally:
        db.execute("DROP TABLE removed_hashes")
