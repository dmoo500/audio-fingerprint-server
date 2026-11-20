"""Bounded PostgreSQL data access for the separate administration service."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from fingerprint_server.admin.errors import ActionFailed
from fingerprint_server.database.metrics import HISTORY_HOURS, REFERENCE_COUNTS, REVIEW_COUNT
from fingerprint_server.database.postgres import PostgresStore
from fingerprint_server.settings import Settings

LOGGER = logging.getLogger(__name__)
type Filter = dict[str, Any] | str | None
type Dataset = Literal["submissions", "unresolved", "references"]

SUBMISSIONS = """
SELECT id,state,error,attempts,pending_rechecks,created_at,retry_at,pending_due_at,reference_id,
(document::json->'metadata')::jsonb AS metadata FROM submissions
"""
REFERENCES = """
SELECT reference_key AS id,publication_ready,verification_version,recording_key,created_at,
metadata::jsonb AS metadata FROM reference_tracks
"""
TEXT_FIELDS = ("id", "title", "artists", "album", "isrc", "recording_mbid")
SUBMISSION_FIELDS = ("state", "error", "reference_id")
REFERENCE_FIELDS = ("recording_key",)
STATES = ("queued", "processing", "processed", "failed", "pending_metadata")


class AdminFilters:
    """Compile bounded, allowlisted administration filters and sorting fields."""

    def source(self, dataset: Dataset) -> str:
        """Return the metadata-only projection for a supported data set."""
        source = REFERENCES if dataset == "references" else SUBMISSIONS
        return (
            f"WITH metadata_entries AS MATERIALIZED ({source}) "
            "SELECT *,metadata->>'title' AS title,"
            "(SELECT string_agg(value, ', ') FROM "
            "jsonb_array_elements_text(metadata->'artists')) AS artists,"
            "metadata->>'album' AS album,metadata->>'isrc' AS isrc,"
            "metadata->>'recording_mbid' AS recording_mbid FROM metadata_entries"
        )

    def fields(self, dataset: Dataset) -> tuple[str, ...]:
        """Return the allowlisted searchable text columns."""
        return TEXT_FIELDS + (REFERENCE_FIELDS if dataset == "references" else SUBMISSION_FIELDS)

    def sortable_fields(self, dataset: Dataset) -> tuple[str, ...]:
        """Return text and native numeric columns available for table sorting."""
        extra = (
            ("publication_ready", "verification_version")
            if dataset == "references"
            else ("attempts", "pending_rechecks")
        )
        return (*self.fields(dataset), "created_at", *extra)

    def sort_clause(self, dataset: Dataset, order_by: list[str] | None) -> str:
        """Validate sorting and keep missing values last in either direction."""
        order = []
        for item in order_by or ["created_at desc"]:
            parts = item.split()
            if len(parts) != 2 or parts[0] not in self.sortable_fields(dataset):
                raise ActionFailed("Unsupported sorting field")
            field, direction = parts
            if direction not in ("asc", "desc"):
                raise ActionFailed("Unsupported sorting direction")
            expression = (
                f"NULLIF(lower(trim(coalesce({field}, ''))), '')"
                if field in self.fields(dataset) and field != "id"
                else field
            )
            order.append(f"{expression} {direction} NULLS LAST")
        return ",".join([*order, "id asc"])

    def predicate(self, dataset: Dataset, where: Filter) -> tuple[str, list[Any]]:
        """Validate bounded filters and return a parameterized SQL predicate."""
        fields = self.fields(dataset)
        remaining = 40

        def compile_filter(value: Filter, depth: int = 0) -> tuple[str, list[Any]]:
            nonlocal remaining
            remaining -= 1
            if remaining < 0 or depth > 5:
                raise ActionFailed("Too many filter conditions")
            if value is None or value == "":
                return "true", []
            if isinstance(value, str):
                if len(value) > 200:
                    raise ActionFailed("Search is limited to 200 characters")
                normalized_query = "regexp_replace(lower(%s),'[^[:alnum:]]','','g')"
                terms = []
                args = []
                for field in fields:
                    column = f"lower(coalesce({field},''))"
                    terms.append(
                        f"(strpos({column},lower(%s))>0 OR "
                        f"({normalized_query}!='' AND "
                        f"strpos(regexp_replace({column},'[^[:alnum:]]','','g'),"
                        f"{normalized_query})>0))"
                    )
                    args.extend([value] * 3)
                return "(" + " OR ".join(terms) + ")", args
            if not isinstance(value, dict) or len(value) != 1:
                raise ActionFailed("Invalid filter")
            field, condition = next(iter(value.items()))
            if field in ("and", "or"):
                if not isinstance(condition, list) or not 1 <= len(condition) <= 20:
                    raise ActionFailed("Invalid filter group")
                parts = [compile_filter(child, depth + 1) for child in condition]
                return (
                    "(" + f" {field.upper()} ".join(part[0] for part in parts) + ")",
                    [arg for part in parts for arg in part[1]],
                )
            if field not in fields or not isinstance(condition, dict) or len(condition) != 1:
                raise ActionFailed("Unsupported filter field")
            operator, operand = next(iter(condition.items()))
            column = f"coalesce({field},'')"
            if operator in ("is_null", "is_not_null"):
                return f"{column} {'=' if operator == 'is_null' else '!='} ''", []
            if not isinstance(operand, str) or len(operand) > 200:
                raise ActionFailed("Filter values must be text of at most 200 characters")
            if operator in ("eq", "neq"):
                return f"lower({column}) {'=' if operator == 'eq' else '!='} lower(%s)", [operand]
            negative = operator.startswith("not_")
            operation = operator.removeprefix("not_")
            if operation == "contains":
                expression = f"strpos(lower({column}),lower(%s))>0"
            elif operation == "startswith":
                expression = f"starts_with(lower({column}),lower(%s))"
            elif operation == "endswith":
                expression = f"right(lower({column}),length(%s))=lower(%s)"
            else:
                raise ActionFailed("Unsupported filter operator")
            return (
                f"NOT ({expression})" if negative else f"({expression})",
                [operand, operand] if operation == "endswith" else [operand],
            )

        clause, args = compile_filter(where)
        if dataset == "unresolved":
            clause = f"({clause}) AND state!='processed' AND coalesce(isrc,'')!=''"
        return clause, args


class AdminData(AdminFilters):
    """Read metadata and perform explicit actions without exposing fingerprint payloads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = PostgresStore(settings)

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
        """Read a bounded metadata page or its filtered count."""
        if skip < 0 or skip > 1_000_000 or not 1 <= limit <= 100:
            raise ActionFailed("Page size must be 1 to 100 and offset 0 to 1000000")
        clause, args = self.predicate(dataset, where)
        if pks is not None:
            if len(pks) > 100:
                raise ActionFailed("Select at most 100 entries")
            clause += " AND id=ANY(%s)"
            args.append(pks)
        order = self.sort_clause(dataset, order_by)
        with self.store.connect() as db:
            db.execute("SET LOCAL statement_timeout='15s'")
            source = self.source(dataset)
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
            # Project metadata once rather than parsing the large document for each UI column.
            rows = db.execute(
                f"SELECT id,created_at,metadata,{columns} FROM ({source}) entries WHERE {clause} "
                f"ORDER BY {order} LIMIT %s OFFSET %s",
                [*args, limit, skip],
            ).fetchall()
            for row in rows:
                metadata = row["metadata"]
                if not isinstance(metadata, dict):
                    raise ActionFailed(f"Submission/reference {row['id']} has invalid metadata")
                for field in TEXT_FIELDS[1:]:
                    row[field] = metadata.get(field)
                artists = metadata.get("artists")
                if isinstance(artists, list):
                    row["artists"] = ", ".join(artists)
            return rows

    def overview(self) -> dict[str, Any]:
        """Read database size and lifecycle counts without scanning landmarks."""
        with self.store.connect() as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            states = db.execute("SELECT state,count(*) AS count FROM submissions GROUP BY state")
            uploads = dict.fromkeys(STATES, 0)
            uploads.update({row["state"]: row["count"] for row in states})
            counts = db.execute(REFERENCE_COUNTS).fetchone()
            assert counts is not None
            sizes = db.execute(
                "SELECT pg_database_size(current_database()) AS database_bytes,"
                "(SELECT format_version FROM migration_info) AS format_version"
            ).fetchone()
            assert sizes is not None
            review_table = db.execute(
                "SELECT to_regclass('musicbrainz_reviews') IS NOT NULL AS installed"
            ).fetchone()
            assert review_table is not None
            review = db.execute(REVIEW_COUNT).fetchone() if review_table["installed"] else None
        return {
            "submissions": uploads,
            "references": {"published": counts["published"], "stored": counts["stored"]},
            "searchable_recordings": counts["searchable_recordings"],
            "manual_review": review["count"] if review is not None else None,
            **sizes,
            "worker_available": self.store.worker_available(),
        }

    def hourly_history(self, hour: int) -> list[dict[str, Any]]:
        """Read at most 30 days of persisted hourly samples."""
        with self.store.connect() as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            table = db.execute(
                "SELECT to_regclass('admin_hourly_metrics') IS NOT NULL AS installed"
            ).fetchone()
            assert table is not None
            if not table["installed"]:
                return []
            return db.execute(
                "SELECT hour,recordings,queued,processing,pending_metadata,manual_review "
                "FROM admin_hourly_metrics WHERE hour BETWEEN %s AND %s ORDER BY hour",
                (hour - HISTORY_HOURS + 1, hour),
            ).fetchall()

    def revisions(self) -> dict[str, list[int]]:
        """Read advisory change counters without scanning documents or landmarks."""
        with self.store.connect() as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            tracking = db.execute("SHOW track_counts").fetchone()
            assert tracking is not None
            if tracking["track_counts"] != "on":
                raise RuntimeError("Admin live updates require PostgreSQL track_counts=on")
            rows = db.execute(
                "SELECT relname,relid,n_tup_ins,n_tup_upd,n_tup_del "
                "FROM pg_stat_all_tables WHERE schemaname=current_schema() "
                "AND relname IN ('submissions','reference_tracks')"
            ).fetchall()
        names = {"submissions": "submissions", "reference_tracks": "references"}
        revisions = {
            names[row["relname"]]: [
                row["relid"],
                row["n_tup_ins"],
                row["n_tup_upd"],
                row["n_tup_del"],
            ]
            for row in rows
        }
        if len(revisions) != 2:
            raise RuntimeError("Admin live updates require existing submission/reference tables")
        return revisions

    def musicbrainz_reviews(self, skip: int = 0) -> list[dict[str, Any]]:
        """Read a bounded list of pending MusicBrainz candidate reviews."""
        if skip < 0 or skip > 1_000_000:
            raise ActionFailed("Review page offset must be from 0 to 1000000")
        with self.store.connect() as db:
            db.execute("SET LOCAL statement_timeout='15s'")
            rows = db.execute(
                "SELECT s.id AS submission_id,s.created_at,"
                "(s.document::json->'metadata')::jsonb AS metadata,r.candidates "
                "FROM musicbrainz_reviews r JOIN submissions s ON s.id=r.submission_id "
                "WHERE s.state='pending_metadata' AND r.selected_mbid IS NULL "
                "ORDER BY s.created_at,s.id LIMIT 51 OFFSET %s",
                (skip,),
            ).fetchall()
        for row in rows:
            try:
                candidates = json.loads(row["candidates"])
            except (TypeError, json.JSONDecodeError) as err:
                raise ActionFailed(
                    f"MusicBrainz candidates for submission {row['submission_id']} are invalid"
                ) from err
            if not isinstance(candidates, list) or any(
                not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str)
                for candidate in candidates
            ):
                raise ActionFailed(
                    f"MusicBrainz candidates for submission {row['submission_id']} are invalid"
                )
            row["candidates"] = candidates
            metadata = row["metadata"]
            row["title"] = metadata.get("title") if isinstance(metadata, dict) else None
        return rows

    def select_musicbrainz_candidate(self, submission_id: str, selected_mbid: str) -> None:
        """Select a listed candidate and atomically requeue its pending submission."""
        if not submission_id or not selected_mbid:
            raise ActionFailed("Select a MusicBrainz candidate from the pending review")
        with self.store.connect(write=True) as db:
            submission = db.execute(
                "SELECT id,state FROM submissions WHERE id=%s FOR UPDATE",
                (submission_id,),
            ).fetchone()
            if submission is None or submission["state"] != "pending_metadata":
                raise ActionFailed("This submission is no longer awaiting metadata review")
            review = db.execute(
                "SELECT candidates,selected_mbid FROM musicbrainz_reviews "
                "WHERE submission_id=%s FOR UPDATE",
                (submission_id,),
            ).fetchone()
            if review is None or review["selected_mbid"] is not None:
                raise ActionFailed("This MusicBrainz review is no longer pending")
            try:
                candidates = json.loads(review["candidates"])
            except (TypeError, json.JSONDecodeError) as err:
                raise ActionFailed("MusicBrainz candidates are invalid") from err
            if not isinstance(candidates, list) or not any(
                isinstance(candidate, dict)
                and isinstance(candidate.get("id"), str)
                and candidate["id"] == selected_mbid
                for candidate in candidates
            ):
                raise ActionFailed("Select one of the listed MusicBrainz candidates")
            db.execute(
                "UPDATE musicbrainz_reviews SET selected_mbid=%s WHERE submission_id=%s",
                (selected_mbid, submission_id),
            )
            db.execute(
                "UPDATE submissions SET state='queued',retry_at=0,attempts=0,error=NULL,"
                "pending_check=0,background_kind=0,enqueued_at=%s WHERE id=%s",
                (time.time(), submission_id),
            )
        LOGGER.info(
            "Admin selected MusicBrainz candidate submission=%s recording=%s",
            submission_id,
            selected_mbid,
        )

    def mutate(self, ids: list[str], action: Literal["retry", "delete"]) -> None:
        """Atomically act on inactive submissions while preserving acoustic references."""
        if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
            raise ActionFailed("Select 1 to 100 distinct submissions")
        with self.store.connect(write=True) as db:
            rows = db.execute(
                "SELECT id,state,octet_length(document) AS bytes FROM submissions "
                "WHERE id=ANY(%s) FOR UPDATE",
                (ids,),
            ).fetchall()
            if len(rows) != len(ids):
                raise ActionFailed("A selected submission no longer exists; refresh the table")
            if any(row["state"] in ("processing", "queued") for row in rows):
                raise ActionFailed("Active or queued jobs cannot be changed; try again later")
            if action == "delete":
                db.execute("DELETE FROM submissions WHERE id=ANY(%s)", (ids,))
            else:
                if any(row["state"] == "processed" for row in rows):
                    raise ActionFailed("Only failed or unresolved submissions can be retried")
                added = [row for row in rows if row["state"] == "failed"]
                totals = db.execute(
                    "SELECT count(*) AS count,coalesce(sum(octet_length(document)),0) AS bytes "
                    "FROM submissions WHERE state IN ('queued','processing','pending_metadata')"
                ).fetchone()
                assert totals is not None
                if (
                    totals["count"] + len(added) > self.settings.queue_limit
                    or totals["bytes"] + sum(row["bytes"] for row in added)
                    > self.settings.queue_bytes_limit
                ):
                    raise ActionFailed("Queue capacity exceeded; retry after it has drained")
                db.execute(
                    "UPDATE submissions SET state='queued',retry_at=0,attempts=0,error=NULL,"
                    "pending_check=0,background_kind=0,enqueued_at=%s WHERE id=ANY(%s)",
                    (time.time(), ids),
                )
        LOGGER.info("Admin action=%s submissions=%s", action, ",".join(ids))
