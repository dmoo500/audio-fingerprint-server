"""Persist MusicBrainz review work independently of the submitted document."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fingerprint_server.database import Storage, Store
from fingerprint_server.database.codes import SUBMISSION_ERRORS, SubmissionState


class RecordingReviews:
    """Keep ambiguous submissions available for an administrator."""

    def __init__(self, store: Storage) -> None:
        self.store = store

    @contextmanager
    def _write(self) -> Iterator[Any]:
        if isinstance(self.store, Store):
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        else:
            with self.store.connect(write=True) as db:
                yield db

    def _sql(self, statement: str) -> str:
        return statement if isinstance(self.store, Store) else statement.replace("?", "%s")

    def get(self, submission_id: str) -> dict[str, Any] | None:
        """Return the review and any selection queued for verification."""
        with self.store.connect() as db:
            row = db.execute(
                self._sql("SELECT selected_mbid FROM musicbrainz_reviews WHERE submission_id=?"),
                (submission_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def hold(
        self, submission_id: str, reason: str, candidates: list[dict[str, Any]] | None = None
    ) -> None:
        """Publish the candidates and pause the submission in one transaction."""
        with self._write() as db:
            row = db.execute(
                self._sql(
                    "UPDATE submissions SET state=?,state_raw=NULL,error=?,retry_at=0,"
                    "pending_check=0,background_kind=0 WHERE id=? AND state=? "
                    "RETURNING id"
                ),
                (
                    SubmissionState.PENDING_METADATA,
                    SUBMISSION_ERRORS.encode(reason),
                    submission_id,
                    SubmissionState.PROCESSING,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("MusicBrainz review lost its claimed submission")
            if candidates is not None:
                db.execute(
                    self._sql(
                        "INSERT INTO musicbrainz_reviews(submission_id,candidates) VALUES(?,?) "
                        "ON CONFLICT(submission_id) DO UPDATE SET "
                        "candidates=excluded.candidates,selected_mbid=NULL"
                    ),
                    (submission_id, json.dumps(candidates)),
                )
            else:
                db.execute(
                    self._sql(
                        "UPDATE musicbrainz_reviews SET selected_mbid=NULL WHERE submission_id=?"
                    ),
                    (submission_id,),
                )
