"""Submission queue operations for postgres storage."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, Protocol

from fingerprint_server.database.codes import (
    SUBMISSION_ERRORS,
    SubmissionState,
    decode_submission_state,
    encode_submission_state,
)

if TYPE_CHECKING:
    from fingerprint_server.database.postgres import Connection


class PostgresStoreQueueMixin(Protocol):
    """Submission queue and publication operations."""

    musicbrainz_enabled: bool
    housekeeping_enabled: bool
    pending_max_rechecks: int
    pending_recheck_days: int
    background_recheck_queue_window: int
    _format_version: int

    def connect(self, *, write: bool = False) -> AbstractContextManager[Connection]: ...

    def _one(self, db: Connection, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any]: ...

    def _set_state(self, db: Connection, key: str, value: str) -> None: ...

    def submit(
        self,
        submission_id: str,
        document: str,
        limit: int,
        byte_limit: int = 256_000_000,
    ) -> dict[str, Any]:
        """Acknowledge only committed submissions within the shared queue budget."""
        from fingerprint_server.database import postgres as _postgres

        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM submissions WHERE id=%s", (submission_id,)).fetchone()
            if row is not None:
                row["state"] = decode_submission_state(row["state"], row.pop("state_raw"))
                row["error"] = SUBMISSION_ERRORS.decode(row["error"])
                return row
            totals = self._one(
                db,
                "SELECT count(*) AS count,coalesce(sum(octet_length(document)),0) AS bytes "
                f"FROM submissions WHERE state IN ({SubmissionState.QUEUED},"
                f"{SubmissionState.PROCESSING},{SubmissionState.PENDING_METADATA})",
            )
            if totals["count"] >= limit or totals["bytes"] + len(document.encode()) > byte_limit:
                raise _postgres.QueueFullError
            state, error = _postgres.initial_submission_state(
                document, self.musicbrainz_enabled, self.housekeeping_enabled
            )
            now = _postgres.time.time()
            db.execute(
                "INSERT INTO submissions(id,document,state,error,created_at,pending_due_at,"
                "enqueued_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    submission_id,
                    document,
                    encode_submission_state(state),
                    SUBMISSION_ERRORS.encode(error),
                    now,
                    now + self.pending_recheck_days * 86400 if state == "pending_metadata" else 0,
                    now,
                ),
            )
        return {"id": submission_id, "state": state}

    def get_submission(self, submission_id: str) -> dict[str, Any] | None:
        """Return a submission's processing state."""
        with self.connect() as db:
            row = db.execute(
                "SELECT id,state,state_raw,attempts,error,reference_id FROM submissions "
                "WHERE id=%s",
                (submission_id,),
            ).fetchone()
            if row is not None:
                row["state"] = decode_submission_state(row["state"], row.pop("state_raw"))
                row["error"] = SUBMISSION_ERRORS.decode(row["error"])
            return row

    def claim(self, pending_ids: list[str] | None = None) -> dict[str, Any] | None:
        """Claim one eligible entry with a stable FIFO tie-breaker."""
        from fingerprint_server.database import postgres as _postgres

        with self.connect(write=True) as db:
            if pending_ids is not None:
                row = db.execute(
                    "SELECT *,1 AS pending_recheck FROM submissions "
                    f"WHERE state={SubmissionState.PENDING_METADATA} "
                    "AND id=ANY(%s) ORDER BY created_at LIMIT 1",
                    (pending_ids,),
                ).fetchone()
            else:
                now = _postgres.time.time()
                self._admit_background(db, now)
                row = db.execute(
                    "SELECT *,(background_kind=1)::integer AS pending_recheck FROM submissions "
                    f"WHERE state={SubmissionState.QUEUED} AND retry_at<=%s "
                    "ORDER BY enqueued_at,queue_order LIMIT 1",
                    (now,),
                ).fetchone()
            if row is None:
                return None
            db.execute(
                f"UPDATE submissions SET state={SubmissionState.PROCESSING},"
                "attempts=attempts+1,pending_check=%s "
                "WHERE id=%s",
                (row["pending_recheck"], row["id"]),
            )
            row["attempts"] += 1
            row["state"] = decode_submission_state(row["state"], row.pop("state_raw"))
            row["error"] = SUBMISSION_ERRORS.decode(row["error"])
            row["pending_recheck"] = bool(row["pending_recheck"])
            return row

    def preview_metadata(self, pending_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Read a bounded batch of eligible metadata without claiming or changing work."""
        from fingerprint_server.database import postgres as _postgres

        with self.connect() as db:
            if pending_ids is not None:
                return db.execute(
                    "SELECT id,document FROM submissions "
                    f"WHERE state={SubmissionState.PENDING_METADATA} "
                    "AND id=ANY(%s) ORDER BY created_at LIMIT 19",
                    (pending_ids,),
                ).fetchall()
            return db.execute(
                f"SELECT id,document FROM submissions WHERE state={SubmissionState.QUEUED} "
                "AND retry_at<=%s "
                "ORDER BY enqueued_at,queue_order LIMIT 19",
                (_postgres.time.time(),),
            ).fetchall()

    def request_pending_recheck(self) -> None:
        """Persist a non-destructive operator request."""
        with self.connect(write=True) as db:
            self._set_state(db, "manual_pending_recheck", "1")

    def take_pending_recheck(self) -> list[str] | None:
        """Consume an operator request and snapshot the pending IDs."""
        with self.connect(write=True) as db:
            row = db.execute(
                "DELETE FROM service_state WHERE key='manual_pending_recheck' RETURNING value"
            ).fetchone()
            if row is None:
                return None
            return [
                row["id"]
                for row in db.execute(
                    f"SELECT id FROM submissions WHERE state={SubmissionState.PENDING_METADATA} "
                    "ORDER BY created_at"
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
        """Publish reference, landmarks and submission completion atomically."""
        from fingerprint_server.database import postgres as _postgres

        if self.musicbrainz_enabled and not verified:
            raise ValueError("MusicBrainz verification is required to publish")
        if not _postgres.metadata_complete(metadata):
            self.pending_metadata(submission_id)
            return
        recording_key = (
            _postgres.UUID(metadata["recording_mbid"]).hex
            if verified and isinstance(metadata.get("recording_mbid"), str)
            else None
        )
        with self.connect(write=True) as db:
            inserted = db.execute(
                "INSERT INTO reference_tracks(reference_key,metadata,created_at,"
                "publication_ready,verification_version,recording_key) VALUES(%s,%s,%s,1,%s,%s) "
                "ON CONFLICT(reference_key) DO NOTHING RETURNING id",
                (
                    reference_id,
                    _postgres.json.dumps(metadata),
                    _postgres.time.time(),
                    _postgres.MUSICBRAINZ_VERIFICATION_VERSION if verified else 0,
                    recording_key,
                ),
            ).fetchone()
            if inserted:
                if self._format_version == _postgres.postgres_postings.POSTINGS_VERSION:
                    _postgres.postgres_postings.insert(db, inserted["id"], fingerprints)
                else:
                    with db.cursor().copy(
                        'COPY landmarks(hash,reference_id,"offset") FROM STDIN'
                    ) as cp:
                        for digest, offset in fingerprints:
                            cp.write_row((bytes.fromhex(digest), inserted["id"], offset))
            else:
                db.execute(
                    "UPDATE reference_tracks SET metadata=%s,publication_ready=1,"
                    "verification_version=%s,recording_key=coalesce(%s,recording_key) "
                    "WHERE reference_key=%s AND publication_ready=0",
                    (
                        _postgres.json.dumps(metadata),
                        _postgres.MUSICBRAINZ_VERIFICATION_VERSION if verified else 0,
                        recording_key,
                        reference_id,
                    ),
                )
                if recording_key:
                    retained = db.execute(
                        "SELECT metadata FROM reference_tracks WHERE reference_key=%s "
                        "AND publication_ready=1 AND recording_key IS NULL",
                        (reference_id,),
                    ).fetchone()
                    if retained:
                        existing = _postgres.json.loads(retained["metadata"])
                        if _postgres.can_add_recording_mbid(existing, metadata):
                            existing["recording_mbid"] = metadata["recording_mbid"]
                            db.execute(
                                "UPDATE reference_tracks SET metadata=%s,recording_key=%s "
                                "WHERE reference_key=%s AND recording_key IS NULL",
                                (_postgres.json.dumps(existing), recording_key, reference_id),
                            )
            db.execute(
                f"UPDATE submissions SET state={SubmissionState.PROCESSED},"
                "reference_id=%s,error=NULL,"
                "background_kind=0,pending_check=0 WHERE id=%s",
                (reference_id, submission_id),
            )

    def pending_metadata(self, submission_id: str, reason: str = "Incomplete metadata") -> None:
        """Retain incomplete metadata with its original lifecycle deadline."""
        from fingerprint_server.database import postgres as _postgres

        with self.connect(write=True) as db:
            db.execute(
                f"UPDATE submissions SET state={SubmissionState.PENDING_METADATA},"
                "error=%s,retry_at=0,"
                "pending_check=0,background_kind=0,pending_due_at=CASE WHEN pending_due_at>0 "
                "THEN pending_due_at ELSE %s END WHERE id=%s",
                (
                    SUBMISSION_ERRORS.encode(reason),
                    _postgres.time.time() + self.pending_recheck_days * 86400,
                    submission_id,
                ),
            )

    def complete_pending_check(
        self, submission_id: str, reason: str, pending_recheck: bool
    ) -> bool:
        """Count completed housekeeping checks, deleting only exhausted submissions."""
        from fingerprint_server.database import postgres as _postgres

        with self.connect(write=True) as db:
            row = db.execute(
                "SELECT pending_rechecks FROM submissions WHERE id=%s", (submission_id,)
            ).fetchone()
            if row is None:
                return False
            automatic = pending_recheck and self.musicbrainz_enabled and self.housekeeping_enabled
            rechecks = int(row["pending_rechecks"]) + int(automatic)
            if automatic and rechecks >= self.pending_max_rechecks:
                db.execute("DELETE FROM submissions WHERE id=%s", (submission_id,))
                return True
            db.execute(
                f"UPDATE submissions SET state={SubmissionState.PENDING_METADATA},"
                "retry_at=0,error=%s,"
                "pending_rechecks=%s,pending_due_at=%s,pending_check=0,background_kind=0 "
                "WHERE id=%s",
                (
                    SUBMISSION_ERRORS.encode(reason),
                    rechecks,
                    _postgres.time.time() + self.pending_recheck_days * 86400,
                    submission_id,
                ),
            )
            return False

    def delete_submission(self, submission_id: str) -> bool:
        """Delete a submission without deleting acoustic data."""
        with self.connect(write=True) as db:
            return db.execute("DELETE FROM submissions WHERE id=%s", (submission_id,)).rowcount > 0

    def delete_missing_identifier_submission(self, submission_id: str) -> tuple[bool, bool, int]:
        """Remove unused unpublished acoustic data under the shared mutation lock."""
        from fingerprint_server.database import postgres as _postgres

        with self.connect(write=True) as db:
            reference = db.execute(
                "SELECT t.id,t.reference_key,t.metadata FROM submissions s "
                "JOIN reference_tracks t ON t.reference_key=s.reference_id "
                "WHERE s.id=%s AND t.publication_ready=0 AND NOT EXISTS "
                "(SELECT 1 FROM submissions other WHERE other.reference_id=t.reference_key "
                "AND other.id!=s.id)",
                (submission_id,),
            ).fetchone()
            safe = reference is not None
            if reference is not None:
                for row in db.execute(
                    "SELECT id,document FROM submissions WHERE reference_id IS NULL AND id!=%s",
                    (submission_id,),
                ):
                    try:
                        key = _postgres.acoustic_reference_id(
                            _postgres.decode_submission(row["document"])
                        )
                    except (ValueError, TypeError, _postgres.ValidationError) as err:
                        _postgres.LOGGER.warning(
                            "Cannot check unlinked submission %s: %s", row["id"], type(err).__name__
                        )
                        safe = False
                        break
                    if key == reference["reference_key"]:
                        safe = False
                        break
                try:
                    metadata = _postgres.json.loads(reference["metadata"])
                except (ValueError, TypeError):
                    _postgres.LOGGER.warning(
                        "Cannot inspect reference %s metadata", reference["reference_key"]
                    )
                    safe = False
                else:
                    safe &= isinstance(metadata, dict) and not _postgres.has_identifier(metadata)
            landmarks = 0
            if safe and reference is not None:
                if self._format_version == _postgres.postgres_postings.POSTINGS_VERSION:
                    landmarks = _postgres.postgres_postings.delete(db, reference["id"])
                else:
                    landmarks = db.execute(
                        "DELETE FROM landmarks WHERE reference_id=%s", (reference["id"],)
                    ).rowcount
                db.execute("DELETE FROM reference_tracks WHERE id=%s", (reference["id"],))
            deleted = db.execute("DELETE FROM submissions WHERE id=%s", (submission_id,)).rowcount
            return deleted > 0, safe, landmarks

    def reference_metadata(self, reference_id: str | None) -> dict[str, Any]:
        """Return retained reference metadata for verification."""
        from fingerprint_server.database import postgres as _postgres

        if reference_id is None:
            return {}
        with self.connect() as db:
            row = db.execute(
                "SELECT metadata FROM reference_tracks WHERE reference_key=%s", (reference_id,)
            ).fetchone()
        if row is None:
            return {}
        result = _postgres.json.loads(row["metadata"])
        if not isinstance(result, dict):
            raise ValueError("Invalid retained reference metadata")
        return result

    def fail(self, submission_id: str, error: str, retry_seconds: int | None) -> None:
        """Persist a terminal failure or delayed retry."""
        from fingerprint_server.database import postgres as _postgres

        with self.connect(write=True) as db:
            db.execute(
                "UPDATE submissions SET state=%s,error=%s,retry_at=%s,enqueued_at=%s WHERE id=%s",
                (
                    SubmissionState.FAILED if retry_seconds is None else SubmissionState.QUEUED,
                    SUBMISSION_ERRORS.encode(error),
                    _postgres.time.time() + (retry_seconds or 0),
                    _postgres.time.time(),
                    submission_id,
                ),
            )

    def defer_musicbrainz(
        self,
        submission_id: str,
        retry_seconds: float,
        pending_recheck: bool = False,
    ) -> None:
        """Requeue a MusicBrainz outage without consuming a completed attempt."""
        from fingerprint_server.database import postgres as _postgres

        with self.connect(write=True) as db:
            if pending_recheck:
                db.execute(
                    f"UPDATE submissions SET state={SubmissionState.PENDING_METADATA},"
                    "attempts=greatest(0,attempts-1),pending_check=0,background_kind=0,"
                    "error=%s,retry_at=0,pending_due_at=%s "
                    f"WHERE id=%s AND state={SubmissionState.PROCESSING}",
                    (
                        SUBMISSION_ERRORS.encode("MusicBrainz service unavailable"),
                        _postgres.time.time() + retry_seconds,
                        submission_id,
                    ),
                )
            else:
                db.execute(
                    f"UPDATE submissions SET state={SubmissionState.QUEUED},"
                    "attempts=greatest(0,attempts-1),pending_check=0,error=%s,retry_at=%s,"
                    f"enqueued_at=%s WHERE id=%s AND state={SubmissionState.PROCESSING}",
                    (
                        SUBMISSION_ERRORS.encode("MusicBrainz service unavailable"),
                        _postgres.time.time() + retry_seconds,
                        _postgres.time.time(),
                        submission_id,
                    ),
                )

    def defer_pending_recheck(self, submission_id: str, error: str, retry_seconds: float) -> None:
        """Delay a pending lookup without changing its completed-check count."""
        from fingerprint_server.database import postgres as _postgres

        with self.connect(write=True) as db:
            db.execute(
                f"UPDATE submissions SET state={SubmissionState.PENDING_METADATA},pending_check=0,"
                "background_kind=0,error=%s,pending_due_at=%s "
                f"WHERE id=%s AND state={SubmissionState.PROCESSING}",
                (
                    SUBMISSION_ERRORS.encode(error),
                    _postgres.time.time() + retry_seconds,
                    submission_id,
                ),
            )

    def _admit_background(self, db: Connection, now: float) -> None:
        if not self.musicbrainz_enabled:
            return
        admitted = self._one(
            db,
            f"SELECT count(*) AS count FROM submissions WHERE state IN "
            f"({SubmissionState.QUEUED},{SubmissionState.PROCESSING}) "
            "AND background_kind!=0",
        )["count"]
        available = max(0, self.background_recheck_queue_window - admitted)
        if not available:
            return
        rows = db.execute(
            "SELECT id,background_kind FROM submissions "
            f"WHERE state={SubmissionState.PENDING_METADATA} "
            "AND pending_due_at<=%s AND (background_kind=2 OR %s) "
            "AND NOT EXISTS (SELECT 1 FROM musicbrainz_reviews WHERE submission_id=submissions.id) "
            "ORDER BY pending_due_at,created_at,queue_order LIMIT %s",
            (now, self.housekeeping_enabled, available),
        ).fetchall()
        tail = self._one(
            db,
            "SELECT max(enqueued_at) AS tail FROM submissions "
            f"WHERE state IN ({SubmissionState.QUEUED},{SubmissionState.PROCESSING})",
        )["tail"]
        enqueued_at = max(now, tail or now)
        for row in rows:
            enqueued_at += 0.000001
            kind = 2 if row["background_kind"] == 2 else 1
            db.execute(
                f"UPDATE submissions SET state={SubmissionState.QUEUED},retry_at=0,enqueued_at=%s,"
                "background_kind=%s,pending_check=%s WHERE id=%s",
                (enqueued_at, kind, int(kind == 1), row["id"]),
            )
