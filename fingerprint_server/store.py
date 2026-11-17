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

from pydantic import ValidationError

from fingerprint_server.migrations import (
    MUSICBRAINZ_VERIFICATION_VERSION,
    initialize_database,
)
from fingerprint_server.schemas import (
    acoustic_reference_id,
    decode_submission,
    metadata_complete,
)
from fingerprint_server.settings import MUSICBRAINZ_CACHE_TTL_DAYS
from fingerprint_server.storage_logic import (
    coverage_statistics,
    full_coverage,
    has_identifier,
    initial_submission_state,
    rank_matches,
    resolve_availability,
)

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


class Store:
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

    def initialize(self) -> None:
        """Upgrade storage and apply verification transitions before serving requests."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            initialize_database(db, self.path)
            db.execute("BEGIN IMMEDIATE")
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

    def submit(
        self, submission_id: str, document: str, limit: int, byte_limit: int = 256_000_000
    ) -> dict[str, Any]:
        """Acknowledge only after the submission is durably committed."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            if row is not None:
                return dict(row)
            count, queued_bytes = db.execute(
                "SELECT count(*),coalesce(sum(length(document)),0) FROM submissions "
                "WHERE state IN ('queued','processing','pending_metadata')"
            ).fetchone()
            if count >= limit or queued_bytes + len(document.encode("utf-8")) > byte_limit:
                raise QueueFullError
            state, error = initial_submission_state(
                document, self.musicbrainz_enabled, self.housekeeping_enabled
            )
            db.execute(
                "INSERT INTO submissions(id,document,state,error,created_at,pending_rechecks,"
                "pending_due_at,pending_check,enqueued_at) VALUES(?,?,?,?,?,?,?,0,?)",
                (
                    submission_id,
                    document,
                    state,
                    error,
                    time.time(),
                    0,
                    time.time() + self.pending_recheck_days * 86400
                    if state == "pending_metadata"
                    else 0,
                    time.time(),
                ),
            )
        return {"id": submission_id, "state": state}

    def get_submission(self, submission_id: str) -> dict[str, Any] | None:
        """Return a submission's processing state."""
        with self.connect() as db:
            row = db.execute(
                "SELECT id,state,attempts,error,reference_id FROM submissions WHERE id=?",
                (submission_id,),
            ).fetchone()
            return dict(row) if row else None

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

    def claim(self, pending_ids: list[str] | None = None) -> dict[str, Any] | None:
        """Atomically claim the oldest eligible queue entry."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            if pending_ids is not None:
                if not pending_ids:
                    return None
                placeholders = ",".join("?" for _ in pending_ids)
                row = db.execute(
                    "SELECT *,1 AS pending_recheck FROM submissions "
                    f"WHERE state='pending_metadata' AND id IN ({placeholders}) "
                    "ORDER BY created_at LIMIT 1",
                    pending_ids,
                ).fetchone()
            else:
                self._admit_background(db, now)
                row = db.execute(
                    "SELECT *,background_kind=1 AS pending_recheck FROM submissions "
                    "WHERE state='queued' AND retry_at<=? "
                    "ORDER BY enqueued_at,rowid LIMIT 1",
                    (now,),
                ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE submissions SET state='processing',attempts=attempts+1,"
                "pending_check=? WHERE id=?",
                (row["pending_recheck"], row["id"]),
            )
            result = dict(row)
            result["attempts"] += 1
            result["pending_recheck"] = bool(row["pending_recheck"])
            return result

    def request_pending_recheck(self) -> None:
        """Request one non-destructive pending pass by the running ingestion worker."""
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO service_state(key,value) "
                "VALUES('manual_pending_recheck','1')"
            )

    def take_pending_recheck(self) -> list[str] | None:
        """Atomically consume an operator request and snapshot pending submission IDs."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            request = db.execute(
                "SELECT value FROM service_state WHERE key='manual_pending_recheck'"
            ).fetchone()
            if request is None:
                return None
            db.execute("DELETE FROM service_state WHERE key='manual_pending_recheck'")
            return [
                row[0]
                for row in db.execute(
                    "SELECT id FROM submissions WHERE state='pending_metadata' ORDER BY created_at"
                )
            ]

    def finish(
        self,
        submission_id: str,
        reference_id: str,
        metadata: dict[str, Any],
        fingerprints: list[tuple[str, int]],
        verified: bool = False,
    ) -> None:
        """Publish a validated reference and mark queue completion in one transaction."""
        if self.musicbrainz_enabled and not verified:
            raise ValueError("MusicBrainz verification is required to publish")
        if not metadata_complete(metadata):
            self.pending_metadata(submission_id)
            return
        recording_key = None
        if verified and isinstance(metadata.get("recording_mbid"), str):
            recording_key = UUID(metadata["recording_mbid"]).hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT OR IGNORE INTO reference_tracks"
                "(reference_key,metadata,created_at,publication_ready,verification_version,"
                "recording_key) VALUES(?,?,?,1,?,?)",
                (
                    reference_id,
                    json.dumps(metadata),
                    time.time(),
                    MUSICBRAINZ_VERIFICATION_VERSION if verified else 0,
                    recording_key,
                ),
            )
            if inserted.rowcount:
                internal_id = inserted.lastrowid
                db.executemany(
                    "INSERT INTO landmarks VALUES(?,?,?)",
                    (
                        (bytes.fromhex(digest), internal_id, offset)
                        for digest, offset in fingerprints
                    ),
                )
            else:
                db.execute(
                    "UPDATE reference_tracks SET metadata=?,publication_ready=1,"
                    "verification_version=?,recording_key=coalesce(?,recording_key) "
                    "WHERE reference_key=? AND publication_ready=0",
                    (
                        json.dumps(metadata),
                        MUSICBRAINZ_VERIFICATION_VERSION if verified else 0,
                        recording_key,
                        reference_id,
                    ),
                )
                if recording_key:
                    retained = db.execute(
                        "SELECT metadata FROM reference_tracks WHERE reference_key=? "
                        "AND publication_ready=1 AND recording_key IS NULL",
                        (reference_id,),
                    ).fetchone()
                    if retained:
                        existing = json.loads(retained["metadata"])
                        identity_fields = ("title", "artists", "duration", "isrc")
                        if (
                            isinstance(existing, dict)
                            and not existing.get("recording_mbid")
                            and all(
                                existing.get(key) == metadata.get(key) for key in identity_fields
                            )
                        ):
                            existing["recording_mbid"] = metadata["recording_mbid"]
                            db.execute(
                                "UPDATE reference_tracks SET metadata=?,recording_key=? "
                                "WHERE reference_key=? AND recording_key IS NULL",
                                (json.dumps(existing), recording_key, reference_id),
                            )
            db.execute(
                "UPDATE submissions SET state='processed',reference_id=?,error=NULL,"
                "background_kind=0,pending_check=0 WHERE id=?",
                (reference_id, submission_id),
            )

    def pending_metadata(self, submission_id: str, reason: str = "Incomplete metadata") -> None:
        """Retain an incomplete document and establish its first lifecycle deadline."""
        with self.connect() as db:
            now = time.time()
            db.execute(
                "UPDATE submissions SET state='pending_metadata',error=?,retry_at=0,"
                "pending_check=0,background_kind=0,"
                "pending_due_at=CASE WHEN pending_due_at>0 THEN pending_due_at "
                "ELSE ? END WHERE id=?",
                (reason, now + self.pending_recheck_days * 86400, submission_id),
            )

    def complete_pending_check(
        self, submission_id: str, reason: str, pending_recheck: bool
    ) -> bool:
        """Record one completed unresolved lookup, deleting only after the configured limit."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT pending_rechecks FROM submissions WHERE id=?", (submission_id,)
            ).fetchone()
            if row is None:
                return False
            rechecks = int(row["pending_rechecks"])
            if pending_recheck and self.musicbrainz_enabled and self.housekeeping_enabled:
                rechecks += 1
            if (
                pending_recheck
                and self.musicbrainz_enabled
                and self.housekeeping_enabled
                and rechecks >= self.pending_max_rechecks
            ):
                db.execute("DELETE FROM submissions WHERE id=?", (submission_id,))
                return True
            db.execute(
                "UPDATE submissions SET state='pending_metadata',retry_at=0,"
                "error=?,pending_rechecks=?,pending_due_at=?,pending_check=0,"
                "background_kind=0 WHERE id=?",
                (
                    reason,
                    rechecks,
                    time.time() + self.pending_recheck_days * 86400,
                    submission_id,
                ),
            )
            return False

    def delete_submission(self, submission_id: str) -> bool:
        """Delete only the durable submission row, never its reference or landmarks."""
        with self.connect() as db:
            deleted = db.execute("DELETE FROM submissions WHERE id=?", (submission_id,)).rowcount
        return deleted > 0

    def delete_missing_identifier_submission(self, submission_id: str) -> tuple[bool, bool, int]:
        """Delete a submission and its unneeded unpublished reference, if safe."""
        with self.connect() as db:
            data_version = db.execute("PRAGMA data_version").fetchone()[0]
            submission = db.execute(
                "SELECT reference_id FROM submissions WHERE id=?", (submission_id,)
            ).fetchone()
            if submission is None:
                return False, False, 0
            reference_key = submission["reference_id"]
            reference_query = (
                "SELECT t.id,t.metadata FROM submissions s "
                "JOIN reference_tracks t ON t.reference_key=s.reference_id "
                "WHERE s.id=? AND s.reference_id=? AND t.publication_ready=0 "
                "AND NOT EXISTS (SELECT 1 FROM submissions other "
                "WHERE other.reference_id=t.reference_key AND other.id!=s.id)"
            )
            reference = db.execute(reference_query, (submission_id, reference_key)).fetchone()
            unlinked_dependency = False
            if reference is not None:
                for row in db.execute(
                    "SELECT id,document FROM submissions WHERE reference_id IS NULL AND id!=?",
                    (submission_id,),
                ):
                    try:
                        other_key = acoustic_reference_id(decode_submission(row["document"]))
                    except (ValueError, TypeError, ValidationError) as err:
                        LOGGER.warning(
                            "Cannot check unlinked submission %s for reference dependency: %s",
                            row["id"],
                            type(err).__name__,
                        )
                        unlinked_dependency = True
                        break
                    if other_key == reference_key:
                        unlinked_dependency = True
                        break
            db.execute("BEGIN IMMEDIATE")
            unchanged = db.execute("PRAGMA data_version").fetchone()[0] == data_version
            if reference is not None and not unchanged:
                db.execute(
                    "UPDATE submissions SET state='pending_metadata',pending_check=0,"
                    "background_kind=0,pending_due_at=? WHERE id=?",
                    (time.time() + self.pending_recheck_days * 86400, submission_id),
                )
                LOGGER.warning(
                    "Submission %s cleanup deferred because the database changed "
                    "during dependency checks",
                    submission_id,
                )
                return False, False, 0
            if reference is not None:
                reference = db.execute(reference_query, (submission_id, reference_key)).fetchone()
            reference_deleted = False
            landmarks_deleted = 0
            if reference and not unlinked_dependency:
                try:
                    metadata = json.loads(reference["metadata"])
                except (ValueError, TypeError):
                    LOGGER.warning("Cannot inspect metadata of reference %s", reference_key)
                else:
                    if isinstance(metadata, dict) and not has_identifier(metadata):
                        landmarks_deleted = db.execute(
                            "DELETE FROM landmarks WHERE reference_id=?", (reference["id"],)
                        ).rowcount
                        db.execute("DELETE FROM reference_tracks WHERE id=?", (reference["id"],))
                        reference_deleted = True
            deleted = db.execute("DELETE FROM submissions WHERE id=?", (submission_id,)).rowcount
        return deleted > 0, reference_deleted, landmarks_deleted

    def reference_metadata(self, reference_id: str | None) -> dict[str, Any]:
        """Return retained reference metadata for a previously published submission."""
        if reference_id is None:
            return {}
        with self.connect() as db:
            row = db.execute(
                "SELECT metadata FROM reference_tracks WHERE reference_key=?", (reference_id,)
            ).fetchone()
        if row is None:
            return {}
        try:
            result = json.loads(row[0])
        except (ValueError, TypeError) as err:
            raise ValueError("Invalid retained reference metadata") from err
        if not isinstance(result, dict):
            raise ValueError("Invalid retained reference metadata")
        return result

    def fail(self, submission_id: str, error: str, retry_seconds: int | None) -> None:
        """Record explicit terminal failure or persist a delayed retry."""
        with self.connect() as db:
            db.execute(
                "UPDATE submissions SET state=?,error=?,retry_at=?,enqueued_at=? WHERE id=?",
                (
                    "failed" if retry_seconds is None else "queued",
                    error,
                    time.time() + (retry_seconds or 0),
                    time.time(),
                    submission_id,
                ),
            )

    def defer_musicbrainz(
        self, submission_id: str, retry_seconds: float, pending_recheck: bool = False
    ) -> None:
        """Return a claimed submission to the queue without consuming an attempt."""
        with self.connect() as db:
            if pending_recheck:
                db.execute(
                    "UPDATE submissions SET state='pending_metadata',"
                    "attempts=max(0,attempts-1),pending_check=0,background_kind=0,"
                    "error='MusicBrainz service unavailable',retry_at=0,pending_due_at=? "
                    "WHERE id=? AND state='processing'",
                    (time.time() + retry_seconds, submission_id),
                )
            else:
                db.execute(
                    "UPDATE submissions SET state='queued',attempts=max(0,attempts-1),"
                    "pending_check=0,error='MusicBrainz service unavailable',retry_at=?,"
                    "enqueued_at=? "
                    "WHERE id=? AND state='processing'",
                    (time.time() + retry_seconds, time.time(), submission_id),
                )

    def defer_pending_recheck(self, submission_id: str, error: str, retry_seconds: float) -> None:
        """Delay a failed pending lookup without consuming a completed check."""
        with self.connect() as db:
            db.execute(
                "UPDATE submissions SET state='pending_metadata',pending_check=0,"
                "background_kind=0,error=?,"
                "pending_due_at=? WHERE id=? AND state='processing'",
                (error, time.time() + retry_seconds, submission_id),
            )

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

    def _admit_background(self, db: sqlite3.Connection, now: float) -> None:
        if not self.musicbrainz_enabled:
            return
        admitted = db.execute(
            "SELECT count(*) FROM submissions WHERE state IN ('queued','processing') "
            "AND background_kind!=0"
        ).fetchone()[0]
        available = max(0, self.background_recheck_queue_window - admitted)
        if not available:
            return
        rows = db.execute(
            "SELECT id,background_kind FROM submissions WHERE state='pending_metadata' "
            "AND pending_due_at<=? AND (background_kind=2 OR ?) "
            "ORDER BY pending_due_at,created_at,rowid LIMIT ?",
            (now, self.housekeeping_enabled, available),
        ).fetchall()
        tail = db.execute(
            "SELECT max(enqueued_at) FROM submissions WHERE state IN ('queued','processing')"
        ).fetchone()[0]
        enqueued_at = max(now, tail or now)
        for row in rows:
            enqueued_at += 0.000001
            kind = 2 if row["background_kind"] == 2 else 1
            db.execute(
                "UPDATE submissions SET state='queued',retry_at=0,enqueued_at=?,"
                "background_kind=?,pending_check=? WHERE id=?",
                (enqueued_at, kind, int(kind == 1), row["id"]),
            )

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
