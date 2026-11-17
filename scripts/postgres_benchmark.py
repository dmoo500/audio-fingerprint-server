"""Compare an isolated PostgreSQL prototype with a fingerprint SQLite backup."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import statistics
import struct
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

from fingerprint_server.schemas import decode_submission

TABLES = {
    "submissions": """id text PRIMARY KEY, document text NOT NULL, state text NOT NULL,
        attempts integer NOT NULL, retry_at double precision NOT NULL, error text,
        reference_id text, created_at double precision NOT NULL, pending_rechecks integer NOT NULL,
        pending_due_at double precision NOT NULL, pending_check integer NOT NULL,
        enqueued_at double precision NOT NULL, background_kind integer NOT NULL""",
    "reference_tracks": """id bigint PRIMARY KEY, reference_key text NOT NULL UNIQUE,
        metadata text NOT NULL, created_at double precision NOT NULL,
        publication_ready integer NOT NULL, verification_version integer NOT NULL,
        recording_key text""",
    "landmarks": """hash bytea NOT NULL, reference_id bigint NOT NULL, "offset" integer NOT NULL""",
    "enrichment_cache": (
        "isrc text PRIMARY KEY, document text NOT NULL, expires_at double precision NOT NULL"
    ),
    "service_state": "key text PRIMARY KEY, value text NOT NULL",
}

INDEXES = (
    "CREATE INDEX submissions_pending ON submissions(state, retry_at, created_at)",
    "CREATE INDEX submissions_pending_due ON submissions(state, pending_due_at, created_at)",
    "CREATE INDEX submissions_fifo ON submissions(state, enqueued_at)",
    "CREATE INDEX references_ready ON reference_tracks(publication_ready, id)",
    "CREATE INDEX references_recording ON reference_tracks(recording_key, id)",
    'ALTER TABLE landmarks ADD PRIMARY KEY(hash, reference_id, "offset")',
    "CREATE INDEX landmarks_reference ON landmarks(reference_id)",
    "ALTER TABLE landmarks ADD FOREIGN KEY(reference_id) REFERENCES reference_tracks(id)",
)


def docker(*args: str, env: dict[str, str] | None = None) -> str:
    """Run a Docker command without including credentials in arguments."""
    result = subprocess.run(["docker", *args], check=True, capture_output=True, text=True, env=env)
    return result.stdout.strip()


def snapshot(source: Path, destination: Path) -> None:
    """Copy the offline backup and add the current SQLite landmark index."""
    uri = f"file:{quote(str(source.resolve()))}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as original, sqlite3.connect(destination) as copy:
        version = original.execute("PRAGMA user_version").fetchone()[0]
        if version not in (7, 8):
            raise ValueError(f"Expected schema 7 or 8 backup, got {version}")
        original.backup(copy)
        if version == 7:
            copy.execute(
                "CREATE INDEX IF NOT EXISTS landmarks_reference ON landmarks(reference_id)"
            )
            copy.execute("PRAGMA user_version=8")
        copy.commit()


def copy_tables(source: sqlite3.Connection, target: psycopg.Connection) -> dict[str, int]:
    """Copy data without changing it and build equivalent indexes."""
    counts = {}
    with target.cursor() as cur:
        for name, definition in TABLES.items():
            cur.execute(
                sql.SQL("CREATE TABLE {} ({})").format(sql.Identifier(name), sql.SQL(definition))
            )
        for name in TABLES:
            columns = [row[1] for row in source.execute(f'PRAGMA table_info("{name}")')]
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(name), sql.SQL(",").join(map(sql.Identifier, columns))
            )
            count = 0
            if name == "landmarks":
                with cur.copy(statement + sql.SQL(" WITH (FORMAT binary)")) as writer:
                    writer.write(b"PGCOPY\n\xff\r\n\x00" + struct.pack("!ii", 0, 0))
                    buffer = bytearray()
                    pack = struct.Struct("!hi10siqii").pack
                    for digest, reference_id, offset in source.execute(
                        'SELECT hash,reference_id,"offset" FROM landmarks'
                    ):
                        buffer.extend(pack(3, 10, digest, 8, reference_id, 4, offset))
                        count += 1
                        if count % 10000 == 0:
                            writer.write(buffer)
                            buffer.clear()
                    if buffer:
                        writer.write(buffer)
                    writer.write(struct.pack("!h", -1))
            else:
                with cur.copy(statement) as writer:
                    for row in source.execute(f'SELECT * FROM "{name}"'):
                        writer.write_row(row)
                        count += 1
            counts[name] = count
            print(f"Copied {name}: {count}", flush=True)
        for statement in INDEXES:
            cur.execute(statement)
        target.commit()
        cur.execute("ANALYZE")
        target.commit()
    return counts


def samples(source: sqlite3.Connection, count: int) -> list[list[tuple[bytes, int]]]:
    """Choose deterministic approximately six-second samples from published submissions."""
    result = []
    seen = set()
    rows = source.execute(
        "SELECT t.id,s.document FROM submissions s JOIN reference_tracks t "
        "ON s.reference_id=t.reference_key WHERE t.publication_ready=1 ORDER BY t.id,s.id"
    )
    for reference_id, document in rows:
        if reference_id in seen:
            continue
        submission = decode_submission(document)
        pairs = sorted(submission.fingerprints, key=lambda pair: pair[1])
        first = pairs[0][1]
        # 129 hops span approximately six seconds at the current hop size and sample rate.
        query = [(bytes.fromhex(h), offset - first) for h, offset in pairs if offset < first + 129]
        if len(query) >= 20:
            result.append(query)
            seen.add(reference_id)
        if len(result) == count:
            return result
    raise ValueError(f"Only {len(result)} suitable published samples found; need {count}")


def measure(
    source: sqlite3.Connection,
    target: psycopg.Connection,
    query: list[tuple[bytes, int]],
    repetitions: int,
) -> tuple[list[float], list[float], int]:
    """Compare exact matching candidate multisets and warm query times."""
    source.execute("DROP TABLE IF EXISTS temp.query")
    source.execute("CREATE TEMP TABLE query(hash BLOB, offset INTEGER, idx INTEGER)")
    source.executemany(
        "INSERT INTO query VALUES(?,?,?)",
        ((h, offset, idx) for idx, (h, offset) in enumerate(query)),
    )
    with target.cursor() as cur:
        cur.execute(
            'CREATE TEMP TABLE IF NOT EXISTS query(hash bytea, "offset" integer, idx integer)'
        )
        cur.execute("TRUNCATE query")
        with cur.copy('COPY query(hash, "offset", idx) FROM STDIN') as writer:
            for idx, (digest, offset) in enumerate(query):
                writer.write_row((digest, offset, idx))
        cur.execute("ANALYZE query")
        sqlite_query = (
            "SELECT t.reference_key,t.recording_key,l.offset-q.offset,q.idx,q.offset "
            "FROM query q CROSS JOIN landmarks l ON l.hash=q.hash "
            "JOIN reference_tracks t ON t.id=l.reference_id "
            "WHERE t.publication_ready=1 LIMIT 100001"
        )
        pg_query = (
            'SELECT t.reference_key,t.recording_key,l."offset"-q."offset",q.idx,q."offset" '
            "FROM query q JOIN landmarks l ON l.hash=q.hash "
            "JOIN reference_tracks t ON t.id=l.reference_id "
            "WHERE t.publication_ready=1 LIMIT 100001"
        )
        times_sqlite, times_pg = [], []
        expected = None
        for iteration in range(repetitions + 1):
            start = time.perf_counter()
            sqlite_rows = source.execute(sqlite_query).fetchall()
            sqlite_time = time.perf_counter() - start
            start = time.perf_counter()
            cur.execute(pg_query)
            pg_rows = cur.fetchall()
            pg_time = time.perf_counter() - start
            if len(sqlite_rows) > 100000 or len(pg_rows) > 100000:
                raise ValueError("Sample exceeds the production candidate limit")
            if sorted(sqlite_rows, key=repr) != sorted(pg_rows, key=repr):
                raise ValueError("PostgreSQL and SQLite candidate sets differ")
            expected = len(sqlite_rows)
            if iteration:
                times_sqlite.append(sqlite_time)
                times_pg.append(pg_time)
        return times_sqlite, times_pg, expected or 0


def main() -> None:
    """Run the experiment against an offline backup and a disposable local container."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.samples < 1 or args.repetitions < 1:
        parser.error("samples and repetitions must be positive")
    name = f"fingerprint-postgres-benchmark-{uuid.uuid4().hex[:12]}"
    password = secrets.token_urlsafe(32)
    env = os.environ | {"POSTGRES_PASSWORD": password}
    started = False
    try:
        docker(
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_PASSWORD",
            "postgres:17",
            env=env,
        )
        started = True
        port = docker("port", name, "5432/tcp").rsplit(":", 1)[1]
        deadline = time.monotonic() + 90
        while True:
            try:
                target = psycopg.connect(
                    host="127.0.0.1",
                    port=port,
                    user="postgres",
                    password=password,
                    dbname="postgres",
                    connect_timeout=3,
                )
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        with tempfile.TemporaryDirectory(prefix="fingerprint-postgres-") as temp, target:
            path = Path(temp) / "snapshot.db"
            print("Creating isolated SQLite snapshot and reference index", flush=True)
            snapshot(args.backup, path)
            with sqlite3.connect(path) as source:
                selected = samples(source, args.samples)
                start = time.monotonic()
                counts = copy_tables(source, target)
                import_seconds = time.monotonic() - start
                with target.cursor() as cur:
                    for table, count in counts.items():
                        cur.execute(
                            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                        )
                        if cur.fetchone()[0] != count:
                            raise ValueError(f"Row count mismatch for {table}")
                    cur.execute("SELECT pg_database_size(current_database())")
                    pg_size = cur.fetchone()[0]
                    cur.execute("SELECT pg_total_relation_size('landmarks')")
                    pg_landmarks = cur.fetchone()[0]
                sqlite_pages = source.execute(
                    "SELECT page_count,freelist_count,page_size "
                    "FROM pragma_page_count,pragma_freelist_count,pragma_page_size"
                ).fetchone()
                sqlite_landmarks = source.execute(
                    "SELECT sum(pgsize) FROM dbstat WHERE name IN "
                    "('landmarks','landmarks_reference')"
                ).fetchone()[0]
                sqlite_times, pg_times, candidates = [], [], []
                for index, query in enumerate(selected):
                    a, b, count = measure(source, target, query, args.repetitions)
                    sqlite_times.extend(a)
                    pg_times.extend(b)
                    candidates.append(count)
                    print(f"Validated sample {index + 1}: {count} candidates", flush=True)
                print(
                    json.dumps(
                        {
                            "source_schema": 8,
                            "rows": counts,
                            "sqlite_occupied_bytes": (sqlite_pages[0] - sqlite_pages[1])
                            * sqlite_pages[2],
                            "sqlite_landmarks_bytes": sqlite_landmarks,
                            "postgres_database_bytes": pg_size,
                            "postgres_landmarks_bytes": pg_landmarks,
                            "import_seconds": round(import_seconds, 2),
                            "samples": len(selected),
                            "repetitions": args.repetitions,
                            "candidate_counts": candidates,
                            "sqlite_median_ms": round(statistics.median(sqlite_times) * 1000, 2),
                            "postgres_median_ms": round(statistics.median(pg_times) * 1000, 2),
                        },
                        indent=2,
                    ),
                    flush=True,
                )
    finally:
        if started:
            docker("stop", name)


if __name__ == "__main__":
    main()
