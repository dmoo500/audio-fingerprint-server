"""SQLite administration with serialized mutations and bounded metadata queries."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from urllib.parse import quote

import httpx

from fingerprint_server.admin.artwork import artwork_statistics
from fingerprint_server.admin.data import STATES, TEXT_FIELDS, AdminFilters, Dataset, Filter
from fingerprint_server.admin.errors import ActionFailed
from fingerprint_server.database.codes import (
    SUBMISSION_ERRORS,
    SubmissionState,
    decode_submission_state,
    decoded_submission_state_sql,
)
from fingerprint_server.database.metrics import HISTORY_HOURS, REFERENCE_COUNTS, REVIEW_COUNT
from fingerprint_server.database.notifications import notify_sqlite, track_changes
from fingerprint_server.database.sqlite import Store
from fingerprint_server.settings import Settings

LOGGER = logging.getLogger(__name__)


class AdminStore(Store):
    """Open only an existing database; never create or migrate it from the admin."""

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open an existing SQLite database with foreign keys enabled."""
        uri = f"file:{quote(str(self.path.resolve()))}?mode=rw"
        db = sqlite3.connect(uri, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.create_function("decode_submission_state", 2, decode_submission_state)
        db.create_function("lower", 1, lambda text: text.lower())
        db.create_function("strpos", 2, lambda text, part: text.find(part) + 1)
        db.create_function("starts_with", 2, lambda text, part: text.startswith(part))
        db.create_function("fp_right", 2, lambda text, length: text[-length:] if length else "")
        db.create_function(
            "regexp_replace",
            4,
            lambda text, _pattern, _replacement, _flags: "".join(
                char for char in text if char.isalnum()
            ),
        )
        changes = track_changes(db)
        try:
            with db:
                yield db
                written = db.in_transaction and bool(db.total_changes)
            if written:
                notify_sqlite(self.path, changes)
        finally:
            db.close()


class SQLiteAdminData(AdminFilters):
    """Provide the administration operations against one shared SQLite file."""

    def __init__(self, settings: Settings, health_url: str) -> None:
        self.settings = settings
        self.store = AdminStore(settings.database)
        self.health_url = health_url
        self._revision_lock = threading.Lock()
        self._revision_db: sqlite3.Connection | None = None

    def close(self) -> None:
        """Close the persistent connection used for cross-process revision tracking."""
        with self._revision_lock:
            if self._revision_db is not None:
                self._revision_db.close()
                self._revision_db = None

    def source(self, dataset: Dataset) -> str:
        """Project metadata without returning fingerprint documents."""
        if dataset == "references":
            source = (
                "SELECT reference_key AS id,publication_ready,verification_version,recording_key,"
                "created_at,metadata FROM reference_tracks"
            )
        else:
            source = (
                f"SELECT id,{decoded_submission_state_sql('state', 'state_raw')} AS state,"
                f"{SUBMISSION_ERRORS.decoded_sql('error')} AS error,"
                "attempts,pending_rechecks,created_at,retry_at,"
                "pending_due_at,reference_id,metadata FROM submission_metadata"
            )
        return (
            f"WITH metadata_entries AS MATERIALIZED ({source}) SELECT *,"
            "json_extract(metadata,'$.title') AS title,"
            "(SELECT group_concat(value, ', ') FROM json_each(metadata,'$.artists')) AS artists,"
            "json_extract(metadata,'$.album') AS album,json_extract(metadata,'$.isrc') AS isrc,"
            "json_extract(metadata,'$.recording_mbid') AS recording_mbid FROM metadata_entries"
        )

    def predicate(self, dataset: Dataset, where: Filter) -> tuple[str, list[Any]]:
        """Compile the shared filter grammar for SQLite."""
        clause, args = super().predicate(dataset, where)
        return clause.replace("%s", "?").replace("right(", "fp_right("), args

    def read(
        self,
        dataset: Dataset,
        where: Filter = None,
        *,
        skip: int = 0,
        limit: int = 50,
        order_by: list[str] | None = None,
        count: bool = False,
        pks: list[str] | None = None,
    ) -> list[dict[str, Any]] | int:
        """Read a filtered page or count without exposing stored documents."""
        if skip < 0 or skip > 1_000_000 or not 1 <= limit <= 100:
            raise ActionFailed("Page size must be 1 to 100 and offset 0 to 1000000")
        order = self.sort_clause(dataset, order_by)
        if count and dataset == "submissions" and where in (None, "") and pks is None:
            with self.store.connect() as db:
                row = db.execute("SELECT count(*) FROM submissions").fetchone()
                assert row is not None
                return int(row[0])
        clause, args = self.predicate(dataset, where)
        if pks is not None:
            if len(pks) > 100:
                raise ActionFailed("Select at most 100 entries")
            clause += " AND id IN (" + ",".join("?" for _ in pks) + ")"
            args.extend(pks)
        source = self.source(dataset)
        with self.store.connect() as db:
            if count:
                row = db.execute(
                    f"SELECT count(*) AS count FROM ({source}) entries WHERE {clause}", args
                ).fetchone()
                assert row is not None
                return int(row["count"])
            columns = (
                "publication_ready,verification_version,recording_key"
                if dataset == "references"
                else "state,error,attempts,pending_rechecks,retry_at,pending_due_at,reference_id"
            )
            native_fields = {
                "id",
                "created_at",
                "reference_id",
                "attempts",
                "pending_rechecks",
            }
            if (
                dataset == "submissions"
                and where in (None, "")
                and pks is None
                and all(
                    item.split()[0] in native_fields for item in (order_by or ["created_at desc"])
                )
            ):
                result = db.execute(
                    "WITH page AS MATERIALIZED ("
                    "SELECT id,created_at,"
                    f"{decoded_submission_state_sql('state', 'state_raw')} AS state,"
                    f"{SUBMISSION_ERRORS.decoded_sql('error')} AS error,"
                    "attempts,pending_rechecks,retry_at,"
                    "pending_due_at,reference_id,metadata "
                    "FROM submission_metadata WHERE id IN "
                    f"(SELECT id FROM submission_metadata ORDER BY {order} LIMIT ? OFFSET ?)) "
                    f"SELECT id,created_at,metadata,{columns} FROM page ORDER BY {order}",
                    (limit, skip),
                ).fetchall()
            else:
                result = db.execute(
                    f"SELECT id,created_at,metadata,{columns} FROM ({source}) entries "
                    f"WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                    [*args, limit, skip],
                ).fetchall()
        rows = [dict(row) for row in result]
        for row in rows:
            try:
                metadata = json.loads(row["metadata"])
            except (TypeError, json.JSONDecodeError) as err:
                raise ActionFailed(
                    f"Submission/reference {row['id']} has invalid metadata"
                ) from err
            if not isinstance(metadata, dict):
                raise ActionFailed(f"Submission/reference {row['id']} has invalid metadata")
            row["metadata"] = metadata
            for field in TEXT_FIELDS[1:]:
                row[field] = metadata.get(field)
            if isinstance(metadata.get("artists"), list):
                row["artists"] = ", ".join(metadata["artists"])
        return rows

    def overview(self) -> dict[str, Any]:
        """Read the database footprint, queue and worker health."""
        with self.store.connect() as db:
            uploads = dict.fromkeys(STATES, 0)
            uploads.update(
                {
                    decode_submission_state(row["state"], row["state_raw"]) or "": row["count"]
                    for row in db.execute(
                        "SELECT state,state_raw,count(*) AS count FROM submissions "
                        "GROUP BY state,state_raw"
                    )
                }
            )
            counts = db.execute(REFERENCE_COUNTS).fetchone()
            assert counts is not None
            version = db.execute("PRAGMA user_version").fetchone()[0]
            review_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='musicbrainz_reviews'"
            ).fetchone()
            review = db.execute(REVIEW_COUNT).fetchone() if review_table else None
        try:
            response = httpx.get(
                self.health_url, timeout=3, follow_redirects=False, trust_env=False
            )
            healthy = response.status_code == 200 and response.json() == {"status": "ok"}
            if not healthy:
                LOGGER.warning("SQLite worker health check returned HTTP %s", response.status_code)
        except (httpx.HTTPError, ValueError):
            LOGGER.exception("SQLite worker health check failed")
            healthy = False
        return {
            "submissions": uploads,
            "references": {"published": counts["published"], "stored": counts["stored"]},
            "searchable_recordings": counts["searchable_recordings"],
            "manual_review": review["count"] if review is not None else None,
            "database_bytes": self.store.path.stat().st_size,
            "format_version": version,
            "worker_available": healthy,
            "database_backend": "sqlite",
            "artwork": artwork_statistics(self.store),
        }

    def hourly_history(self, hour: int) -> list[dict[str, Any]]:
        """Read persisted hourly history."""
        with self.store.connect() as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_hourly_metrics'"
            ).fetchone():
                return []
            columns = {row["name"] for row in db.execute("PRAGMA table_info(admin_hourly_metrics)")}
            artwork = "cached_artwork" if "cached_artwork" in columns else "NULL AS cached_artwork"
            return [
                dict(row)
                for row in db.execute(
                    "SELECT hour,recordings,queued,processing,pending_metadata,manual_review,"
                    f"{artwork} "
                    "FROM admin_hourly_metrics WHERE hour BETWEEN ? AND ? ORDER BY hour",
                    (hour - HISTORY_HOURS + 1, hour),
                )
            ]

    def revisions(self) -> dict[str, list[int]]:
        """Track all commits by other connections without scanning large tables."""
        with self._revision_lock:
            if self._revision_db is None:
                uri = f"file:{quote(str(self.settings.database.resolve()))}?mode=rw"
                self._revision_db = sqlite3.connect(
                    uri, uri=True, check_same_thread=False, timeout=10
                )
            version = self._revision_db.execute("PRAGMA data_version").fetchone()[0]
        return {"submissions": [version], "references": [version]}

    def musicbrainz_reviews(self, skip: int = 0) -> list[dict[str, Any]]:
        """Read pending candidate reviews."""
        if skip < 0 or skip > 1_000_000:
            raise ActionFailed("Review page offset must be from 0 to 1000000")
        with self.store.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT s.id AS submission_id,s.created_at,"
                    "json_extract(s.document,'$.metadata') AS metadata,r.candidates "
                    "FROM musicbrainz_reviews r JOIN submissions s ON s.id=r.submission_id "
                    f"WHERE s.state={SubmissionState.PENDING_METADATA.value} "
                    "AND r.selected_mbid IS NULL "
                    "ORDER BY s.created_at,s.id LIMIT 51 OFFSET ?",
                    (skip,),
                )
            ]
        for row in rows:
            try:
                candidates = json.loads(row["candidates"])
                metadata = json.loads(row["metadata"])
            except (TypeError, json.JSONDecodeError) as err:
                raise ActionFailed(
                    f"MusicBrainz candidates for submission {row['submission_id']} are invalid"
                ) from err
            if not isinstance(candidates, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                for item in candidates
            ):
                raise ActionFailed(
                    f"MusicBrainz candidates for submission {row['submission_id']} are invalid"
                )
            row["candidates"] = candidates
            row["metadata"] = metadata
            row["title"] = metadata.get("title") if isinstance(metadata, dict) else None
        return rows

    def select_musicbrainz_candidate(self, submission_id: str, selected_mbid: str) -> None:
        """Select a candidate and requeue the submission atomically."""
        if not submission_id or not selected_mbid:
            raise ActionFailed("Select a MusicBrainz candidate from the pending review")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            submission = db.execute(
                "SELECT state FROM submissions WHERE id=?", (submission_id,)
            ).fetchone()
            if submission is None or submission["state"] != SubmissionState.PENDING_METADATA:
                raise ActionFailed("This submission is no longer awaiting metadata review")
            review = db.execute(
                "SELECT candidates,selected_mbid FROM musicbrainz_reviews WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            if review is None or review["selected_mbid"] is not None:
                raise ActionFailed("This MusicBrainz review is no longer pending")
            try:
                candidates = json.loads(review["candidates"])
            except (TypeError, json.JSONDecodeError) as err:
                raise ActionFailed("MusicBrainz candidates are invalid") from err
            if not isinstance(candidates, list) or not any(
                isinstance(item, dict) and item.get("id") == selected_mbid for item in candidates
            ):
                raise ActionFailed("Select one of the listed MusicBrainz candidates")
            db.execute(
                "UPDATE musicbrainz_reviews SET selected_mbid=? WHERE submission_id=?",
                (selected_mbid, submission_id),
            )
            db.execute(
                f"UPDATE submissions SET state={SubmissionState.QUEUED.value},"
                "state_raw=NULL,retry_at=0,attempts=0,error=NULL,"
                "pending_check=0,background_kind=0,enqueued_at=? WHERE id=?",
                (time.time(), submission_id),
            )
        LOGGER.info(
            "Admin selected MusicBrainz candidate submission=%s recording=%s",
            submission_id,
            selected_mbid,
        )

    def mutate(self, ids: list[str], action: Literal["retry", "delete"]) -> None:
        """Atomically act on inactive submissions while preserving references."""
        if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
            raise ActionFailed("Select 1 to 100 distinct submissions")
        placeholders = ",".join("?" for _ in ids)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                f"SELECT id,state,length(CAST(document AS BLOB)) AS bytes FROM submissions "
                f"WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            if len(rows) != len(ids):
                raise ActionFailed("A selected submission no longer exists; refresh the table")
            if any(
                row["state"] in (SubmissionState.PROCESSING, SubmissionState.QUEUED) for row in rows
            ):
                raise ActionFailed("Active or queued jobs cannot be changed; try again later")
            if action == "delete":
                db.execute(f"DELETE FROM submissions WHERE id IN ({placeholders})", ids)
            else:
                if any(row["state"] == SubmissionState.PROCESSED for row in rows):
                    raise ActionFailed("Only failed or unresolved submissions can be retried")
                added = [row for row in rows if row["state"] != SubmissionState.PENDING_METADATA]
                totals = db.execute(
                    "SELECT count(*) AS count,"
                    "coalesce(sum(length(CAST(document AS BLOB))),0) AS bytes "
                    f"FROM submissions WHERE state IN ({SubmissionState.QUEUED.value},"
                    f"{SubmissionState.PROCESSING.value},{SubmissionState.PENDING_METADATA.value})"
                ).fetchone()
                assert totals is not None
                if (
                    totals["count"] + len(added) > self.settings.queue_limit
                    or totals["bytes"] + sum(row["bytes"] for row in added)
                    > self.settings.queue_bytes_limit
                ):
                    raise ActionFailed("Queue capacity exceeded; retry after it has drained")
                db.execute(
                    f"UPDATE submissions SET state={SubmissionState.QUEUED.value},"
                    "state_raw=NULL,retry_at=0,attempts=0,error=NULL,"
                    f"pending_check=0,background_kind=0,enqueued_at=? WHERE id IN ({placeholders})",
                    (time.time(), *ids),
                )
        LOGGER.info("Admin action=%s submissions=%s", action, ",".join(ids))
