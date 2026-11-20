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
SCHEMA_VERSION = 1
QUEUE_LIMIT = 4096
PREWARM_LIMIT = 3072
PREWARM_INTERVAL = 15
PREWARM_RETRY_DELAY = 3600
RELEASE_FALLBACK_LIMIT = 3
TABLE = (
    "CREATE TABLE IF NOT EXISTS artwork_requests ("
    "reference_key text PRIMARY KEY, state text NOT NULL, url text, "
    "due_at double precision NOT NULL, expires_at double precision NOT NULL)"
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
        """Apply the additive version-one artwork migration idempotently."""
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM service_state WHERE key='artwork_schema'"
            ).fetchone()
            if row is not None and row["value"] != str(SCHEMA_VERSION):
                raise RuntimeError("Unsupported artwork schema version")
            db.execute(TABLE)
            db.execute("CREATE INDEX IF NOT EXISTS artwork_due ON artwork_requests(state,due_at)")
            db.execute(
                "SELECT reference_key,state,url,due_at,expires_at FROM artwork_requests LIMIT 0"
            )
            db.execute(
                self.sql(
                    "INSERT INTO service_state(key,value) VALUES('artwork_schema',?) "
                    "ON CONFLICT(key) DO NOTHING"
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
                    "ORDER BY CASE WHEN a.reference_key IS NULL THEN 0 ELSE 1 END, "
                    "t.id DESC LIMIT 1"
                ),
                (now, now - PREWARM_RETRY_DELAY),
            ).fetchone()
            if row is not None and self._enqueue(db, row["reference_key"], now, PREWARM_LIMIT):
                return dict(row)
            return None

    def finish(self, reference: str, url: str | None, *, failed: bool = False) -> None:
        """Cache absence separately from transient failure, which retries on later demand."""
        with self.connection() as db:
            db.execute(
                self.sql(
                    "UPDATE artwork_requests SET state=?,url=?,expires_at=? WHERE reference_key=?"
                ),
                (
                    "unavailable" if failed else "ready" if url else "missing",
                    url,
                    time.time() + (60 if failed else (30 if url else 7) * 86400),
                    reference,
                ),
            )

    def invalidate(self, reference: str) -> None:
        """Discard one cached lookup so a corrected album association can be resolved again."""
        with self.connection() as db:
            db.execute(self.sql("DELETE FROM artwork_requests WHERE reference_key=?"), (reference,))

    def _enqueue(self, db: Any, reference: str, now: float, limit: int) -> bool:
        db.execute(
            self.sql(
                "DELETE FROM artwork_requests WHERE expires_at<=? AND "
                "(state<>'unavailable' OR expires_at<=? OR reference_key=?)"
            ),
            (now, now - PREWARM_RETRY_DELAY, reference),
        )
        count = db.execute("SELECT count(*) AS total FROM artwork_requests").fetchone()["total"]
        if count >= limit:
            return False
        db.execute(
            self.sql(
                "INSERT INTO artwork_requests VALUES(?,'pending',NULL,?,?) "
                "ON CONFLICT(reference_key) DO UPDATE SET state='pending',url=NULL,"
                "due_at=excluded.due_at,expires_at=excluded.expires_at"
            ),
            (reference, now, now + 86400),
        )
        return True


def normalized(value: str) -> str:
    """Compare album names without punctuation, case or accent differences."""
    value = re.sub(
        r"\s*[\[(](?:deluxe|special|expanded|anniversary|collector'?s)"
        r"(?:\s+(?:edition|version))?[\])]\s*$",
        "",
        value,
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
            releases = result.get("releases", [])
            url = await self.album_cover(album, releases)
        except MusicBrainzUnavailable as err:
            LOGGER.warning("Artwork lookup deferred by MusicBrainz: HTTP %s", err.status)
            await run_sync(self.queue.finish, reference, None, failed=True)
            return
        except (httpx.HTTPError, EnrichmentError, ValueError, KeyError, TypeError) as err:
            LOGGER.warning("Artwork lookup failed: %s", type(err).__name__)
            await run_sync(self.queue.finish, reference, None, failed=True)
            return
        await run_sync(self.queue.finish, reference, url)

    async def album_cover(self, album: str, releases: list[dict[str, str]]) -> str | None:
        """Find a cover only when the album maps to an unambiguous release group."""
        if not album:
            return None
        matching = [release for release in releases if normalized(release["title"]) == album]
        candidates = {release["group"] for release in matching}
        candidates.update(
            release["group"]
            for release in releases
            if normalized(release.get("group_title", "")) == album
        )
        if len(candidates) != 1:
            return None
        if url := await self.cover(next(iter(candidates))):
            return url
        for release_id in sorted({release["id"] for release in matching if "id" in release})[
            :RELEASE_FALLBACK_LIMIT
        ]:
            if url := await self.cover(release_id, release=True):
                return url
        return None

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
