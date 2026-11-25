"""Safe SQLite cleanup of submissions and unneeded references."""

import sqlite3
from contextlib import AbstractContextManager
from typing import Protocol

from fingerprint_server.database.codes import SubmissionState
from fingerprint_server.database.sqlite.landmarks import delete_reference_landmarks


class SQLiteHousekeepingMixin(Protocol):
    """Clean up submissions without removing referenced fingerprints."""

    pending_recheck_days: int

    def connect(self) -> AbstractContextManager[sqlite3.Connection]: ...

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
                    f"UPDATE submissions SET state={SubmissionState.PENDING_METADATA.value},"
                    "state_raw=NULL,pending_check=0,"
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
                        landmarks_deleted = delete_reference_landmarks(db, reference["id"])
                        db.execute("DELETE FROM reference_tracks WHERE id=?", (reference["id"],))
                        reference_deleted = True
            deleted = db.execute("DELETE FROM submissions WHERE id=?", (submission_id,)).rowcount
        return deleted > 0, reference_deleted, landmarks_deleted
