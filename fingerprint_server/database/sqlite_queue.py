"""Submission queue operations for sqlite storage."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Protocol


class StoreQueueMixin(Protocol):
    """Submission queue and publication operations."""

    musicbrainz_enabled: bool
    housekeeping_enabled: bool
    pending_max_rechecks: int
    pending_recheck_days: int
    background_recheck_queue_window: int

    def connect(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def submit(
        self,
        submission_id: str,
        document: str,
        limit: int,
        byte_limit: int = 256_000_000,
    ) -> dict[str, Any]:
        """Acknowledge only after the submission is durably committed."""
        from fingerprint_server.database import sqlite as _sqlite

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
                raise _sqlite.QueueFullError
            state, error = _sqlite.initial_submission_state(
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
                    _sqlite.time.time(),
                    0,
                    _sqlite.time.time() + self.pending_recheck_days * 86400
                    if state == "pending_metadata"
                    else 0,
                    _sqlite.time.time(),
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

    def claim(self, pending_ids: list[str] | None = None) -> dict[str, Any] | None:
        """Atomically claim the oldest eligible queue entry."""
        from fingerprint_server.database import sqlite as _sqlite

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _sqlite.time.time()
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

    def preview_metadata(self, pending_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Read a bounded batch of eligible metadata without claiming or changing work."""
        from fingerprint_server.database import sqlite as _sqlite

        with self.connect() as db:
            if pending_ids is not None:
                placeholders = ",".join("?" for _ in pending_ids)
                rows = db.execute(
                    "SELECT id,document FROM submissions WHERE state='pending_metadata' "
                    f"AND id IN ({placeholders}) ORDER BY created_at LIMIT 19",
                    pending_ids,
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id,document FROM submissions WHERE state='queued' AND retry_at<=? "
                    "ORDER BY enqueued_at,rowid LIMIT 19",
                    (_sqlite.time.time(),),
                ).fetchall()
            return [dict(row) for row in rows]

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
        from fingerprint_server.database import sqlite as _sqlite

        if self.musicbrainz_enabled and not verified:
            raise ValueError("MusicBrainz verification is required to publish")
        if not _sqlite.metadata_complete(metadata):
            self.pending_metadata(submission_id)
            return
        recording_key = None
        if verified and isinstance(metadata.get("recording_mbid"), str):
            recording_key = _sqlite.UUID(metadata["recording_mbid"]).hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT OR IGNORE INTO reference_tracks"
                "(reference_key,metadata,created_at,publication_ready,verification_version,"
                "recording_key) VALUES(?,?,?,1,?,?)",
                (
                    reference_id,
                    _sqlite.json.dumps(metadata),
                    _sqlite.time.time(),
                    _sqlite.MUSICBRAINZ_VERIFICATION_VERSION if verified else 0,
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
                        _sqlite.json.dumps(metadata),
                        _sqlite.MUSICBRAINZ_VERIFICATION_VERSION if verified else 0,
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
                        existing = _sqlite.json.loads(retained["metadata"])
                        if _sqlite.can_add_recording_mbid(existing, metadata):
                            existing["recording_mbid"] = metadata["recording_mbid"]
                            db.execute(
                                "UPDATE reference_tracks SET metadata=?,recording_key=? "
                                "WHERE reference_key=? AND recording_key IS NULL",
                                (_sqlite.json.dumps(existing), recording_key, reference_id),
                            )
            db.execute(
                "UPDATE submissions SET state='processed',reference_id=?,error=NULL,"
                "background_kind=0,pending_check=0 WHERE id=?",
                (reference_id, submission_id),
            )

    def pending_metadata(self, submission_id: str, reason: str = "Incomplete metadata") -> None:
        """Retain an incomplete document and establish its first lifecycle deadline."""
        from fingerprint_server.database import sqlite as _sqlite

        with self.connect() as db:
            now = _sqlite.time.time()
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
        from fingerprint_server.database import sqlite as _sqlite

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
                    _sqlite.time.time() + self.pending_recheck_days * 86400,
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
        from fingerprint_server.database import sqlite as _sqlite

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
                        other_key = _sqlite.acoustic_reference_id(
                            _sqlite.decode_submission(row["document"])
                        )
                    except (ValueError, TypeError, _sqlite.ValidationError) as err:
                        _sqlite.LOGGER.warning(
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
                    (_sqlite.time.time() + self.pending_recheck_days * 86400, submission_id),
                )
                _sqlite.LOGGER.warning(
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
                    metadata = _sqlite.json.loads(reference["metadata"])
                except (ValueError, TypeError):
                    _sqlite.LOGGER.warning("Cannot inspect metadata of reference %s", reference_key)
                else:
                    if isinstance(metadata, dict) and not _sqlite.has_identifier(metadata):
                        landmarks_deleted = db.execute(
                            "DELETE FROM landmarks WHERE reference_id=?", (reference["id"],)
                        ).rowcount
                        db.execute("DELETE FROM reference_tracks WHERE id=?", (reference["id"],))
                        reference_deleted = True
            deleted = db.execute("DELETE FROM submissions WHERE id=?", (submission_id,)).rowcount
        return deleted > 0, reference_deleted, landmarks_deleted

    def reference_metadata(self, reference_id: str | None) -> dict[str, Any]:
        """Return retained reference metadata for a previously published submission."""
        from fingerprint_server.database import sqlite as _sqlite

        if reference_id is None:
            return {}
        with self.connect() as db:
            row = db.execute(
                "SELECT metadata FROM reference_tracks WHERE reference_key=?", (reference_id,)
            ).fetchone()
        if row is None:
            return {}
        try:
            result = _sqlite.json.loads(row[0])
        except (ValueError, TypeError) as err:
            raise ValueError("Invalid retained reference metadata") from err
        if not isinstance(result, dict):
            raise ValueError("Invalid retained reference metadata")
        return result

    def fail(self, submission_id: str, error: str, retry_seconds: int | None) -> None:
        """Record explicit terminal failure or persist a delayed retry."""
        from fingerprint_server.database import sqlite as _sqlite

        with self.connect() as db:
            db.execute(
                "UPDATE submissions SET state=?,error=?,retry_at=?,enqueued_at=? WHERE id=?",
                (
                    "failed" if retry_seconds is None else "queued",
                    error,
                    _sqlite.time.time() + (retry_seconds or 0),
                    _sqlite.time.time(),
                    submission_id,
                ),
            )

    def defer_musicbrainz(
        self,
        submission_id: str,
        retry_seconds: float,
        pending_recheck: bool = False,
    ) -> None:
        """Return a claimed submission to the queue without consuming an attempt."""
        from fingerprint_server.database import sqlite as _sqlite

        with self.connect() as db:
            if pending_recheck:
                db.execute(
                    "UPDATE submissions SET state='pending_metadata',"
                    "attempts=max(0,attempts-1),pending_check=0,background_kind=0,"
                    "error='MusicBrainz service unavailable',retry_at=0,pending_due_at=? "
                    "WHERE id=? AND state='processing'",
                    (_sqlite.time.time() + retry_seconds, submission_id),
                )
            else:
                db.execute(
                    "UPDATE submissions SET state='queued',attempts=max(0,attempts-1),"
                    "pending_check=0,error='MusicBrainz service unavailable',retry_at=?,"
                    "enqueued_at=? "
                    "WHERE id=? AND state='processing'",
                    (
                        _sqlite.time.time() + retry_seconds,
                        _sqlite.time.time(),
                        submission_id,
                    ),
                )

    def defer_pending_recheck(self, submission_id: str, error: str, retry_seconds: float) -> None:
        """Delay a failed pending lookup without consuming a completed check."""
        from fingerprint_server.database import sqlite as _sqlite

        with self.connect() as db:
            db.execute(
                "UPDATE submissions SET state='pending_metadata',pending_check=0,"
                "background_kind=0,error=?,"
                "pending_due_at=? WHERE id=? AND state='processing'",
                (error, _sqlite.time.time() + retry_seconds, submission_id),
            )

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
            "AND NOT EXISTS (SELECT 1 FROM musicbrainz_reviews WHERE submission_id=submissions.id) "
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
