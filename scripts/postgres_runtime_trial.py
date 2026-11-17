"""Rehearse the real runtime and backup/restore on an isolated full SQLite snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import numpy as np
import psycopg
from audio_fingerprint import HOP_SIZE, SAMPLE_RATE, fingerprint
from migrate_postgres_to_sqlite import export_sqlite
from migrate_sqlite_to_postgres import create_snapshot, migrate
from postgres_benchmark import docker, samples
from psycopg import sql

from fingerprint_server.postgres_store import PostgresStore
from fingerprint_server.schemas import acoustic_reference_id, decode_submission
from fingerprint_server.settings import Settings
from fingerprint_server.store import QueryTooBroadError, Store

LOGGER = logging.getLogger(__name__)
SCHEMA = "runtime_trial"
MBID = "00000000-0000-4000-8000-000000000001"


def require(condition: bool, message: str) -> None:
    """Keep rehearsal checks enabled even under optimized Python."""
    if not condition:
        raise RuntimeError(message)


def unused_port() -> int:
    """Choose a currently unused loopback port for a bounded test process."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def process(arguments: list[str], env: dict[str, str], logfile: Path) -> Iterator[subprocess.Popen]:
    """Stop only the child created by this trial, preserving its log on failure."""
    with logfile.open("w") as log:
        child = subprocess.Popen(arguments, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    raise


def wait_state(store: PostgresStore, identifier: str, expected: str | None) -> None:
    """Wait for a finite processing milestone without masking worker failures."""
    deadline = time.monotonic() + 30
    while True:
        row = store.get_submission(identifier)
        if (row is None and expected is None) or (row and row["state"] == expected):
            return
        require(time.monotonic() < deadline, f"Timed out waiting for {identifier}: {row}")
        time.sleep(0.1)


@contextmanager
def metadata_stub() -> Iterator[tuple[str, list[str]]]:
    """Serve one synthetic identity locally; reject every real catalogue lookup."""
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            calls.append(self.path)
            allowed = self.path.split("?", 1)[0] == f"/ws/2/recording/{MBID}"
            body = json.dumps(
                {
                    "id": MBID,
                    "title": "Synthetic trial",
                    "length": 12000,
                    "artist-credit": [{"name": "Trial", "artist": {"name": "Trial"}}],
                    "isrcs": [],
                    "releases": [{"title": "Trial"}],
                    "release-count": 1,
                }
            ).encode()
            self.send_response(200 if allowed else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/ws/2", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def settings(env: dict[str, str], schema: str = SCHEMA) -> Settings:
    """Build explicit trial credentials without inheriting operator configuration."""
    return Settings(
        database_backend="postgres",
        postgres_schema=schema,
        service_role="api",
        postgres_dsn=psycopg.conninfo.make_conninfo(
            host=env["PGHOST"],
            port=env["PGPORT"],
            user=env["PGUSER"],
            password=env["PGPASSWORD"],
            dbname=env["PGDATABASE"],
        ),
        upload_key=env["FP_UPLOAD_KEY"],
        read_key=env["FP_READ_KEY"],
        musicbrainz_enabled=True,
        musicbrainz_contact="trial@example.invalid",
        musicbrainz_base_url=env["FP_MUSICBRAINZ_BASE_URL"],
    )


def content_digest(db: psycopg.Connection) -> dict[str, dict[str, str | int]]:
    """Hash every stored field, including landmarks, in deterministic binary order."""
    result = {}
    with db.cursor() as cur:
        for name, order in (
            ("submissions", "queue_order"),
            ("reference_tracks", "id"),
            ("landmarks", 'hash,reference_id,"offset"'),
            ("enrichment_cache", 'isrc COLLATE "C"'),
            ("service_state", 'key COLLATE "C"'),
            ("migration_info", "format_version"),
        ):
            checksum = hashlib.sha256()
            statement = sql.SQL(
                "COPY (SELECT * FROM {}.{} ORDER BY {}) TO STDOUT (FORMAT binary)"
            ).format(sql.Identifier(SCHEMA), sql.Identifier(name), sql.SQL(order))
            with cur.copy(statement) as cp:
                for block in cp:
                    checksum.update(block)
            cur.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(SCHEMA), sql.Identifier(name)
                )
            )
            result[name] = {"sha256": checksum.hexdigest(), "rows": cur.fetchone()[0]}
    return result


