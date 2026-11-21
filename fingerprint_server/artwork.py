"""Durable album artwork resolved only by the ingestion worker."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from fingerprint_server.async_utils import run_sync
from fingerprint_server.database import Storage
from fingerprint_server.database.sqlite import Store
from fingerprint_server.http_utils import trusted_image
from fingerprint_server.musicbrainz import (
    EnrichmentError,
    MusicBrainzService,
    MusicBrainzUnavailable,
)

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 3
QUEUE_LIMIT = 4096
PREWARM_LIMIT = 3072
PREWARM_INTERVAL = 15
PREWARM_RETRY_DELAY = 3600
RELEASE_FALLBACK_LIMIT = 3
TABLE = (
    "CREATE TABLE IF NOT EXISTS artwork_requests ("
    "reference_key text PRIMARY KEY, state text NOT NULL, url text, "
    "due_at double precision NOT NULL, expires_at double precision NOT NULL, reason text)"
)


@dataclass(frozen=True)
class ArtworkResult:
    """Artwork URL and the reason for its selection or absence."""

    url: str | None
    reason: str


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
            if row is not None and row["value"] not in ("1", "2", str(SCHEMA_VERSION)):
                raise RuntimeError("Unsupported artwork schema version")
            db.execute(TABLE)
            columns = (
                {item["name"] for item in db.execute("PRAGMA table_info(artwork_requests)")}
                if isinstance(self.store, Store)
                else {
                    item["column_name"]
                    for item in db.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=current_schema() AND table_name='artwork_requests'"
                    )
                }
            )
            if "reason" not in columns:
                db.execute("ALTER TABLE artwork_requests ADD COLUMN reason text")
            if row is None or row["value"] == "1":
                db.execute(
                    "UPDATE artwork_requests SET expires_at=0,reason='legacy_retry' "
                    "WHERE state IN ('missing','unavailable') AND reason IS NULL"
                )
            if row is not None and row["value"] in ("1", "2"):
                db.execute(
                    "UPDATE artwork_requests SET expires_at=0,reason='edition_retry' "
                    "WHERE state='missing' AND reason='album_not_found'"
                )
            db.execute("CREATE INDEX IF NOT EXISTS artwork_due ON artwork_requests(state,due_at)")
            db.execute(
                "SELECT reference_key,state,url,due_at,expires_at,reason "
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
                return {"status": row["state"], "url": row["url"]}
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
                    "WHERE t.publication_ready=1 AND a.state='ready' AND a.expires_at>? "
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
                    "WHERE a.state='pending' AND a.due_at<=? AND a.expires_at>? "
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
                    "(a.expires_at<=? AND a.state<>'unavailable') OR a.expires_at<=?) "
                    "ORDER BY CASE WHEN a.reason='edition_retry' THEN 0 "
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
                    "UPDATE artwork_requests SET state=?,url=?,expires_at=?,reason=? "
                    "WHERE reference_key=?"
                ),
                (
                    "unavailable" if failed else "ready" if url else "missing",
                    url,
                    time.time() + (60 if failed else (30 if url else 7) * 86400),
                    reason,
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
                "(state<>'unavailable' OR expires_at<=? OR reference_key=?) AND "
                "(reason IS NULL OR reason<>'edition_retry' OR reference_key=?)"
            ),
            (now, now - PREWARM_RETRY_DELAY, reference, reference),
        )
        count = db.execute("SELECT count(*) AS total FROM artwork_requests").fetchone()["total"]
        if count >= limit and existing is None:
            return False
        db.execute(
            self.sql(
                "INSERT INTO artwork_requests(reference_key,state,url,due_at,expires_at,reason) "
                "VALUES(?,'pending',NULL,?,?,NULL) "
                "ON CONFLICT(reference_key) DO UPDATE SET state='pending',url=NULL,"
                "due_at=excluded.due_at,expires_at=excluded.expires_at,reason=NULL"
            ),
            (reference, now, now + 86400),
        )
        return True


def normalized(value: str) -> str:
    """Compare album names without punctuation, case or accent differences."""
    value = re.sub(
        r"\s*(?:[\[(](?:(?:\d+(?:st|nd|rd|th)\s+)?anniversary(?:\s+deluxe)?|"
        r"deluxe|special|expanded|collector'?s|remaster(?:ed)?|bonus track)"
        r"(?:\s+(?:edition|version))?[\])]|-\s+(?:ep|single))\s*$",
        "",
        unicodedata.normalize("NFKC", value),
        flags=re.IGNORECASE,
    )
    return "".join(c for c in unicodedata.normalize("NFKD", value).casefold() if c.isalnum())


class ArtworkResolver:
    """Resolve matching album releases using the worker's shared MusicBrainz pacing."""

    def __init__(self, store: Storage, musicbrainz: MusicBrainzService) -> None:
        self.queue = ArtworkStore(store)
        self.musicbrainz = musicbrainz
        self.next_cover = 0.0
        self.next_prewarm = 0.0

    async def process_one(self, *, prewarm: bool = False) -> None:
        """Complete one lookup, respecting archive backoff and the idle work budget."""
        now = time.monotonic()
        if self.next_cover > now or (prewarm and self.next_prewarm > now):
            return
        if prewarm:
            self.next_prewarm = now + PREWARM_INTERVAL
        job = await run_sync(self.queue.next, prewarm=prewarm)
        if job is None:
            return
        reference = job["reference_key"]
        try:
            metadata = json.loads(job["metadata"])
            mbid = str(UUID(metadata["recording_mbid"]))
            album = normalized(metadata["album"])
            result = await self.musicbrainz.lookup("artwork", mbid)
            resolved = (
                ArtworkResult(None, "recording_not_found")
                if result.get("status") == "not found"
                else await self.album_cover(
                    album,
                    result["releases"],
                    release_hint=metadata.get("release_mbid"),
                    group_hint=metadata.get("release_group_mbid"),
                    complete=result["complete"],
                )
            )
        except MusicBrainzUnavailable as err:
            LOGGER.warning("Artwork lookup deferred by MusicBrainz: HTTP %s", err.status)
            await run_sync(
                self.queue.finish, reference, None, failed=True, reason="upstream_unavailable"
            )
            return
        except (httpx.HTTPError, EnrichmentError, ValueError, KeyError, TypeError) as err:
            LOGGER.warning("Artwork lookup failed: %s", type(err).__name__)
            await run_sync(self.queue.finish, reference, None, failed=True, reason="upstream_error")
            return
        LOGGER.info("Artwork lookup completed: reference=%s reason=%s", reference, resolved.reason)
        await run_sync(
            self.queue.finish,
            reference,
            resolved.url,
            reason=resolved.reason,
            failed=resolved.reason == "incomplete_releases",
        )

    async def album_cover(
        self,
        album: str,
        releases: list[dict[str, Any]],
        *,
        release_hint: str | None = None,
        group_hint: str | None = None,
        complete: bool = True,
    ) -> ArtworkResult:
        """Find artwork using recording-associated editions or an unambiguous album group."""
        if not album:
            return ArtworkResult(None, "album_not_found")
        release_hint = str(UUID(release_hint)) if release_hint else None
        group_hint = str(UUID(group_hint)) if group_hint else None
        hinted = next((item for item in releases if item.get("id") == release_hint), None)
        if release_hint:
            if hinted is None:
                return ArtworkResult(
                    None, "release_hint_not_found" if complete else "incomplete_releases"
                )
            if group_hint and hinted["group"] != group_hint:
                return ArtworkResult(None, "conflicting_release_hint")
            group_hint = hinted["group"]
        if group_hint and not any(item["group"] == group_hint for item in releases):
            return ArtworkResult(
                None, "release_hint_not_found" if complete else "incomplete_releases"
            )
        matching = [release for release in releases if normalized(release["title"]) == album]
        candidates = {release["group"] for release in matching}
        candidates.update(
            release["group"]
            for release in releases
            if normalized(release.get("group_title", "")) == album
        )
        if not group_hint:
            if not complete:
                return ArtworkResult(None, "incomplete_releases")
            if not candidates:
                return ArtworkResult(None, "album_not_found")
            if len(candidates) != 1:
                return ArtworkResult(None, "ambiguous_album")
            group_hint = next(iter(candidates))
        if release_hint and (url := await self.cover(release_hint, release=True)):
            return ArtworkResult(url, "ready_release")
        if url := await self.cover(group_hint):
            return ArtworkResult(url, "ready_group")
        editions = {
            item["id"]: item
            for item in releases
            if item["group"] == group_hint and item.get("id") and item["id"] != release_hint
        }
        ranked = sorted(
            editions.values(),
            key=lambda item: (
                not item.get("front", False),
                item.get("status") != "Official",
                normalized(item["title"]) != album,
                item.get("date") or "9999",
                item["id"],
            ),
        )
        for edition in ranked[:RELEASE_FALLBACK_LIMIT]:
            if url := await self.cover(edition["id"], release=True):
                return ArtworkResult(url, "ready_release")
        return ArtworkResult(
            None,
            "incomplete_releases"
            if not complete
            else "search_limit"
            if len(ranked) > RELEASE_FALLBACK_LIMIT
            else "no_cover",
        )

    async def cover(self, identifier: str, *, release: bool = False) -> str | None:
        """Resolve the archive redirect without downloading the image."""
        kind = "release" if release else "release-group"
        key = f"coverart:v1:{kind}:{identifier}"
        cached = await run_sync(self.queue.store.cached_enrichment, key)
        if cached is not None:
            return cached["url"]
        await asyncio.sleep(max(0, self.next_cover - time.monotonic()))
        self.next_cover = time.monotonic() + 1.1
        response = await self.musicbrainz.client.head(
            f"https://coverartarchive.org/{kind}/{identifier}/front-500",
            headers={
                "User-Agent": f"MA-Fingerprint/0.1 "
                f"({self.musicbrainz.settings.musicbrainz_contact})"
            },
            follow_redirects=False,
        )
        if response.status_code == 404:
            await run_sync(self.queue.store.cache_enrichment, key, {"url": None})
            return None
        if response.status_code in (301, 302, 307, 308):
            url = response.headers.get("location", "")
            if trusted_image(url):
                await run_sync(self.queue.store.cache_enrichment, key, {"url": url})
                return url
            raise ValueError("Untrusted cover redirect")
        if response.status_code in (429, 503):
            retry = response.headers.get("retry-after", "60")
            delay = min(900, max(60, int(retry))) if retry.isdecimal() else 60
            self.next_cover = time.monotonic() + delay
        response.raise_for_status()
        raise ValueError("Cover archive did not return an image redirect")
