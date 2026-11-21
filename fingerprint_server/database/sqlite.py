"""Durable SQLite queue and atomic, indexed recognition references."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError as ValidationError

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
from fingerprint_server.database.migrations import (
    MUSICBRAINZ_VERIFICATION_VERSION,
    initialize_database,
)
from fingerprint_server.database.review_schema import REVIEW_TABLE
from fingerprint_server.database.sqlite_queue import StoreQueueMixin
from fingerprint_server.schemas import (
    acoustic_reference_id as acoustic_reference_id,
)
from fingerprint_server.schemas import (
    decode_submission as decode_submission,
)
from fingerprint_server.schemas import (
    metadata_complete,
)
from fingerprint_server.settings import MUSICBRAINZ_CACHE_TTL_DAYS

MATCH_QUERY = """
    SELECT t.reference_key AS reference_id,t.recording_key,
        l.offset-q.offset AS delta,q.idx,q.offset
    FROM query q CROSS JOIN landmarks l ON l.hash=q.hash
    JOIN reference_tracks t ON t.id=l.reference_id
    WHERE t.publication_ready=1 LIMIT 100001
"""

LOGGER = logging.getLogger(__name__)


class QueueFullError(Exception):
    """The persistent submission queue is at capacity."""


class QueryTooBroadError(Exception):
    """The query generates more candidates than can be safely evaluated."""


class Store(StoreQueueMixin):
    """Single-process SQLite storage; call methods outside the event loop."""

    def __init__(
        self,
        path: Path,
        musicbrainz_enabled: bool = False,
        housekeeping_enabled: bool = True,
        pending_max_rechecks: int = 3,
        pending_recheck_days: int = MUSICBRAINZ_CACHE_TTL_DAYS,
        background_recheck_queue_window: int = 2,
    ) -> None:
        self.path = path
        self.musicbrainz_enabled = musicbrainz_enabled
        self.housekeeping_enabled = housekeeping_enabled
        self.pending_max_rechecks = pending_max_rechecks
        self.pending_recheck_days = pending_recheck_days
        self.background_recheck_queue_window = background_recheck_queue_window

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection with foreign-key enforcement."""
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def owner(self) -> Iterator[None]:
        """Retain exclusive service ownership without blocking separate admin connections."""
        import fcntl

        path = self.path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as err:
                raise RuntimeError(
                    f"SQLite database is already owned by another service: {path}"
                ) from err
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def initialize(self) -> None:
        """Upgrade storage and apply verification transitions before serving requests."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            initialize_database(db, self.path)
            db.execute("BEGIN IMMEDIATE")
            db.execute(REVIEW_TABLE)
            initialize_metrics(db)
            previous = db.execute(
                "SELECT value FROM service_state WHERE key='musicbrainz_enabled'"
            ).fetchone()
            previous_version = db.execute(
                "SELECT value FROM service_state WHERE key='verification_version'"
            ).fetchone()
            enabled = "1" if self.musicbrainz_enabled else "0"
            current_version = str(MUSICBRAINZ_VERIFICATION_VERSION)
            changed = previous is None or previous[0] != enabled
            if self.musicbrainz_enabled and (
                previous_version is None or previous_version[0] != current_version
            ):
                changed = True
            if changed:
                if self.musicbrainz_enabled:
                    db.execute(
                        "UPDATE reference_tracks SET publication_ready=0 "
                        "WHERE verification_version!=?",
                        (MUSICBRAINZ_VERIFICATION_VERSION,),
                    )
                    rows = db.execute(
                        "SELECT s.id FROM submissions s "
                        "LEFT JOIN reference_tracks t ON t.reference_key=s.reference_id "
                        "WHERE s.state='processed' AND "
                        "(s.reference_id IS NULL OR coalesce(t.verification_version,0)!=?)",
                        (MUSICBRAINZ_VERIFICATION_VERSION,),
                    ).fetchall()
                    for (submission_id,) in rows:
                        row = db.execute(
                            "SELECT s.document,s.state,t.metadata FROM submissions s "
                            "LEFT JOIN reference_tracks t ON t.reference_key=s.reference_id "
                            "WHERE s.id=?",
                            (submission_id,),
                        ).fetchone()
                        try:
                            supplied = json.loads(row["document"]).get("metadata", {})
                            existing = json.loads(row["metadata"]) if row["metadata"] else {}
                            usable = has_identifier(supplied) or (
                                row["state"] == "processed" and has_identifier(existing)
                            )
                        except (ValueError, TypeError, AttributeError):
                            LOGGER.warning(
                                "Cannot inspect retained metadata for submission %s",
                                submission_id,
                            )
                            usable = False
                        if usable:
                            db.execute(
                                "UPDATE submissions SET state='pending_metadata',attempts=0,"
                                "retry_at=0,error=NULL,background_kind=2,pending_check=0,"
                                "pending_due_at=?,enqueued_at=0 WHERE id=?",
                                (time.time(), submission_id),
                            )
                        elif row["metadata"] is not None:
                            db.execute(
                                "UPDATE submissions SET state='pending_metadata',"
                                "error='MusicBrainz identifier unavailable',"
                                "pending_due_at=CASE WHEN pending_due_at>0 THEN pending_due_at "
                                "ELSE ? END WHERE id=?",
                                (
                                    time.time() + self.pending_recheck_days * 86400,
                                    submission_id,
                                ),
                            )
                else:
                    for row in db.execute(
                        "SELECT id,metadata FROM reference_tracks WHERE publication_ready=0"
                    ).fetchall():
                        try:
                            complete = metadata_complete(json.loads(row["metadata"]))
                        except (TypeError, ValueError):
                            LOGGER.warning(
                                "Cannot restore reference %s with invalid retained metadata",
                                row["id"],
                            )
                            complete = False
                        if complete:
                            db.execute(
                                "UPDATE reference_tracks SET publication_ready=1 WHERE id=?",
                                (row["id"],),
                            )
                    for (submission_id,) in db.execute(
                        "SELECT id FROM submissions WHERE state='pending_metadata'"
                    ).fetchall():
                        document = db.execute(
                            "SELECT document FROM submissions WHERE id=?", (submission_id,)
                        ).fetchone()[0]
                        try:
                            complete = metadata_complete(json.loads(document)["metadata"])
                        except (TypeError, ValueError, KeyError):
                            LOGGER.warning(
                                "Cannot restore submission %s with invalid retained metadata",
                                submission_id,
                            )
                            complete = False
                        if complete:
                            db.execute(
                                "UPDATE submissions SET state='queued',attempts=0,retry_at=0,"
                                "error=NULL,background_kind=0,pending_check=0,enqueued_at=? "
                                "WHERE id=?",
                                (time.time(), submission_id),
                            )
                db.execute(
                    "INSERT OR REPLACE INTO service_state VALUES('musicbrainz_enabled',?)",
                    (enabled,),
                )
                if self.musicbrainz_enabled:
                    db.execute(
                        "INSERT OR REPLACE INTO service_state VALUES('verification_version',?)",
                        (current_version,),
                    )
            if not self.housekeeping_enabled or not self.musicbrainz_enabled:
                db.execute(
                    "UPDATE submissions SET state='pending_metadata',pending_check=0,"
                    "background_kind=0 WHERE state='queued' AND background_kind=1"
                )

    def statistics(self) -> dict[str, Any]:
        """Count acoustic references, logical recordings and uploads by coverage and state."""
        with self.connect() as db:
            db.execute("BEGIN")
            uploads = {
                "queued": 0,
                "processing": 0,
                "processed": 0,
                "failed": 0,
                "pending_metadata": 0,
            }
            for row in db.execute("SELECT state,count(*) AS count FROM submissions GROUP BY state"):
                uploads[row["state"]] = row["count"]
            rows = db.execute(
                "WITH representatives AS ("
                "SELECT reference_id,min(id) AS id FROM submissions "
                "WHERE state='processed' GROUP BY reference_id) "
                "SELECT t.id,t.reference_key,t.recording_key,t.publication_ready,"
                "s.document,t.metadata FROM reference_tracks t "
                "LEFT JOIN representatives r ON t.reference_key=r.reference_id "
                "LEFT JOIN submissions s ON s.id=r.id"
            ).fetchall()
            return coverage_statistics([dict(row) for row in rows], uploads)

    def cached_enrichment(self, key: str) -> dict[str, Any] | None:
        """Return unexpired MusicBrainz lookup data."""
        with self.connect() as db:
            row = db.execute(
                "SELECT document FROM enrichment_cache WHERE isrc=? AND expires_at>?",
                (key, time.time()),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def cache_enrichment(self, key: str, document: dict[str, Any]) -> None:
        """Persist positive, ambiguous and empty lookups to avoid repeat requests."""
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO enrichment_cache VALUES(?,?,?)",
                (key, json.dumps(document), time.time() + MUSICBRAINZ_CACHE_TTL_DAYS * 86400),
            )

    def match(self, query: list[tuple[str, int]]) -> list[dict[str, Any]]:
        """Rank offset-consistent matches, with distinct query pairs and bounded work."""
        with self.connect() as db:
            db.execute("CREATE TEMP TABLE query(hash BLOB, offset INTEGER, idx INTEGER)")
            db.executemany(
                "INSERT INTO query VALUES(?,?,?)",
                (
                    (bytes.fromhex(digest), offset, index)
                    for index, (digest, offset) in enumerate(query)
                ),
            )
            # Keep the small query outermost instead of scanning the whole landmark collection.
            rows = db.execute(MATCH_QUERY).fetchall()
            if len(rows) > 100000:
                raise QueryTooBroadError

            def read_metadata(reference_id: str) -> str:
                return str(
                    db.execute(
                        "SELECT metadata FROM reference_tracks WHERE reference_key=?",
                        (reference_id,),
                    ).fetchone()[0]
                )

            return rank_matches(query, [dict(row) for row in rows], read_metadata)

    @staticmethod
    def _full_coverage(document: str | None, metadata: str) -> bool:
        """Check coverage, treating missing or malformed historical coverage as partial."""
        return full_coverage(document, metadata)

    def availability(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return coverage of published, verified recordings without upstream lookups."""
        mbids = {UUID(item["recording_mbid"]).hex for item in items if item.get("recording_mbid")}
        isrcs = {item["isrc"].upper() for item in items if item.get("isrc")}
        if not mbids and not isrcs:
            return [{"status": "unknown", "recording_mbid": None} for _ in items]
        clauses = []
        params: list[str] = []
        if mbids:
            clauses.append(f"t.recording_key IN ({','.join('?' for _ in mbids)})")
            params.extend(sorted(mbids))
        if isrcs:
            clauses.append(
                f"upper(json_extract(t.metadata,'$.isrc')) IN ({','.join('?' for _ in isrcs)})"
            )
            params.extend(sorted(isrcs))
        with self.connect() as db:
            rows = db.execute(
                "SELECT t.recording_key,t.metadata FROM reference_tracks t "
                "WHERE t.publication_ready=1 AND t.recording_key IS NOT NULL "
                "AND t.verification_version=? AND ("
                + " OR ".join(clauses)
                + ") ORDER BY t.id LIMIT 10000",
                [str(MUSICBRAINZ_VERIFICATION_VERSION), *params],
            ).fetchall()
        resolved = resolve_availability(items, [dict(row) for row in rows])
        keys = sorted({key for key in resolved if key is not None})
        coverage: dict[str, bool] = {key: False for key in keys}
        if keys:
            with self.connect() as db:
                variants = db.execute(
                    "WITH relevant AS (SELECT reference_key,recording_key,metadata "
                    "FROM reference_tracks WHERE publication_ready=1 AND verification_version=? "
                    f"AND recording_key IN ({','.join('?' for _ in keys)}) "
                    "ORDER BY id LIMIT 10000),"
                    "representatives AS (SELECT reference_id,min(id) AS id FROM submissions "
                    "WHERE state='processed' AND reference_id IN "
                    "(SELECT reference_key FROM relevant) GROUP BY reference_id) "
                    "SELECT t.recording_key,t.metadata,"
                    "CASE WHEN json_valid(s.document) THEN json_object("
                    "'windows',json_extract(s.document,'$.windows'),"
                    "'format_id',json_extract(s.document,'$.format_id')) END AS document "
                    "FROM relevant t LEFT JOIN representatives r ON r.reference_id=t.reference_key "
                    "LEFT JOIN submissions s ON s.id=r.id",
                    [str(MUSICBRAINZ_VERIFICATION_VERSION), *keys],
                )
                for row in variants:
                    document = row["document"]
                    if (
                        document
                        and json.loads(document).get("format_id")
                        in ("ma-dejavu-landmarks-v1", "ma-dejavu-landmarks-v2")
                        and self._full_coverage(document, row["metadata"])
                    ):
                        coverage[row["recording_key"]] = True
        return [
            {
                "status": "full" if key and coverage[key] else "partial" if key else "unknown",
                "recording_mbid": str(UUID(key)) if key else None,
            }
            for key in resolved
        ]
