"""PostgreSQL storage with serialized admission and a single ingestion owner."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import ValidationError as ValidationError

from fingerprint_server.database import application_keys, postgres_postings
from fingerprint_server.database.logic import (
    can_add_recording_mbid as can_add_recording_mbid,
)
from fingerprint_server.database.logic import (
    coverage_statistics,
    full_coverage,
    has_identifier,
    rank_matches,
    resolve_availability,
)
from fingerprint_server.database.logic import (
    initial_submission_state as initial_submission_state,
)
from fingerprint_server.database.metrics import initialize_metrics
from fingerprint_server.database.migrations import MUSICBRAINZ_VERIFICATION_VERSION
from fingerprint_server.database.notifications import postgres_channel
from fingerprint_server.database.postgres_queue import PostgresStoreQueueMixin
from fingerprint_server.database.postgres_schema import (
    FORMAT_VERSION,
    TABLES,
    create_tables,
    finish_schema,
)
from fingerprint_server.database.review_schema import REVIEW_TABLE
from fingerprint_server.database.sqlite import QueryTooBroadError
from fingerprint_server.database.sqlite import QueueFullError as QueueFullError
from fingerprint_server.schemas import (
    acoustic_reference_id as acoustic_reference_id,
)
from fingerprint_server.schemas import (
    decode_submission as decode_submission,
)
from fingerprint_server.schemas import (
    metadata_complete,
)
from fingerprint_server.settings import MUSICBRAINZ_CACHE_TTL_DAYS, Settings

LOGGER = logging.getLogger(__name__)
type Connection = psycopg.Connection[dict[str, Any]]


class PostgresStore(PostgresStoreQueueMixin):
    """Durable PostgreSQL implementation; blocking methods run outside the event loop."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.schema = settings.postgres_schema
        self.musicbrainz_enabled = settings.musicbrainz_enabled
        self.housekeeping_enabled = settings.housekeeping_enabled
        self.pending_max_rechecks = settings.pending_max_rechecks
        self.pending_recheck_days = settings.pending_recheck_days
        self.background_recheck_queue_window = settings.background_recheck_queue_window
        self._worker_token: str | None = None
        self._format_version = FORMAT_VERSION

    @contextmanager
    def connect(self, *, write: bool = False, notification: str = "all") -> Iterator[Connection]:
        """Open a transaction, serializing mutations before reading admission state."""
        with self._open() as db:
            if not write:
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            db.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(self.schema)
                )
            )
            if write:
                db.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"fingerprint:{self.schema}:write",),
                )
                if self._worker_token is not None:
                    row = db.execute(
                        "SELECT value FROM service_state WHERE key='worker_token'"
                    ).fetchone()
                    if row is None or row["value"] != self._worker_token:
                        raise RuntimeError("PostgreSQL ingestion ownership was lost")
            yield db
            if write:
                db.execute("SELECT pg_notify(%s,%s)", (postgres_channel(self.schema), notification))

    def initialize(self) -> None:
        """Create an empty schema or validate an imported, versioned schema."""
        with self.connect(write=True) as db:
            exists = db.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname=%s", (self.schema,)
            ).fetchone()
            if exists is None:
                db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
                create_tables(db)
                finish_schema(db)
                LOGGER.info(
                    "Created PostgreSQL schema %s at version %s", self.schema, FORMAT_VERSION
                )
            version = db.execute("SELECT format_version FROM migration_info").fetchall()
            if len(version) != 1 or version[0]["format_version"] not in (
                FORMAT_VERSION,
                postgres_postings.POSTINGS_VERSION,
            ):
                raise ValueError(
                    f"Unsupported PostgreSQL schema version; expected {FORMAT_VERSION}"
                )
            self._format_version = version[0]["format_version"]
            expected = {name: set(spec["columns"]) for name, spec in TABLES.items()}
            expected["landmarks"] = {"hash", "reference_id", "offset"}
            if self._format_version == postgres_postings.POSTINGS_VERSION:
                expected["landmark_postings"] = {
                    "hash",
                    "reference_block",
                    "offset_block",
                    "reference_ids",
                    "offsets",
                }
            for name, columns in expected.items():
                actual = db.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s",
                    (self.schema, name),
                ).fetchall()
                if {row["column_name"] for row in actual} != columns:
                    raise ValueError(f"Unexpected PostgreSQL table layout: {name}")
            if self.settings.gateway_enabled:
                application_keys.initialize(db)
            db.execute(REVIEW_TABLE)
            initialize_metrics(db)

    @contextmanager
    def owner(self, role: str) -> Iterator[Connection]:
        """Hold API membership or exclusive ingestion ownership on a database session."""
        if role not in ("api", "worker"):
            raise ValueError("Unknown PostgreSQL process role")
        key = f"fingerprint:{self.schema}:{role}"
        with self._open(autocommit=True) as db:
            function = (
                "pg_try_advisory_lock_shared"
                if role == "api" and self.settings.api_replicas_enabled
                else "pg_try_advisory_lock"
            )
            row = db.execute(
                sql.SQL("SELECT {}(hashtextextended(%s,0)) AS acquired").format(sql.SQL(function)),
                (key,),
            ).fetchone()
            if row is None or not row["acquired"]:
                raise RuntimeError(f"A PostgreSQL {role} process is already active")
            db.execute(
                sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(self.schema))
            )
            try:
                if role == "worker":
                    token = uuid4().hex
                    with self.connect(write=True) as transaction:
                        self._set_state(transaction, "worker_token", token)
                    self._worker_token = token
                    self.prepare_worker()
                self.heartbeat(db, role)
                yield db
            finally:
                # The connection owns the lock until close, including on startup failure.
                self._worker_token = None

    def heartbeat(self, db: Connection, role: str) -> None:
        """Check the ownership connection and publish worker liveness."""
        if role == "api":
            db.execute("SELECT 1")
            return
        db.execute(
            "INSERT INTO service_state(key,value) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"{role}_heartbeat", str(time.time())),
        )

    def worker_available(self) -> bool:
        """Require both a held worker lock and a recent heartbeat."""
        key = f"fingerprint:{self.schema}:worker"
        with self._open(autocommit=True) as db:
            row = db.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", (key,)
            ).fetchone()
            if row is not None and row["acquired"]:
                db.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
                return False
            row = db.execute(
                sql.SQL("SELECT value FROM {}.service_state WHERE key='worker_heartbeat'").format(
                    sql.Identifier(self.schema)
                )
            ).fetchone()
            return row is not None and time.time() - float(row["value"]) < 20

    def allow_request(self, scope: str, limit: int) -> bool:
        """Apply one shared rolling-minute budget using the database clock."""
        if scope not in ("upload", "read", "statistics", "availability"):
            raise ValueError("Unknown request budget")
        key = f"api_rate:{scope}"
        with self._open() as db:
            db.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(self.schema)
                )
            )
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"fingerprint:{self.schema}:{key}",),
            )
            now = self._one(db, "SELECT extract(epoch FROM clock_timestamp())::float8 AS now")[
                "now"
            ]
            row = db.execute("SELECT value FROM service_state WHERE key=%s", (key,)).fetchone()
            times = json.loads(row["value"]) if row else []
            if not isinstance(times, list) or any(
                type(timestamp) not in (int, float) for timestamp in times
            ):
                raise ValueError("Invalid shared request budget")
            times = [timestamp for timestamp in times if timestamp > now - 60]
            if len(times) >= limit:
                return False
            times.append(now)
            self._set_state(db, key, json.dumps(times))
            return True

    @contextmanager
    def recognition_slot(self) -> Iterator[bool]:
        """Hold one of two shared recognition slots until all request work finishes."""
        with self._open(autocommit=True) as db:
            for index in range(2):
                row = db.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired",
                    (f"fingerprint:{self.schema}:recognition:{index}",),
                ).fetchone()
                if row is not None and row["acquired"]:
                    yield True
                    # Do not return a successful recognition after losing its slot connection.
                    db.execute("SELECT 1")
                    return
            yield False

    def prepare_worker(self) -> None:
        """Recover abandoned claims and apply verification changes only as owner."""
        if self._worker_token is None:
            raise RuntimeError("Worker recovery requires PostgreSQL ingestion ownership")
        with self.connect(write=True) as db:
            db.execute(
                "UPDATE submissions SET "
                "state=CASE WHEN pending_check=1 AND background_kind=0 "
                "THEN 'pending_metadata' ELSE 'queued' END WHERE state='processing'"
            )
            previous = db.execute(
                "SELECT value FROM service_state WHERE key='musicbrainz_enabled'"
            ).fetchone()
            version = db.execute(
                "SELECT value FROM service_state WHERE key='verification_version'"
            ).fetchone()
            enabled = "1" if self.musicbrainz_enabled else "0"
            current = str(MUSICBRAINZ_VERIFICATION_VERSION)
            changed = previous is None or previous["value"] != enabled
            changed |= self.musicbrainz_enabled and (version is None or version["value"] != current)
            if changed:
                self._verification_transition(db)
                self._set_state(db, "musicbrainz_enabled", enabled)
                if self.musicbrainz_enabled:
                    self._set_state(db, "verification_version", current)
            if not self.housekeeping_enabled or not self.musicbrainz_enabled:
                db.execute(
                    "UPDATE submissions SET state='pending_metadata',pending_check=0,"
                    "background_kind=0 WHERE state='queued' AND background_kind=1"
                )

    def cached_enrichment(self, key: str) -> dict[str, Any] | None:
        """Read an unexpired persistent MusicBrainz result."""
        with self.connect() as db:
            row = db.execute(
                "SELECT document FROM enrichment_cache WHERE isrc=%s AND expires_at>%s",
                (key, time.time()),
            ).fetchone()
            return json.loads(row["document"]) if row else None

    def cache_enrichment(self, key: str, document: dict[str, Any]) -> None:
        """Cache positive, ambiguous and empty upstream results."""
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO enrichment_cache VALUES(%s,%s,%s) ON CONFLICT(isrc) DO UPDATE "
                "SET document=excluded.document,expires_at=excluded.expires_at",
                (key, json.dumps(document), time.time() + MUSICBRAINZ_CACHE_TTL_DAYS * 86400),
            )

    def match(self, query: list[tuple[str, int]]) -> list[dict[str, Any]]:
        """Fetch bounded candidates and apply the same ranking as SQLite."""
        with self.connect() as db:
            db.execute(
                'CREATE TEMP TABLE query(hash bytea, "offset" integer, idx integer) ON COMMIT DROP'
            )
            with db.cursor().copy('COPY query(hash,"offset",idx) FROM STDIN') as cp:
                for index, (digest, offset) in enumerate(query):
                    cp.write_row((bytes.fromhex(digest), offset, index))
            db.execute("ANALYZE query")
            rows = db.execute(
                'SELECT t.reference_key AS reference_id,t.recording_key,l."offset"-q."offset" '
                'AS delta,q.idx,q."offset" FROM query q JOIN landmarks l ON l.hash=q.hash '
                "JOIN reference_tracks t ON t.id=l.reference_id "
                "WHERE t.publication_ready=1 LIMIT 100001"
            ).fetchall()
            if len(rows) > 100000:
                raise QueryTooBroadError

            def read_metadata(reference_id: str) -> str:
                return str(
                    self._one(
                        db,
                        "SELECT metadata FROM reference_tracks WHERE reference_key=%s",
                        (reference_id,),
                    )["metadata"]
                )

            return rank_matches(query, rows, read_metadata)

    def statistics(self) -> dict[str, Any]:
        """Report coverage and queue counts from one repeatable-read snapshot."""
        with self.connect() as db:
            uploads = dict.fromkeys(
                ("queued", "processing", "processed", "failed", "pending_metadata"), 0
            )
            uploads.update(
                {
                    row["state"]: row["count"]
                    for row in db.execute(
                        "SELECT state,count(*) AS count FROM submissions GROUP BY state"
                    )
                }
            )
            rows = db.execute(
                "WITH representatives AS (SELECT reference_id,min(id) AS id FROM submissions "
                "WHERE state='processed' GROUP BY reference_id) "
                "SELECT t.id,t.recording_key,t.publication_ready,t.metadata,s.document "
                "FROM reference_tracks t "
                "LEFT JOIN representatives r ON r.reference_id=t.reference_key "
                "LEFT JOIN submissions s ON s.id=r.id"
            ).fetchall()
            return coverage_statistics(rows, uploads)

    def availability(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Report verified recording coverage without an upstream lookup."""
        mbids = sorted(
            {UUID(item["recording_mbid"]).hex for item in items if item.get("recording_mbid")}
        )
        isrcs = sorted({item["isrc"].upper() for item in items if item.get("isrc")})
        if not mbids and not isrcs:
            return [{"status": "unknown", "recording_mbid": None} for _ in items]
        with self.connect() as db:
            candidates = db.execute(
                "SELECT recording_key,metadata FROM reference_tracks WHERE publication_ready=1 "
                "AND recording_key IS NOT NULL AND verification_version=%s "
                "AND (recording_key=ANY(%s) OR upper(metadata::jsonb->>'isrc')=ANY(%s)) "
                "ORDER BY id LIMIT 10000",
                (MUSICBRAINZ_VERIFICATION_VERSION, mbids, isrcs),
            ).fetchall()
            resolved = resolve_availability(items, candidates)
            keys = sorted({key for key in resolved if key is not None})
            coverage = dict.fromkeys(keys, False)
            variants = db.execute(
                "WITH relevant AS (SELECT reference_key,recording_key,metadata "
                "FROM reference_tracks "
                "WHERE publication_ready=1 AND verification_version=%s AND recording_key=ANY(%s) "
                "ORDER BY id LIMIT 10000), representatives AS ("
                "SELECT reference_id,min(id) AS id FROM submissions WHERE state='processed' "
                "AND reference_id IN (SELECT reference_key FROM relevant) GROUP BY reference_id) "
                "SELECT t.recording_key,t.metadata,s.document FROM relevant t "
                "LEFT JOIN representatives r ON r.reference_id=t.reference_key "
                "LEFT JOIN submissions s ON s.id=r.id",
                (MUSICBRAINZ_VERIFICATION_VERSION, keys),
            )
            for row in variants:
                document = row["document"]
                try:
                    payload = json.loads(document) if document else {}
                except (ValueError, TypeError):
                    LOGGER.warning(
                        "Cannot inspect retained coverage for recording %s", row["recording_key"]
                    )
                    continue
                if (
                    document
                    and payload.get("format_id")
                    in ("ma-dejavu-landmarks-v1", "ma-dejavu-landmarks-v2")
                    and full_coverage(document, row["metadata"])
                ):
                    coverage[row["recording_key"]] = True
        return [
            {
                "status": "full" if key and coverage[key] else "partial" if key else "unknown",
                "recording_mbid": str(UUID(key)) if key else None,
            }
            for key in resolved
        ]

    def _open(self, *, autocommit: bool = False) -> Connection:
        dsn = self.settings.postgres_dsn
        return psycopg.connect(
            dsn.get_secret_value() if dsn is not None else "",
            row_factory=dict_row,
            autocommit=autocommit,
            connect_timeout=5,
            application_name=f"fingerprint:{self.schema}",
            options="-c statement_timeout=60000 -c lock_timeout=15000",
        )

    @staticmethod
    def _one(db: Connection, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
        row = db.execute(query, params).fetchone()
        if row is None:
            raise ValueError("Expected PostgreSQL row is missing")
        return row

    @staticmethod
    def _set_state(db: Connection, key: str, value: str) -> None:
        db.execute(
            "INSERT INTO service_state VALUES(%s,%s) ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _verification_transition(self, db: Connection) -> None:
        if self.musicbrainz_enabled:
            db.execute(
                "UPDATE reference_tracks SET publication_ready=0 WHERE verification_version!=%s",
                (MUSICBRAINZ_VERIFICATION_VERSION,),
            )
            rows = db.execute(
                "SELECT s.id,s.document,s.state,t.metadata FROM submissions s "
                "LEFT JOIN reference_tracks t ON t.reference_key=s.reference_id "
                "WHERE s.state='processed' AND (s.reference_id IS NULL "
                "OR coalesce(t.verification_version,0)!=%s)",
                (MUSICBRAINZ_VERIFICATION_VERSION,),
            ).fetchall()
            for row in rows:
                try:
                    supplied = json.loads(row["document"]).get("metadata", {})
                    existing = json.loads(row["metadata"]) if row["metadata"] else {}
                    usable = has_identifier(supplied) or has_identifier(existing)
                except (ValueError, TypeError, AttributeError):
                    LOGGER.warning("Cannot inspect retained metadata for submission %s", row["id"])
                    usable = False
                if usable:
                    db.execute(
                        "UPDATE submissions SET state='pending_metadata',attempts=0,retry_at=0,"
                        "error=NULL,background_kind=2,pending_check=0,"
                        "pending_due_at=%s,enqueued_at=0 "
                        "WHERE id=%s",
                        (time.time(), row["id"]),
                    )
                elif row["metadata"] is not None:
                    db.execute(
                        "UPDATE submissions SET state='pending_metadata',"
                        "error='MusicBrainz identifier unavailable',pending_due_at=CASE "
                        "WHEN pending_due_at>0 THEN pending_due_at ELSE %s END WHERE id=%s",
                        (time.time() + self.pending_recheck_days * 86400, row["id"]),
                    )
        else:
            for row in db.execute(
                "SELECT id,metadata FROM reference_tracks WHERE publication_ready=0"
            ).fetchall():
                try:
                    complete = metadata_complete(json.loads(row["metadata"]))
                except (TypeError, ValueError):
                    LOGGER.warning("Cannot restore reference %s with invalid metadata", row["id"])
                    complete = False
                if complete:
                    db.execute(
                        "UPDATE reference_tracks SET publication_ready=1 WHERE id=%s", (row["id"],)
                    )
            for row in db.execute(
                "SELECT id,document FROM submissions WHERE state='pending_metadata'"
            ).fetchall():
                try:
                    complete = metadata_complete(json.loads(row["document"])["metadata"])
                except (TypeError, ValueError, KeyError):
                    LOGGER.warning("Cannot inspect retained submission %s metadata", row["id"])
                    complete = False
                if complete:
                    db.execute(
                        "UPDATE submissions SET state='queued',attempts=0,retry_at=0,error=NULL,"
                        "background_kind=0,pending_check=0,enqueued_at=%s WHERE id=%s",
                        (time.time(), row["id"]),
                    )
