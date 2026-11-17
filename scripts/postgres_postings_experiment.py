"""Compare lossless landmark posting layouts in a disposable PostgreSQL instance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import statistics
import struct
import time
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import psycopg
from postgres_benchmark import docker

LAYOUTS = {
    "baseline": 'SELECT hash,reference_id,"offset" FROM landmarks',
    "by_reference": ("SELECT hash,reference_id,unnest(offsets) AS offset FROM by_reference"),
    "by_hash": (
        "SELECT hash,p.reference_id,p.offset FROM by_hash "
        'CROSS JOIN LATERAL unnest(reference_ids,offsets) p(reference_id,"offset")'
    ),
}


def checksum(db: psycopg.Connection, query: str) -> dict:
    """Compare every decoded landmark independently of its physical layout."""
    digest = hashlib.sha256()
    count = 0
    with db.cursor(name="checksum") as cursor:
        cursor.execute(f'SELECT * FROM ({query}) decoded ORDER BY hash,reference_id,"offset"')
        while rows := cursor.fetchmany(10000):
            for fingerprint, reference_id, offset in rows:
                digest.update(struct.pack("!10sqi", fingerprint, reference_id, offset))
            count += len(rows)
    return {"rows": count, "sha256": digest.hexdigest()}


def experiment(source: Path, references: int, report: Path) -> None:
    """Benchmark a bounded offline sample without touching a running database."""
    if not source.is_file():
        raise FileNotFoundError(source)
    wal = source.with_name(source.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("Use an offline backup without a nonempty WAL")
    name = f"fingerprint-postings-{uuid.uuid4().hex[:12]}"
    password = secrets.token_urlsafe(32)
    result: dict = {"status": "running", "scope": "bounded reference subset; not full catalogue"}
    docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--cpus=2",
        "--memory=4g",
        "-p",
        "127.0.0.1::5432",
        "-e",
        "POSTGRES_PASSWORD",
        "postgres:17",
        env=os.environ | {"POSTGRES_PASSWORD": password},
    )
    try:
        port = docker("port", name, "5432/tcp").rsplit(":", 1)[1]
        for attempt in range(60):
            try:
                db = psycopg.connect(
                    host="127.0.0.1",
                    port=port,
                    user="postgres",
                    password=password,
                    dbname="postgres",
                    connect_timeout=3,
                )
                break
            except psycopg.OperationalError:
                if attempt == 59:
                    raise
                time.sleep(1)
        uri = f"file:{quote(str(source.resolve()))}?mode=ro"
        with db, closing(sqlite3.connect(uri, uri=True)) as sqlite:
            ids = [
                row[0]
                for row in sqlite.execute(
                    "SELECT id FROM reference_tracks WHERE publication_ready=1 ORDER BY id LIMIT ?",
                    (references,),
                )
            ]
            if not ids:
                raise ValueError("No searchable references")
            result["references"] = len(ids)
            result["documents"] = {}
            for compression in ("pglz", "lz4"):
                db.execute(
                    f"CREATE TABLE documents_{compression}(id text PRIMARY KEY,"
                    f"document text COMPRESSION {compression} NOT NULL)"
                )
                started = time.perf_counter()
                with db.cursor().copy(f"COPY documents_{compression} FROM STDIN") as writer:
                    for row in sqlite.execute(
                        "SELECT id,document FROM submissions ORDER BY rowid LIMIT ?", (references,)
                    ):
                        writer.write_row(row)
                result["documents"][compression] = {
                    "insert_seconds": time.perf_counter() - started,
                }
            db.execute(
                "CREATE TABLE landmarks(hash bytea NOT NULL,reference_id bigint NOT NULL,"
                '"offset" integer NOT NULL)'
            )
            queries: list[list[tuple[bytes, int]]] = []
            with db.cursor().copy("COPY landmarks FROM STDIN") as writer:
                for reference_id in ids:
                    sample: list[tuple[bytes, int]] = []
                    for row in sqlite.execute(
                        'SELECT hash,reference_id,"offset" FROM landmarks '
                        'WHERE reference_id=? ORDER BY "offset",hash',
                        (reference_id,),
                    ):
                        writer.write_row(row)
                        if len(sample) < 400:
                            sample.append((row[0], row[2]))
                    if sample and len(queries) < 10:
                        queries.append(sample)
            db.execute('ALTER TABLE landmarks ADD PRIMARY KEY(hash,reference_id,"offset")')
            db.execute("CREATE INDEX landmarks_reference ON landmarks(reference_id)")
            print("Building grouped layouts", flush=True)
            db.execute(
                "CREATE TABLE by_reference AS SELECT hash,reference_id,"
                'array_agg("offset" ORDER BY "offset") AS offsets FROM landmarks '
                "GROUP BY hash,reference_id"
            )
            db.execute("ALTER TABLE by_reference ADD PRIMARY KEY(hash,reference_id)")
            db.execute("CREATE INDEX by_reference_reference ON by_reference(reference_id)")
            db.execute(
                "CREATE TABLE by_hash AS SELECT hash,"
                'array_agg(reference_id ORDER BY reference_id,"offset") AS reference_ids,'
                'array_agg("offset" ORDER BY reference_id,"offset") AS offsets '
                "FROM landmarks GROUP BY hash"
            )
            db.execute("ALTER TABLE by_hash ADD PRIMARY KEY(hash)")
            db.execute("CREATE INDEX by_hash_references ON by_hash USING gin(reference_ids)")
            db.commit()
            db.autocommit = True
            db.execute("VACUUM (ANALYZE)")
            db.autocommit = False
            result["layouts"] = {}
            expected_documents = sqlite.execute(
                "SELECT document FROM (SELECT id,document FROM submissions "
                "ORDER BY rowid LIMIT ?) ORDER BY id",
                (references,),
            ).fetchall()
            for compression in ("pglz", "lz4"):
                started = time.perf_counter()
                documents = db.execute(
                    f"SELECT document FROM documents_{compression} ORDER BY id"
                ).fetchall()
                read_seconds = time.perf_counter() - started
                if documents != expected_documents:
                    raise ValueError(f"Document mismatch: {compression}")
                document_size = db.execute(
                    "SELECT pg_total_relation_size(%s::regclass)",
                    (f"documents_{compression}",),
                ).fetchone()
                assert document_size is not None
                result["documents"][compression] |= {
                    "read_seconds": read_seconds,
                    "rows": len(documents),
                    "total_bytes": document_size[0],
                }
            for label, decoded in LAYOUTS.items():
                table = "landmarks" if label == "baseline" else label
                size = db.execute(
                    "SELECT pg_relation_size(%s::regclass),pg_indexes_size(%s::regclass),"
                    "pg_total_relation_size(%s::regclass)",
                    (table, table, table),
                ).fetchone()
                assert size is not None
                content = checksum(db, decoded)
                result["layouts"][label] = (
                    dict(zip(("heap_bytes", "index_bytes", "total_bytes"), size, strict=True))
                    | content
                )
                if label != "baseline":
                    expected = result["layouts"]["baseline"]
                    if any(content[key] != expected[key] for key in ("rows", "sha256")):
                        raise ValueError(f"Lossy layout: {label}")
                print(label, result["layouts"][label], flush=True)
            statements = {
                "baseline": (
                    'SELECT l.reference_id,l."offset"-q."offset",q.idx FROM query q '
                    "JOIN landmarks l ON l.hash=q.hash LIMIT 100001"
                ),
                "by_reference": (
                    'SELECT l.reference_id,p.offset-q."offset",q.idx FROM query q '
                    "JOIN by_reference l ON l.hash=q.hash "
                    'CROSS JOIN LATERAL unnest(l.offsets) p("offset") LIMIT 100001'
                ),
                "by_hash": (
                    'SELECT p.reference_id,p.offset-q."offset",q.idx FROM query q '
                    "JOIN by_hash l ON l.hash=q.hash CROSS JOIN LATERAL "
                    'unnest(l.reference_ids,l.offsets) p(reference_id,"offset") LIMIT 100001'
                ),
            }
            db.execute('CREATE TEMP TABLE query(hash bytea,"offset" integer,idx integer)')
            timings: dict[str, list[float]] = {label: [] for label in LAYOUTS}
            broad = 0
            for pairs in queries:
                db.execute("TRUNCATE query")
                with db.cursor().copy("COPY query FROM STDIN") as writer:
                    for index, (fingerprint, offset) in enumerate(pairs):
                        writer.write_row((fingerprint, offset, index))
                db.execute("ANALYZE query")
                expected = None
                for label, statement in statements.items():
                    started = time.perf_counter()
                    rows = db.execute(statement).fetchall()
                    timings[label].append((time.perf_counter() - started) * 1000)
                    outcome = "too_broad" if len(rows) > 100000 else Counter(rows)
                    if expected is None:
                        expected = outcome
                        broad += outcome == "too_broad"
                    elif outcome != expected:
                        raise ValueError(f"Candidate mismatch: {label}")
            plans = {}
            for label, statement in statements.items():
                plan = db.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement).fetchone()
                assert plan is not None
                plans[label] = plan[0]
            result["queries"] = {
                "samples": len(queries),
                "candidate_limit_errors": broad,
                "median_ms": {key: statistics.median(values) for key, values in timings.items()},
                "plans": plans,
            }
            # Deleting one reference must leave the exact same decoded collection.
            removed = ids[0]
            started = time.perf_counter()
            db.execute("DELETE FROM landmarks WHERE reference_id=%s", (removed,))
            db.execute("DELETE FROM by_reference WHERE reference_id=%s", (removed,))
            db.execute(
                "UPDATE by_hash SET reference_ids=remaining.reference_ids,"
                "offsets=remaining.offsets FROM "
                "(SELECT hash,array_agg(p.reference_id ORDER BY p.reference_id,p.offset) "
                "AS reference_ids,array_agg(p.offset ORDER BY p.reference_id,p.offset) AS offsets "
                "FROM by_hash CROSS JOIN LATERAL unnest(reference_ids,offsets) "
                'p(reference_id,"offset") WHERE reference_ids @> ARRAY[%s]::bigint[] '
                "AND p.reference_id!=%s GROUP BY hash) remaining "
                "WHERE by_hash.hash=remaining.hash",
                (removed, removed),
            )
            db.execute("DELETE FROM by_hash WHERE reference_ids @> ARRAY[%s]::bigint[]", (removed,))
            expected = checksum(db, LAYOUTS["baseline"])
            for label in ("by_reference", "by_hash"):
                if checksum(db, LAYOUTS[label]) != expected:
                    raise ValueError(f"Deletion mismatch: {label}")
            result["deletion_all_layouts_seconds"] = time.perf_counter() - started
            db.rollback()
            result["status"] = "passed"
    finally:
        docker("stop", name)
        report.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source", type=Path, help="Offline SQLite backup, never the active database"
    )
    parser.add_argument("--references", type=int, default=40)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.references <= 1000:
        parser.error("--references must be between 1 and 1000")
    experiment(args.source, args.references, args.report)
