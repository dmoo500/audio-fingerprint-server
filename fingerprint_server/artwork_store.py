"""Durable artwork queue and cache."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fingerprint_server.database import Storage
from fingerprint_server.database.codes import ARTWORK_REASONS, ArtworkState, decode_artwork_state
from fingerprint_server.database.sqlite import Store
from fingerprint_server.http_utils import trusted_image

SCHEMA_VERSION = 4
QUEUE_LIMIT = 4096
PREWARM_LIMIT = 3072
PREWARM_RETRY_DELAY = 3600
TABLE = (
    "CREATE TABLE IF NOT EXISTS artwork_requests ("
    "reference_key text PRIMARY KEY, state smallint NOT NULL, state_raw text, url text, "
    "due_at double precision NOT NULL, expires_at double precision NOT NULL, reason text)"
)


class ArtworkStore:
    """Manage a bounded queue and expiring cache without changing reference metadata."""

    def __init__(self, store: Storage) -> None:
        self.store = store

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Serialize short queue mutations using the backend's existing write lock."""
        if isinstance(self.store, Store):
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        else:
            with self.store.connect(write=True) as db:
                yield db

    def sql(self, statement: str) -> str:
        """Adapt parameter placeholders for the selected backend."""
        return statement if isinstance(self.store, Store) else statement.replace("?", "%s")

    def initialize(self) -> None:
        """Install or upgrade the optional artwork cache without changing fingerprints."""
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM service_state WHERE key='artwork_schema'"
            ).fetchone()
            if row is not None and row["value"] not in ("1", "2", "3", str(SCHEMA_VERSION)):
                raise RuntimeError("Unsupported artwork schema version")
            db.execute(TABLE)
            columns = (
                {
                    item["name"]: item["type"].lower()
                    for item in db.execute("PRAGMA table_info(artwork_requests)")
                }
                if isinstance(self.store, Store)
                else {
                    item["column_name"]: item["data_type"]
                    for item in db.execute(
                        "SELECT column_name,data_type FROM information_schema.columns "
                        "WHERE table_schema=current_schema() AND table_name='artwork_requests'"
                    )
                }
            )
            if "reason" not in columns:
                db.execute("ALTER TABLE artwork_requests ADD COLUMN reason text")
            legacy = columns.get("state") == "text"
            if legacy and row is not None and row["value"] == str(SCHEMA_VERSION):
                raise RuntimeError("Artwork state layout does not match its schema version")
            if legacy and (row is None or row["value"] == "1"):
                db.execute(
                    "UPDATE artwork_requests SET expires_at=0,reason='legacy_retry' "
                    "WHERE state IN ('missing','unavailable') AND reason IS NULL"
                )
            if legacy and row is not None and row["value"] in ("1", "2"):
                db.execute(
                    "UPDATE artwork_requests SET expires_at=0,reason='edition_retry' "
                    "WHERE state='missing' AND reason='album_not_found'"
                )
            if legacy:
                self._migrate_codes(db)
            elif row is not None and row["value"] in ("1", "2"):
                if row["value"] == "1":
                    db.execute(
                        self.sql(
                            "UPDATE artwork_requests SET expires_at=0,reason=? "
                            f"WHERE state IN ({ArtworkState.MISSING},{ArtworkState.UNAVAILABLE}) "
                            "AND reason IS NULL"
                        ),
                        (ARTWORK_REASONS.encode("legacy_retry"),),
                    )
                db.execute(
                    self.sql(
                        "UPDATE artwork_requests SET expires_at=0,reason=? "
                        f"WHERE state={ArtworkState.MISSING} AND reason=?"
                    ),
                    (
                        ARTWORK_REASONS.encode("edition_retry"),
                        ARTWORK_REASONS.encode("album_not_found"),
                    ),
                )
            db.execute("CREATE INDEX IF NOT EXISTS artwork_due ON artwork_requests(state,due_at)")
            db.execute(
                "SELECT reference_key,state,state_raw,url,due_at,expires_at,reason "
                "FROM artwork_requests LIMIT 0"
            )
            db.execute(
                self.sql(
                    "INSERT INTO service_state(key,value) VALUES('artwork_schema',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                ),
                (str(SCHEMA_VERSION),),
            )

    def request(self, reference: str) -> dict[str, Any]:
        """Return cached artwork or enqueue an eligible published reference."""
        now = time.time()
        with self.connection() as db:
            track = db.execute(
                self.sql(
                    "SELECT metadata FROM reference_tracks "
                    "WHERE reference_key=? AND publication_ready=1"
                ),
                (reference,),
            ).fetchone()
            if track is None:
                return {"status": "missing", "url": None}
            metadata = json.loads(track["metadata"])
            if not metadata.get("recording_mbid") or not metadata.get("album"):
                return {"status": "missing", "url": None}
            row = db.execute(
                self.sql("SELECT * FROM artwork_requests WHERE reference_key=?"),
                (reference,),
            ).fetchone()
            if row is not None and row["expires_at"] > now:
                return {
                    "status": decode_artwork_state(row["state"], row["state_raw"]),
                    "url": row["url"],
                }
            if not self._enqueue(db, reference, now, QUEUE_LIMIT):
                return {"status": "busy", "url": None}
            return {"status": "pending", "url": None}

    def ready_urls(self, references: list[str]) -> dict[str, str]:
        """Read current trusted covers for the references on one page without enqueuing jobs."""
        if not references:
            return {}
        with self.store.connect() as db:
            installed = db.execute(
                "SELECT 1 FROM service_state WHERE key='artwork_schema'"
            ).fetchone()
            if installed is None:
                return {}
            placeholders = ",".join("?" for _ in references)
            rows = db.execute(
                self.sql(
                    "SELECT a.reference_key,a.url FROM artwork_requests a "
                    "JOIN reference_tracks t ON t.reference_key=a.reference_key "
                    f"WHERE t.publication_ready=1 AND a.state={ArtworkState.READY} "
                    "AND a.expires_at>? "
                    f"AND a.reference_key IN ({placeholders})"
                ),
                [time.time(), *references],
            ).fetchall()
        return {
            row["reference_key"]: row["url"]
            for row in rows
            if row["url"] and trusted_image(row["url"])
        }

    def next(self, *, prewarm: bool = False) -> dict[str, Any] | None:
        """Return a due request, or reserve one uncached published reference when idle."""
        now = time.time()
        sqlite = isinstance(self.store, Store)
        album = "json_extract(t.metadata,'$.album')" if sqlite else "t.metadata::jsonb->>'album'"
        mbid = (
            "json_extract(t.metadata,'$.recording_mbid')"
            if sqlite
            else "t.metadata::jsonb->>'recording_mbid'"
        )
        with self.connection() as db:
            row = db.execute(
                self.sql(
                    "SELECT a.reference_key,t.metadata FROM artwork_requests a "
                    "JOIN reference_tracks t ON t.reference_key=a.reference_key "
                    f"WHERE a.state={ArtworkState.PENDING} AND a.due_at<=? AND a.expires_at>? "
                    "AND t.publication_ready=1 "
                    "ORDER BY a.due_at LIMIT 1"
                ),
                (now, now),
            ).fetchone()
            if row is not None or not prewarm:
                return dict(row) if row is not None else None
            row = db.execute(
                self.sql(
                    "SELECT t.reference_key,t.metadata FROM reference_tracks t "
                    "LEFT JOIN artwork_requests a ON a.reference_key=t.reference_key "
                    f"WHERE t.publication_ready=1 AND trim({album})<>'' AND trim({mbid})<>'' "
                    "AND (a.reference_key IS NULL OR "
                    f"(a.expires_at<=? AND a.state<>{ArtworkState.UNAVAILABLE}) "
                    "OR a.expires_at<=?) "
                    f"ORDER BY CASE WHEN a.reason='{ARTWORK_REASONS.encode('edition_retry')}' "
                    "THEN 0 "
                    "WHEN a.reference_key IS NULL THEN 1 ELSE 2 END, "
                    "t.id DESC LIMIT 1"
                ),
                (now, now - PREWARM_RETRY_DELAY),
            ).fetchone()
            if row is not None and self._enqueue(db, row["reference_key"], now, PREWARM_LIMIT):
                return dict(row)
            return None

    def finish(
        self, reference: str, url: str | None, *, failed: bool = False, reason: str | None = None
    ) -> None:
        """Cache absence separately from transient failure, which retries on later demand."""
        with self.connection() as db:
            db.execute(
                self.sql(
                    "UPDATE artwork_requests SET state=?,state_raw=NULL,url=?,expires_at=?,"
                    "reason=? "
                    "WHERE reference_key=?"
                ),
                (
                    ArtworkState.UNAVAILABLE
                    if failed
                    else ArtworkState.READY
                    if url
                    else ArtworkState.MISSING,
                    url,
                    time.time() + (60 if failed else (30 if url else 7) * 86400),
                    ARTWORK_REASONS.encode(reason),
                    reference,
                ),
            )

    def invalidate(self, reference: str) -> None:
        """Discard one cached lookup so a corrected album association can be resolved again."""
        with self.connection() as db:
            db.execute(self.sql("DELETE FROM artwork_requests WHERE reference_key=?"), (reference,))

    def _enqueue(self, db: Any, reference: str, now: float, limit: int) -> bool:
        existing = db.execute(
            self.sql("SELECT 1 FROM artwork_requests WHERE reference_key=?"), (reference,)
        ).fetchone()
        db.execute(
            self.sql(
                "DELETE FROM artwork_requests WHERE expires_at<=? AND "
                f"(state<>{ArtworkState.UNAVAILABLE} OR expires_at<=? OR reference_key=?) AND "
                f"(reason IS NULL OR reason<>'{ARTWORK_REASONS.encode('edition_retry')}' "
                "OR reference_key=?)"
            ),
            (now, now - PREWARM_RETRY_DELAY, reference, reference),
        )
        count = db.execute("SELECT count(*) AS total FROM artwork_requests").fetchone()["total"]
        if count >= limit and existing is None:
            return False
        db.execute(
            self.sql(
                "INSERT INTO artwork_requests(reference_key,state,url,due_at,expires_at,reason) "
                f"VALUES(?,{ArtworkState.PENDING},NULL,?,?,NULL) "
                f"ON CONFLICT(reference_key) DO UPDATE SET state={ArtworkState.PENDING},"
                "state_raw=NULL,url=NULL,"
                "due_at=excluded.due_at,expires_at=excluded.expires_at,reason=NULL"
            ),
            (reference, now, now + 86400),
        )
        return True

    def _migrate_codes(self, db: Any) -> None:
        known = [state for state in ArtworkState if state != ArtworkState.UNKNOWN]
        cases = " ".join(f"WHEN '{state.name.lower()}' THEN {state.value}" for state in known)
        names = ",".join(f"'{state.name.lower()}'" for state in known)
        raw = f"CASE WHEN state IN ({names}) THEN NULL ELSE state END"
        encoded_reason = ARTWORK_REASONS.encoded_sql("reason")
        if isinstance(self.store, Store):
            db.execute(TABLE.replace("IF NOT EXISTS artwork_requests", "artwork_requests_new"))
            db.execute(
                "INSERT INTO artwork_requests_new(reference_key,state,state_raw,url,due_at,"
                "expires_at,reason) SELECT reference_key,"
                f"CASE state {cases} ELSE {ArtworkState.UNKNOWN} END,{raw},"
                f"url,due_at,expires_at,{encoded_reason} FROM artwork_requests"
            )
            db.execute("DROP TABLE artwork_requests")
            db.execute("ALTER TABLE artwork_requests_new RENAME TO artwork_requests")
            return
        for role in ("api", "worker"):
            acquired = db.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired",
                (f"fingerprint:{self.store.schema}:{role}",),
            ).fetchone()
            if acquired is None or not acquired["acquired"]:
                raise RuntimeError("Stop all PostgreSQL API and worker processes before upgrading")
        db.execute("ALTER TABLE artwork_requests ADD COLUMN state_raw text")
        db.execute(f"UPDATE artwork_requests SET state_raw={raw}")
        db.execute(
            "ALTER TABLE artwork_requests ALTER COLUMN state TYPE smallint USING "
            f"(CASE state {cases} ELSE {ArtworkState.UNKNOWN} END)"
        )
        db.execute(f"UPDATE artwork_requests SET reason={encoded_reason}")
