"""Durable SQLite queue and atomic, indexed recognition references."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from music_assistant_fingerprint import HOP_SIZE, SAMPLE_RATE

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY, document TEXT NOT NULL, state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
    error TEXT, reference_id TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS submissions_pending ON submissions(state, retry_at, created_at);
CREATE TABLE IF NOT EXISTS reference_tracks (
    id TEXT PRIMARY KEY, metadata TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS landmarks (
    hash TEXT NOT NULL, reference_id TEXT NOT NULL REFERENCES reference_tracks(id),
    offset INTEGER NOT NULL, PRIMARY KEY(hash, reference_id, offset)
);
CREATE TABLE IF NOT EXISTS enrichment_cache (
    isrc TEXT PRIMARY KEY, document TEXT NOT NULL, expires_at REAL NOT NULL
);
"""


class QueueFullError(Exception):
    """The persistent submission queue is at capacity."""


class QueryTooBroadError(Exception):
    """The query generates more candidates than can be safely evaluated."""


class Store:
    """Single-process SQLite storage; call methods outside the event loop."""

    def __init__(self, path: Path) -> None:
        self.path = path

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
        """Create only the new service's tables and recover interrupted queue work."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("UPDATE submissions SET state='queued' WHERE state='processing'")

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
                "WHERE state IN ('queued','processing')"
            ).fetchone()
            if count >= limit or queued_bytes + len(document.encode("utf-8")) > byte_limit:
                raise QueueFullError
            db.execute(
                "INSERT INTO submissions(id, document, state, created_at) VALUES(?,?,'queued',?)",
                (submission_id, document, time.time()),
            )
        return {"id": submission_id, "state": "queued"}

    def get_submission(self, submission_id: str) -> dict[str, Any] | None:
        """Return a submission's processing state."""
        with self.connect() as db:
            row = db.execute(
                "SELECT id,state,attempts,error,reference_id FROM submissions WHERE id=?",
                (submission_id,),
            ).fetchone()
            return dict(row) if row else None

    def statistics(self) -> dict[str, Any]:
        """Count published acoustic references by coverage and uploads by queue state."""
        with self.connect() as db:
            db.execute("BEGIN")
            uploads = {"queued": 0, "processing": 0, "processed": 0, "failed": 0}
            for row in db.execute("SELECT state,count(*) AS count FROM submissions GROUP BY state"):
                uploads[row["state"]] = row["count"]
            coverage = {"total": 0, "partial": 0, "full": 0}
            rows = db.execute(
                "WITH representatives AS ("
                "SELECT reference_id,min(id) AS id FROM submissions "
                "WHERE state='processed' GROUP BY reference_id) "
                "SELECT json_extract(s.document,'$.windows') AS windows,"
                "json_extract(t.metadata,'$.duration') AS duration "
                "FROM representatives r JOIN submissions s ON s.id=r.id "
                "JOIN reference_tracks t ON t.id=r.reference_id"
            )
            for row in rows:
                windows = json.loads(row["windows"])
                end = 0
                contiguous = True
                for window in windows:
                    if window["start_sample"] > end:
                        contiguous = False
                    end = max(end, window["start_sample"] + window["sample_count"])
                full = contiguous and end == round(row["duration"] * SAMPLE_RATE)
                coverage["full" if full else "partial"] += 1
                coverage["total"] += 1
            return {"references": coverage, "submissions": uploads}

    def claim(self) -> dict[str, Any] | None:
        """Atomically claim the oldest eligible queue entry."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM submissions WHERE state='queued' AND retry_at<=? "
                "ORDER BY created_at LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE submissions SET state='processing', attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
            result = dict(row)
            result["attempts"] += 1
            return result

    def finish(
        self,
        submission_id: str,
        reference_id: str,
        metadata: dict[str, Any],
        fingerprints: list[tuple[str, int]],
    ) -> None:
        """Publish a validated reference and mark queue completion in one transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT OR IGNORE INTO reference_tracks VALUES(?,?,?)",
                (reference_id, json.dumps(metadata), time.time()),
            )
            if inserted.rowcount:
                db.executemany(
                    "INSERT INTO landmarks VALUES(?,?,?)",
                    ((digest, reference_id, offset) for digest, offset in fingerprints),
                )
            db.execute(
                "UPDATE submissions SET state='processed',reference_id=?,error=NULL WHERE id=?",
                (reference_id, submission_id),
            )

    def fail(self, submission_id: str, error: str, retry_seconds: int | None) -> None:
        """Record explicit terminal failure or persist a delayed retry."""
        with self.connect() as db:
            db.execute(
                "UPDATE submissions SET state=?,error=?,retry_at=? WHERE id=?",
                (
                    "failed" if retry_seconds is None else "queued",
                    error,
                    time.time() + (retry_seconds or 0),
                    submission_id,
                ),
            )

    def cached_enrichment(self, isrc: str) -> dict[str, Any] | None:
        """Return unexpired MusicBrainz lookup data."""
        with self.connect() as db:
            row = db.execute(
                "SELECT document FROM enrichment_cache WHERE isrc=? AND expires_at>?",
                (isrc, time.time()),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def cache_enrichment(self, isrc: str, document: dict[str, Any]) -> None:
        """Persist positive, ambiguous and empty lookups to avoid repeat requests."""
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO enrichment_cache VALUES(?,?,?)",
                (isrc, json.dumps(document), time.time() + 7 * 86400),
            )

    def match(self, query: list[tuple[str, int]]) -> list[dict[str, Any]]:
        """Rank offset-consistent matches, with distinct query pairs and bounded work."""
        with self.connect() as db:
            db.execute("CREATE TEMP TABLE query(hash TEXT, offset INTEGER, idx INTEGER)")
            db.executemany(
                "INSERT INTO query VALUES(?,?,?)",
                ((digest, offset, index) for index, (digest, offset) in enumerate(query)),
            )
            rows = db.execute(
                "SELECT l.reference_id,l.offset-q.offset AS delta,q.idx,q.offset "
                "FROM query q JOIN landmarks l ON l.hash=q.hash LIMIT 100001"
            ).fetchall()
            if len(rows) > 100000:
                raise QueryTooBroadError
            groups: dict[tuple[str, int], set[int]] = defaultdict(set)
            for row in rows:
                groups[(row["reference_id"], row["delta"])].add(row["idx"])
            best: dict[str, tuple[int, set[int]]] = {}
            for (reference_id, delta), indices in groups.items():
                if reference_id not in best or len(indices) > len(best[reference_id][1]):
                    best[reference_id] = (delta, indices)
            ranked = sorted(best.items(), key=lambda entry: (-len(entry[1][1]), entry[0]))
            results = []
            accepted: list[tuple[int, set[int], dict[str, Any]]] = []
            for reference_id, (delta, indices) in ranked:
                offsets = [query[index][1] for index in indices]
                raw_metadata = db.execute(
                    "SELECT metadata FROM reference_tracks WHERE id=?", (reference_id,)
                ).fetchone()[0]
                metadata = json.loads(raw_metadata)
                # Old sampled and new full references can describe the same aligned recording.
                identified = (
                    metadata.get("recording_mbid")
                    or metadata.get("isrc")
                    or (metadata.get("title") and metadata.get("artists"))
                )
                if identified and any(
                    other_delta == delta
                    and other_metadata == metadata
                    and len(indices & other_indices) >= 0.9 * min(len(indices), len(other_indices))
                    for other_delta, other_indices, other_metadata in accepted
                ):
                    continue
                accepted.append((delta, indices, metadata))
                results.append(
                    {
                        "reference_id": reference_id,
                        "matched_landmarks": len(indices),
                        "query_landmarks": len(query),
                        "match_fraction": len(indices) / len(query),
                        "offset_seconds": delta * HOP_SIZE / SAMPLE_RATE,
                        "matched_span_seconds": (max(offsets) - min(offsets))
                        * HOP_SIZE
                        / SAMPLE_RATE,
                        "metadata": metadata,
                    }
                )
                if len(results) == 2:
                    break
            return results
