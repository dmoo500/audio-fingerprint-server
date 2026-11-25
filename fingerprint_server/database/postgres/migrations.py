"""PostgreSQL state-code and verification transitions."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Protocol

from fingerprint_server.database.codes import SUBMISSION_ERRORS, SubmissionState
from fingerprint_server.database.logic import has_identifier
from fingerprint_server.database.sqlite.migrations import MUSICBRAINZ_VERIFICATION_VERSION
from fingerprint_server.schemas import metadata_complete

if TYPE_CHECKING:
    from fingerprint_server.database.postgres import Connection

LOGGER = logging.getLogger("fingerprint_server.database.postgres")


class PostgresMigrationMixin(Protocol):
    """Upgrade submission storage and apply verification transitions."""

    schema: str
    musicbrainz_enabled: bool
    pending_recheck_days: int

    def _set_state(self, db: Connection, key: str, value: str) -> None: ...

    def _migrate_submission_codes(self, db: Connection) -> None:
        marker = db.execute(
            "SELECT value FROM service_state WHERE key='submission_codes_version'"
        ).fetchone()
        columns = {
            row["column_name"]: row["data_type"]
            for row in db.execute(
                "SELECT column_name,data_type FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='submissions'",
                (self.schema,),
            )
        }
        if marker is not None:
            if marker["value"] != "1":
                raise ValueError("Unsupported PostgreSQL submission codes version")
            if columns.get("state") != "smallint" or columns.get("state_raw") != "text":
                raise ValueError("PostgreSQL submission state layout does not match its version")
            return
        if columns.get("state") == "smallint" and columns.get("state_raw") == "text":
            self._set_state(db, "submission_codes_version", "1")
            return
        if columns.get("state") != "text" or "state_raw" in columns:
            raise ValueError("Unrecognized PostgreSQL submission state layout")
        for role in ("api", "worker"):
            acquired = db.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired",
                (f"fingerprint:{self.schema}:{role}",),
            ).fetchone()
            if acquired is None or not acquired["acquired"]:
                raise RuntimeError("Stop all PostgreSQL API and worker processes before upgrading")
        known = [state for state in SubmissionState if state != SubmissionState.UNKNOWN]
        cases = " ".join(f"WHEN '{state.name.lower()}' THEN {state.value}" for state in known)
        names = ",".join(f"'{state.name.lower()}'" for state in known)
        db.execute("ALTER TABLE submissions ADD COLUMN state_raw text")
        db.execute(f"UPDATE submissions SET state_raw=state WHERE state NOT IN ({names})")
        db.execute(
            "ALTER TABLE submissions ALTER COLUMN state TYPE smallint USING "
            f"(CASE state {cases} ELSE {SubmissionState.UNKNOWN} END)"
        )
        db.execute(f"UPDATE submissions SET error={SUBMISSION_ERRORS.encoded_sql('error')}")
        self._set_state(db, "submission_codes_version", "1")
        LOGGER.info("Upgraded PostgreSQL submission state codes in schema %s", self.schema)

    def _verification_transition(self, db: Connection) -> None:
        if self.musicbrainz_enabled:
            db.execute(
                "UPDATE reference_tracks SET publication_ready=0 WHERE verification_version!=%s",
                (MUSICBRAINZ_VERIFICATION_VERSION,),
            )
            rows = db.execute(
                "SELECT s.id,s.document,s.state,t.metadata FROM submissions s "
                "LEFT JOIN reference_tracks t ON t.reference_key=s.reference_id "
                f"WHERE s.state={SubmissionState.PROCESSED} AND (s.reference_id IS NULL "
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
                        f"UPDATE submissions SET state={SubmissionState.PENDING_METADATA},"
                        "attempts=0,retry_at=0,"
                        "error=NULL,background_kind=2,pending_check=0,"
                        "pending_due_at=%s,enqueued_at=0 "
                        "WHERE id=%s",
                        (time.time(), row["id"]),
                    )
                elif row["metadata"] is not None:
                    db.execute(
                        f"UPDATE submissions SET state={SubmissionState.PENDING_METADATA},"
                        "error=%s,pending_due_at=CASE "
                        "WHEN pending_due_at>0 THEN pending_due_at ELSE %s END WHERE id=%s",
                        (
                            SUBMISSION_ERRORS.encode("MusicBrainz identifier unavailable"),
                            time.time() + self.pending_recheck_days * 86400,
                            row["id"],
                        ),
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
                "SELECT id,document FROM submissions "
                f"WHERE state={SubmissionState.PENDING_METADATA}"
            ).fetchall():
                try:
                    complete = metadata_complete(json.loads(row["document"])["metadata"])
                except (TypeError, ValueError, KeyError):
                    LOGGER.warning("Cannot inspect retained submission %s metadata", row["id"])
                    complete = False
                if complete:
                    db.execute(
                        f"UPDATE submissions SET state={SubmissionState.QUEUED},"
                        "attempts=0,retry_at=0,error=NULL,"
                        "background_kind=0,pending_check=0,enqueued_at=%s WHERE id=%s",
                        (time.time(), row["id"]),
                    )