def match_outcome(store: Store | PostgresStore, query: list) -> list | dict:
    """Compare explicit candidate-limit errors as well as successful rankings."""
    try:
        return store.match(query)
    except QueryTooBroadError:
        return {"error": "QueryTooBroadError"}


def compare_runtime(
    source: Path, store: PostgresStore, sample_count: int, repetitions: int
) -> tuple[dict, list[list[tuple[str, int]]]]:
    """Compare exact runtime output, not merely candidate counts."""
    sqlite = Store(source)
    # Do not initialize SQLite: recovery/verification transitions would alter the snapshot.
    with sqlite3.connect(source) as db:
        queries = [
            [(digest.hex(), offset) for digest, offset in query]
            for query in samples(db, sample_count)
        ]
    require(sqlite.statistics() == store.statistics(), "Statistics mismatch")
    timings: dict[str, list[float]] = {"sqlite": [], "postgres": []}
    outcomes = {"matched": 0, "no_match": 0, "too_broad": 0}
    for query in queries:
        expected = match_outcome(sqlite, query)
        outcome = (
            "too_broad" if isinstance(expected, dict) else "matched" if expected else "no_match"
        )
        outcomes[outcome] += 1
        require(
            expected == match_outcome(store, query), "Full recognition ranking/metadata mismatch"
        )
        for _ in range(repetitions):
            for name, target in (("sqlite", sqlite), ("postgres", store)):
                started = time.perf_counter()
                actual = match_outcome(target, query)
                timings[name].append((time.perf_counter() - started) * 1000)
                require(actual == expected, f"{name} recognition changed across repetitions")
    with sqlite.connect() as db:
        rows = db.execute(
            "SELECT metadata FROM reference_tracks WHERE publication_ready=1 "
            "AND recording_key IS NOT NULL ORDER BY id LIMIT 100"
        ).fetchall()
    items = [{**json.loads(row[0]), "format_id": "ma-dejavu-landmarks-v2"} for row in rows]
    require(sqlite.availability(items) == store.availability(items), "Availability mismatch")
    return {
        "samples": len(queries),
        "repetitions": repetitions,
        "availability_items": len(items),
        "outcomes": outcomes,
        "median_ms": {name: statistics.median(values) for name, values in timings.items()},
        "max_ms": {name: max(values) for name, values in timings.items()},
    }, queries


