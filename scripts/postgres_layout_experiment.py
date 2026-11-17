"""Investigate PostgreSQL landmark layouts using an isolated SQLite backup."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import statistics
import struct
import tempfile
import time
import uuid
from pathlib import Path

import psycopg
from postgres_benchmark import copy_tables, docker, samples, snapshot

QUERY = (
    'SELECT t.reference_key,t.recording_key,l."offset"-q."offset",q.idx,q."offset" '
    "FROM query q JOIN landmarks l ON l.hash=q.hash "
    "JOIN reference_tracks t ON t.id=l.reference_id "
    "WHERE t.publication_ready=1 LIMIT 100001"
)
PACKED_QUERY = (
    'SELECT t.reference_key,t.recording_key,l."offset"-q."offset",q.idx,q."offset" '
    "FROM packed_query q JOIN packed_landmarks l "
    "ON l.hash_high=q.hash_high AND l.hash_low=q.hash_low "
    "JOIN reference_tracks t ON t.id=l.reference_id "
    "WHERE t.publication_ready=1 LIMIT 100001"
)
SMALL_QUERY = QUERY.replace("JOIN landmarks l", "JOIN small_landmarks l")


def split_hash(digest: bytes) -> tuple[int, int]:
    """Split a 10-byte hash into reversible signed integers."""
    if len(digest) != 10:
        raise ValueError("Fingerprint hash must be exactly 10 bytes")
    return struct.unpack("!qh", digest)


def copy_packed(source: sqlite3.Connection, target: psycopg.Connection) -> None:
    """Create an experimental integer-key table with equivalent indexes."""
    with target.cursor() as cur:
        cur.execute(
            "CREATE TABLE packed_landmarks (hash_high bigint NOT NULL, hash_low smallint NOT NULL,"
            'reference_id bigint NOT NULL, "offset" integer NOT NULL)'
        )
        with cur.copy("COPY packed_landmarks FROM STDIN WITH (FORMAT binary)") as writer:
            writer.write(b"PGCOPY\n\xff\r\n\x00" + struct.pack("!ii", 0, 0))
            buf = bytearray()
            pack = struct.Struct("!hiqihiqii").pack
            for count, (digest, reference_id, offset) in enumerate(
                source.execute('SELECT hash,reference_id,"offset" FROM landmarks'), 1
            ):
                high, low = split_hash(digest)
                buf.extend(pack(4, 8, high, 2, low, 8, reference_id, 4, offset))
                if count % 10000 == 0:
                    writer.write(buf)
                    buf.clear()
            if buf:
                writer.write(buf)
            writer.write(struct.pack("!h", -1))
        cur.execute(
            'ALTER TABLE packed_landmarks ADD PRIMARY KEY(hash_high,hash_low,reference_id,"offset")'
        )
        cur.execute("CREATE INDEX packed_landmarks_reference ON packed_landmarks(reference_id)")
        cur.execute(
            "ALTER TABLE packed_landmarks ADD FOREIGN KEY(reference_id) REFERENCES reference_tracks"
        )
    target.commit()


def copy_small(source: sqlite3.Connection, target: psycopg.Connection) -> None:
    """Create an experimental table with 32-bit internal reference IDs."""
    with target.cursor() as cur:
        cur.execute(
            "CREATE TABLE small_landmarks (hash bytea NOT NULL,"
            'reference_id integer NOT NULL, "offset" integer NOT NULL)'
        )
        with cur.copy("COPY small_landmarks FROM STDIN WITH (FORMAT binary)") as writer:
            writer.write(b"PGCOPY\n\xff\r\n\x00" + struct.pack("!ii", 0, 0))
            buf = bytearray()
            pack = struct.Struct("!hi10siiii").pack
            for count, (digest, reference_id, offset) in enumerate(
                source.execute('SELECT hash,reference_id,"offset" FROM landmarks'), 1
            ):
                if not -(2**31) <= reference_id < 2**31:
                    raise ValueError("Reference ID exceeds the experimental 32-bit range")
                buf.extend(pack(3, 10, digest, 4, reference_id, 4, offset))
                if count % 10000 == 0:
                    writer.write(buf)
                    buf.clear()
            if buf:
                writer.write(buf)
            writer.write(struct.pack("!h", -1))
        cur.execute('ALTER TABLE small_landmarks ADD PRIMARY KEY(hash,reference_id,"offset")')
        cur.execute("CREATE INDEX small_landmarks_reference ON small_landmarks(reference_id)")
        cur.execute(
            "ALTER TABLE small_landmarks ADD FOREIGN KEY(reference_id) REFERENCES reference_tracks"
        )
    target.commit()


def vacuum(target: psycopg.Connection, table: str) -> None:
    """Set visibility-map bits before measuring index-only scans."""
    target.commit()
    target.autocommit = True
    try:
        with target.cursor() as cur:
            if table not in ("landmarks", "packed_landmarks", "small_landmarks"):
                raise ValueError("Unexpected table")
            cur.execute(f"VACUUM (ANALYZE) {table}")
    finally:
        target.autocommit = False


def prepare_query(target: psycopg.Connection, query: list[tuple[bytes, int]]) -> None:
    """Populate equivalent temporary tables for a sample."""
    with target.cursor() as cur:
        cur.execute(
            'CREATE TEMP TABLE IF NOT EXISTS query(hash bytea, "offset" integer, idx integer)'
        )
        cur.execute(
            "CREATE TEMP TABLE IF NOT EXISTS packed_query(hash_high bigint,hash_low smallint,"
            '"offset" integer,idx integer)'
        )
        cur.execute("TRUNCATE query,packed_query")
        with cur.copy('COPY query(hash,"offset",idx) FROM STDIN') as writer:
            for idx, (digest, offset) in enumerate(query):
                writer.write_row((digest, offset, idx))
        with cur.copy('COPY packed_query(hash_high,hash_low,"offset",idx) FROM STDIN') as writer:
            for idx, (digest, offset) in enumerate(query):
                writer.write_row((*split_hash(digest), offset, idx))
        cur.execute("ANALYZE query")
        cur.execute("ANALYZE packed_query")


def sql_rows(target: psycopg.Connection, statement: str) -> tuple[list[tuple], float]:
    """Fetch one query and its elapsed wall time."""
    start = time.perf_counter()
    with target.cursor() as cur:
        cur.execute(statement)
        rows = cur.fetchall()
    return rows, (time.perf_counter() - start) * 1000


def plan(target: psycopg.Connection, statement: str) -> dict:
    """Return an analyzed PostgreSQL plan without including query values."""
    with target.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement)
        return cur.fetchone()[0][0]


def size(target: psycopg.Connection, table: str) -> dict[str, int]:
    """Return heap and index sizes."""
    with target.cursor() as cur:
        cur.execute(
            "SELECT pg_relation_size(%s::regclass),pg_indexes_size(%s::regclass),"
            "pg_total_relation_size(%s::regclass)",
            (table, table, table),
        )
        heap, indexes, total = cur.fetchone()
        return {"heap_bytes": heap, "index_bytes": indexes, "total_bytes": total}


def main() -> None:
    """Run the experiment locally and print aggregate measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.samples < 1 or args.repetitions < 1:
        parser.error("samples and repetitions must be positive")
    name = f"fingerprint-postgres-layout-{uuid.uuid4().hex[:12]}"
    password = secrets.token_urlsafe(32)
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
            env=os.environ | {"POSTGRES_PASSWORD": password},
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
        with tempfile.TemporaryDirectory(prefix="fingerprint-postgres-layout-") as temp, target:
            path = Path(temp) / "snapshot.db"
            print("Preparing isolated SQLite copy", flush=True)
            snapshot(args.backup, path)
            with sqlite3.connect(path) as source:
                selected = samples(source, args.samples)
                copy_tables(source, target)
                prepare_query(target, selected[0])
                before_plan = plan(target, QUERY)
                _, before_time = sql_rows(target, QUERY)
                vacuum(target, "landmarks")
                prepare_query(target, selected[0])
                after_plan = plan(target, QUERY)
                print("Building integer-key alternative", flush=True)
                start = time.monotonic()
                copy_packed(source, target)
                packed_build_seconds = time.monotonic() - start
                vacuum(target, "packed_landmarks")
                print("Building 32-bit reference alternative", flush=True)
                start = time.monotonic()
                copy_small(source, target)
                small_build_seconds = time.monotonic() - start
                vacuum(target, "small_landmarks")
                baseline_times, packed_times, small_times, candidates = [], [], [], []
                packed_plan = {}
                small_plan = {}
                for sample in selected:
                    prepare_query(target, sample)
                    if not packed_plan:
                        packed_plan = plan(target, PACKED_QUERY)
                        small_plan = plan(target, SMALL_QUERY)
                    for iteration in range(args.repetitions + 1):
                        baseline_rows, baseline_ms = sql_rows(target, QUERY)
                        packed_rows, packed_ms = sql_rows(target, PACKED_QUERY)
                        small_rows, small_ms = sql_rows(target, SMALL_QUERY)
                        if any(
                            len(rows) > 100000 for rows in (baseline_rows, packed_rows, small_rows)
                        ):
                            raise ValueError("Sample exceeds candidate limit")
                        if sorted(baseline_rows, key=repr) != sorted(packed_rows, key=repr):
                            raise ValueError("Candidate sets differ between layouts")
                        if sorted(baseline_rows, key=repr) != sorted(small_rows, key=repr):
                            raise ValueError("Candidate sets differ for 32-bit references")
                        if iteration:
                            baseline_times.append(baseline_ms)
                            packed_times.append(packed_ms)
                            small_times.append(small_ms)
                    candidates.append(len(baseline_rows))
                    print(f"Validated sample {len(candidates)}: {candidates[-1]}", flush=True)
                output = {
                    "baseline_size": size(target, "landmarks"),
                    "packed_size": size(target, "packed_landmarks"),
                    "small_size": size(target, "small_landmarks"),
                    "before_vacuum_ms_one_run": round(before_time, 2),
                    "baseline_median_ms": round(statistics.median(baseline_times), 2),
                    "packed_median_ms": round(statistics.median(packed_times), 2),
                    "small_median_ms": round(statistics.median(small_times), 2),
                    "packed_build_seconds": round(packed_build_seconds, 2),
                    "small_build_seconds": round(small_build_seconds, 2),
                    "samples": len(selected),
                    "repetitions": args.repetitions,
                    "candidate_counts": candidates,
                    "before_vacuum_plan": before_plan,
                    "after_vacuum_plan": after_plan,
                    "packed_plan": packed_plan,
                    "small_plan": small_plan,
                }
                print(json.dumps(output, indent=2), flush=True)
    finally:
        if started:
            docker("stop", name)


if __name__ == "__main__":
    main()
