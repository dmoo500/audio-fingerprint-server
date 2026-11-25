"""Verify and convert an offline PostgreSQL schema between landmark layouts 1 and 2."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging

import psycopg
from migrate_postgres_to_sqlite import offline_snapshot, validate_postgres

from fingerprint_server.database.postgres.postings import (
    CREATE,
    LEGACY_POSTINGS_VERSION,
    POSTINGS_VERSION,
    VIEW,
)
from fingerprint_server.database.postgres.schema import FORMAT_VERSION

LOGGER = logging.getLogger(__name__)


def checksum(db: psycopg.Connection, table: str) -> str:
    """Hash the full canonical binary landmark stream."""
    if table not in ("landmarks", "landmark_rows"):
        raise ValueError("Unexpected landmark source")
    digest = hashlib.sha256()
    with db.cursor().copy(
        f'COPY (SELECT hash,reference_id,"offset" FROM {table} '
        'ORDER BY hash,reference_id,"offset") TO STDOUT (FORMAT binary)'
    ) as copy:
        for chunk in copy:
            digest.update(chunk)
    return digest.hexdigest()


def migrate(db: psycopg.Connection, schema: str, target: int) -> dict:
    """Atomically migrate with service exclusion and full landmark verification."""
    if target not in (1, 2):
        raise ValueError("Target must be 1 or 2")
    with offline_snapshot(db, schema, read_only=False):
        validate_postgres(db, schema)
        version = db.execute("SELECT format_version FROM migration_info").fetchone()
        assert version is not None
        layout = 2 if version[0] in (LEGACY_POSTINGS_VERSION, POSTINGS_VERSION) else 1
        if layout == target:
            return {"version": target, "changed": False}
        before = checksum(db, "landmarks")
        LOGGER.info("Verified source checksum; building layout %s", target)
        if target == 2:
            db.execute("ALTER TABLE landmarks RENAME TO landmark_rows")
            db.execute(CREATE)
            db.execute("ALTER TABLE landmark_postings DISABLE TRIGGER postings_reference_check")
            db.execute("""
                INSERT INTO landmark_postings
                SELECT hash,reference_id/32,"offset"/1024,
                       array_agg(reference_id ORDER BY reference_id,"offset"),
                       array_agg("offset" ORDER BY reference_id,"offset")
                FROM landmark_rows GROUP BY hash,reference_id/32,"offset"/1024
            """)
            db.execute("ALTER TABLE landmark_postings ENABLE TRIGGER postings_reference_check")
            db.execute(VIEW)
            if checksum(db, "landmarks") != before:
                raise ValueError("Postings checksum mismatch; migration rolled back")
            db.execute("DROP TABLE landmark_rows")
        else:
            db.execute("""
                CREATE TABLE landmark_rows (
                    hash bytea NOT NULL CHECK(octet_length(hash)=10),
                    reference_id bigint NOT NULL REFERENCES reference_tracks(id),
                    "offset" integer NOT NULL,
                    PRIMARY KEY(hash,reference_id,"offset")
                )
            """)
            db.execute("INSERT INTO landmark_rows SELECT * FROM landmarks")
            if checksum(db, "landmark_rows") != before:
                raise ValueError("Row-layout checksum mismatch; migration rolled back")
            db.execute("DROP VIEW landmarks")
            db.execute("DROP TRIGGER postings_reference_delete ON reference_tracks")
            db.execute("DROP TABLE landmark_postings")
            db.execute("DROP FUNCTION check_posting_references()")
            db.execute("DROP FUNCTION protect_posting_reference()")
            db.execute("DROP FUNCTION valid_posting(bigint,integer,bigint[],integer[])")
            db.execute("ALTER TABLE landmark_rows RENAME TO landmarks")
            db.execute("CREATE INDEX landmarks_reference ON landmarks(reference_id)")
        format_version = target + (FORMAT_VERSION - 1 if version[0] >= FORMAT_VERSION else 0)
        db.execute("UPDATE migration_info SET format_version=%s", (format_version,))
        validate_postgres(db, schema)
        return {"version": target, "changed": True, "landmarks_sha256": before}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--target", required=True, type=int, choices=(1, 2))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    with psycopg.connect("") as connection:
        print(json.dumps(migrate(connection, args.schema, args.target)))