def exercise_processes(env: dict[str, str], directory: Path, queries: list) -> dict:
    """Exercise real loopback HTTP, subprocess workers, restart and housekeeping."""
    store = PostgresStore(settings(env))
    # Freeze imported work only AFTER validating the untouched import. This prevents
    # catalogue reprocessing; the worker exercises isolated synthetic fixtures instead.
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE submissions SET retry_at=%s,pending_due_at=%s "
            "WHERE state IN ('queued','processing','pending_metadata')",
            (time.time() + 86400 * 365, time.time() + 86400 * 365),
        )
    signal = np.random.default_rng(42).normal(0, 0.1, SAMPLE_RATE * 12).astype("<f4")
    pairs = fingerprint(signal)
    document = {
        "format_id": "ma-dejavu-landmarks-v2",
        "sample_rate": SAMPLE_RATE,
        "offset_unit": "fft_hops",
        "windows": [{"start_sample": 0, "sample_count": len(signal)}],
        "fingerprints": [list(pair) for pair in pairs],
        "metadata": {
            "title": "Synthetic trial",
            "artists": ["Trial"],
            "album": "Trial",
            "duration": 12,
            "recording_mbid": MBID,
        },
    }
    body = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    identifier = hashlib.sha256(body).hexdigest()
    port = unused_port()
    api_env = env | {"FP_SERVICE_ROLE": "api"}
    worker_env = env | {"FP_SERVICE_ROLE": "worker"}
    api_command = [
        sys.executable,
        "-m",
        "uvicorn",
        "fingerprint_server.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        "1",
    ]
    worker_command = [sys.executable, "-m", "fingerprint_server.worker_runtime"]
    with process(api_command, api_env, directory / "api.log") as api:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
        ) as client:
            deadline = time.monotonic() + 20
            while True:
                require(api.poll() is None, "API exited before becoming responsive")
                try:
                    response = client.get("/health")
                    break
                except httpx.ConnectError:
                    require(time.monotonic() < deadline, "API did not start")
                    time.sleep(0.1)
            require(response.status_code == 503, "API must notice absent worker")
            with process(worker_command, worker_env, directory / "worker-first.log"):
                deadline = time.monotonic() + 20
                while client.get("/health").status_code != 200:
                    require(time.monotonic() < deadline, "Worker failed to become healthy")
                    time.sleep(0.1)
                response = client.post(
                    "/api/v1/dejavu/submissions",
                    content=body,
                    headers={
                        "X-API-Key": env["FP_UPLOAD_KEY"],
                        "Content-Type": "application/json",
                        "Idempotency-Key": identifier,
                    },
                )
                require(response.status_code == 202, f"HTTP upload failed: {response.text}")
                wait_state(store, identifier, "processed")
                require(
                    store.reference_metadata(
                        acoustic_reference_id(decode_submission(body.decode()))
                    )["recording_mbid"]
                    == MBID,
                    "Synthetic identity not verified",
                )
                start = time.perf_counter()
                response = client.post(
                    "/api/v1/dejavu/recognize?sample_rate=44100",
                    content=signal[50 * HOP_SIZE : 50 * HOP_SIZE + 5 * SAMPLE_RATE].tobytes(),
                    headers={
                        "X-API-Key": env["FP_READ_KEY"],
                        "Content-Type": "application/octet-stream",
                    },
                )
                recognition_ms = (time.perf_counter() - start) * 1000
                require(
                    response.status_code == 200 and response.json()["status"] == "matched",
                    f"Synthetic PCM recognition failed: {response.text}",
                )
                require(
                    response.json()["match"]["metadata"]["recording_mbid"] == MBID,
                    "PCM recognized wrong recording",
                )
            require(client.get("/health").status_code == 503, "Stopped worker reported healthy")
            # Prepare an interrupted claim and an identifier-free legacy reference.
            store.submit("trial-interrupted", body.decode(), 100000, 10**12)
            with store.connect(write=True) as db:
                db.execute(
                    "UPDATE submissions SET state='processing',attempts=1 "
                    "WHERE id='trial-interrupted'"
                )
            cleanup = {**document, "fingerprints": [[hashlib.sha1(b"trial").hexdigest()[:20], 0]]}
            cleanup_key = acoustic_reference_id(decode_submission(json.dumps(cleanup)))
            store.submit("trial-housekeeping", json.dumps(cleanup), 100000, 10**12)
            store.finish(
                "trial-housekeeping",
                cleanup_key,
                document["metadata"],
                [(cleanup["fingerprints"][0][0], 0)],
                verified=True,
            )
            cleanup["metadata"] = {"duration": 12}
            with store.connect(write=True) as db:
                db.execute(
                    "UPDATE submissions SET state='pending_metadata',pending_due_at=0,document=%s "
                    "WHERE id='trial-housekeeping'",
                    (json.dumps(cleanup),),
                )
                db.execute(
                    "UPDATE reference_tracks SET publication_ready=0,metadata=%s "
                    "WHERE reference_key=%s",
                    (json.dumps({"duration": 12}), cleanup_key),
                )
            with process(worker_command, worker_env, directory / "worker-restart.log"):
                wait_state(store, "trial-interrupted", "processed")
                wait_state(store, "trial-housekeeping", None)
                with store.connect() as db:
                    require(
                        db.execute(
                            "SELECT 1 FROM reference_tracks WHERE reference_key=%s", (cleanup_key,)
                        ).fetchone()
                        is None,
                        "Housekeeping retained unused synthetic reference",
                    )
                    require(
                        db.execute(
                            "SELECT 1 FROM landmarks WHERE hash=%s",
                            (bytes.fromhex(cleanup["fingerprints"][0][0]),),
                        ).fetchone()
                        is None,
                        "Housekeeping retained synthetic landmarks",
                    )
            timings = []

            def lookup(query: list) -> list | dict:
                started = time.perf_counter()
                result = match_outcome(store, query)
                timings.append((time.perf_counter() - started) * 1000)
                return result

            expected = [match_outcome(store, query) for query in queries[:4]]
            with ThreadPoolExecutor(max_workers=2) as executor:
                actual = list(executor.map(lookup, queries[:4]))
            require(actual == expected, "Two concurrent recognition calls changed results")
    return {
        "http_ingestion": "passed",
        "synthetic_pcm_match": "passed",
        "pcm_http_ms": recognition_ms,
        "worker_restart_recovery": "passed",
        "missing_identifier_reference_and_landmark_cleanup": "passed",
        "two_concurrent_store_queries": len(timings),
        "concurrent_query_median_ms": statistics.median(timings),
        "imported_work": "paused on PostgreSQL test copy after parity checks",
    }


def run_trial(source: Path, report: Path, count: int, repetitions: int) -> None:
    """Import, exercise, dump and restore a disposable PostgreSQL collection."""
    report.parent.mkdir(parents=True, exist_ok=True)
    name = f"fingerprint-runtime-trial-{uuid.uuid4().hex[:12]}"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("FP_", "PG"))}
    env.update(
        PGHOST="127.0.0.1",
        PGUSER="postgres",
        PGDATABASE="postgres",
        PGPASSWORD=secrets.token_urlsafe(32),
        FP_DATABASE_BACKEND="postgres",
        FP_POSTGRES_SCHEMA=SCHEMA,
        FP_MUSICBRAINZ_ENABLED="true",
        FP_HOUSEKEEPING_ENABLED="true",
        FP_MUSICBRAINZ_CONTACT="trial@example.invalid",
        FP_UPLOAD_KEY=secrets.token_urlsafe(32),
        FP_READ_KEY=secrets.token_urlsafe(32),
    )
    result: dict = {
        "status": "running",
        "source": "consistent read-only SQLite backup",
        "postgres_limits": {"cpus": 2, "memory_gib": 4},
    }
    with tempfile.TemporaryDirectory(prefix="fingerprint-runtime-") as work:
        directory = Path(work)
        snapshot = directory / "source.db"
        LOGGER.info("Creating consistent SQLite snapshot through a read-only source connection")
        create_snapshot(source, snapshot)
        with sqlite3.connect(snapshot) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 7:
                db.execute("CREATE INDEX landmarks_reference ON landmarks(reference_id)")
                db.execute("PRAGMA user_version=8")
            require(version in (7, 8), "Unsupported snapshot version")
            result["sqlite_active_bytes"] = (
                db.execute("PRAGMA page_count").fetchone()[0]
                - db.execute("PRAGMA freelist_count").fetchone()[0]
            ) * db.execute("PRAGMA page_size").fetchone()[0]
            result["sqlite_file_bytes"] = snapshot.stat().st_size
            result["sqlite_landmark_bytes"] = db.execute(
                "SELECT sum(pgsize) FROM dbstat WHERE name IN ('landmarks','landmarks_reference')"
            ).fetchone()[0]
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
            env=env | {"POSTGRES_PASSWORD": env["PGPASSWORD"]},
        )
        try:
            env["PGPORT"] = docker("port", name, "5432/tcp").rsplit(":", 1)[1]
            deadline = time.monotonic() + 60
            while True:
                try:
                    db = psycopg.connect(
                        settings(
                            env | {"FP_MUSICBRAINZ_BASE_URL": "http://127.0.0.1/ws/2"}
                        ).postgres_dsn.get_secret_value(),
                        autocommit=True,
                    )
                    break
                except psycopg.OperationalError:
                    require(time.monotonic() < deadline, "PostgreSQL failed to start")
                    time.sleep(0.5)
            with db, metadata_stub() as (endpoint, calls):
                env["FP_MUSICBRAINZ_BASE_URL"] = endpoint
                LOGGER.info("Importing and checking every stored field and landmark")
                started = time.perf_counter()
                with sqlite3.connect(snapshot) as sqlite:
                    result["import_counts"] = migrate(sqlite, db, SCHEMA)
                result["import_seconds"] = time.perf_counter() - started
                db.execute(
                    sql.SQL("SET search_path TO {},pg_catalog").format(sql.Identifier(SCHEMA))
                )
                db.execute("VACUUM (ANALYZE)")
                result["postgres_version"] = db.execute("SHOW server_version").fetchone()[0]
                result["postgres_database_bytes"] = db.execute(
                    "SELECT pg_database_size(current_database())"
                ).fetchone()[0]
                result["postgres_landmark_bytes"] = db.execute(
                    "SELECT pg_total_relation_size('landmarks')"
                ).fetchone()[0]
                result["postgres_landmark_heap_bytes"] = db.execute(
                    "SELECT pg_relation_size('landmarks')"
                ).fetchone()[0]
                result["postgres_wal_bytes"] = int(
                    db.execute("SELECT sum(size) FROM pg_ls_waldir()").fetchone()[0]
                )
                store = PostgresStore(settings(env))
                store.initialize()
                LOGGER.info("Comparing actual runtime output against SQLite")
                result["recognition"], queries = compare_runtime(
                    snapshot, store, count, repetitions
                )
                LOGGER.info("Testing API and worker subprocesses with full collection present")
                result["runtime"] = exercise_processes(env, directory, queries)
                require(
                    all(path.split("?", 1)[0] == f"/ws/2/recording/{MBID}" for path in calls),
                    "Trial attempted a real catalogue MusicBrainz lookup",
                )
                result["stub_musicbrainz_calls"] = len(calls)
                LOGGER.info("Hashing full PostgreSQL content before backup")
                before = content_digest(db)
                LOGGER.info("Exporting all PostgreSQL changes back to a new SQLite database")
                rollback = directory / "rollback.db"
                started = time.perf_counter()
                reverse = export_sqlite(db, SCHEMA, rollback)
                result["reverse_export"] = {
                    **reverse,
                    "seconds": time.perf_counter() - started,
                    "bytes": rollback.stat().st_size,
                }
                result["reverse_export"]["recognition"], _ = compare_runtime(
                    rollback, store, count, 1
                )
                require(
                    content_digest(db) == before,
                    "Reverse export changed PostgreSQL source content",
                )
                backup = directory / "backup.dump"
                started = time.perf_counter()
                with backup.open("wb") as file:
                    subprocess.run(
                        [
                            "docker",
                            "exec",
                            name,
                            "pg_dump",
                            "-U",
                            "postgres",
                            "-Fc",
                            "--schema",
                            SCHEMA,
                            "postgres",
                        ],
                        stdout=file,
                        check=True,
                    )
                result["backup"] = {
                    "seconds": time.perf_counter() - started,
                    "bytes": backup.stat().st_size,
                }
                db.execute("CREATE DATABASE restored")
                LOGGER.info("Restoring backup into a second empty PostgreSQL database")
                started = time.perf_counter()
                with backup.open("rb") as file:
                    subprocess.run(
                        [
                            "docker",
                            "exec",
                            "-i",
                            name,
                            "pg_restore",
                            "-U",
                            "postgres",
                            "--exit-on-error",
                            "--no-owner",
                            "--no-acl",
                            "-d",
                            "restored",
                        ],
                        stdin=file,
                        check=True,
                    )
                restored_env = env | {"PGDATABASE": "restored"}
                restored_store = PostgresStore(settings(restored_env))
                restored_store.initialize()
                with psycopg.connect(
                    settings(restored_env).postgres_dsn.get_secret_value(), autocommit=True
                ) as restored:
                    require(content_digest(restored) == before, "Backup/restore content mismatch")
                    restored.execute("VACUUM (ANALYZE)")
                for query in queries:
                    require(
                        match_outcome(store, query) == match_outcome(restored_store, query),
                        "Restored recognition differs from pre-backup runtime",
                    )
                result["backup"]["restore_and_verification_seconds"] = time.perf_counter() - started
                result["backup"]["all_table_checksums"] = before
                result["backup"]["recognition_samples"] = len(queries)
                result["status"] = "passed"
        finally:
            if result["status"] != "passed":
                result["status"] = "failed"
                for log in directory.glob("*.log"):
                    LOGGER.error("%s:\n%s", log.name, log.read_text()[-8000:])
            docker("stop", name)
            report.write_text(json.dumps(result, indent=2) + "\n")
    LOGGER.info("Trial completed; report: %s", report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    require(
        args.samples >= 4 and args.repetitions >= 1, "Need at least four samples and one repetition"
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_trial(args.source, args.report, args.samples, args.repetitions)
