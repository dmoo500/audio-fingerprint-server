"""Optional shared cache for already resolved artwork."""

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import httpx
from aiohttp import web

from fingerprint_server.async_utils import run_sync
from fingerprint_server.http_utils import trusted_image

LOGGER = logging.getLogger(__name__)
MAX_BYTES = 512 * 1024
MAX_IMAGES = 256
TTL = 7 * 86400


class ImageCache:
    """Keep disposable raster images in a separate SQLite file."""

    def __init__(self, path: Path, client: httpx.AsyncClient) -> None:
        self.path = path
        self.client = client
        self.downloads = asyncio.Semaphore(3)
        self.locks = [asyncio.Lock() for _ in range(32)]

    def initialize(self) -> None:
        """Create the disposable cache if needed."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS images ("
                "key TEXT PRIMARY KEY, expires REAL NOT NULL, "
                "kind TEXT NOT NULL, body BLOB NOT NULL)"
            )

    async def response(self, url: str) -> web.Response:
        """Serve the cached image or fall back to the original public URL."""
        if not trusted_image(url):
            raise web.HTTPBadGateway(reason="Invalid artwork URL")
        key = hashlib.sha256(url.encode()).hexdigest()
        try:
            async with asyncio.timeout(8):
                async with self.locks[int(key[:2], 16) % len(self.locks)]:
                    image = await run_sync(self._read, key)
                    if image is None:
                        async with self.downloads:
                            image = await self._download(url)
                        try:
                            await run_sync(self._write, key, *image)
                        except sqlite3.Error as err:
                            LOGGER.warning("Could not cache local artwork (%s)", type(err).__name__)
        except (ValueError, httpx.HTTPError, TimeoutError, sqlite3.Error) as err:
            LOGGER.warning(
                "Local artwork delivery failed (%s); using archive URL", type(err).__name__
            )
            return web.Response(status=302, headers={"Location": url})
        kind, body = image
        return web.Response(body=body, content_type=kind)

    def _read(self, key: str) -> tuple[str, bytes] | None:
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            row = db.execute(
                "SELECT kind,body FROM images WHERE key=? AND expires>?", (key, time.time())
            ).fetchone()
        return (row[0], row[1]) if row else None

    def _write(self, key: str, kind: str, body: bytes) -> None:
        now = time.time()
        with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
            db.execute("DELETE FROM images WHERE expires<=?", (now,))
            db.execute(
                "INSERT OR REPLACE INTO images VALUES(?,?,?,?)", (key, now + TTL, kind, body)
            )
            db.execute(
                "DELETE FROM images WHERE key IN "
                "(SELECT key FROM images ORDER BY expires DESC,key LIMIT -1 OFFSET ?)",
                (MAX_IMAGES,),
            )

    async def _download(self, url: str) -> tuple[str, bytes]:
        async with self.client.stream(
            "GET", url, headers={"Accept-Encoding": "identity"}, follow_redirects=False
        ) as res:
            res.raise_for_status()
            if (
                res.status_code != 200
                or res.headers.get("content-encoding", "identity") != "identity"
            ):
                raise ValueError("Unexpected artwork response")
            body = bytearray()
            async for chunk in res.aiter_raw(chunk_size=16384):
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ValueError("Artwork exceeds the local cache limit")
        if body.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", bytes(body)
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", bytes(body)
        if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
            return "image/webp", bytes(body)
        raise ValueError("Unsupported artwork format")


IMAGE_CACHE = web.AppKey("artwork_image_cache", ImageCache)


async def image_cache_context(app: web.Application) -> AsyncIterator[None]:
    """Enable local delivery when a writable shared cache path is configured."""
    path = os.environ.get("FP_ARTWORK_IMAGE_CACHE")
    if not path:
        yield
        return
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=5, headers={"User-Agent": "MA-Fingerprint"}
    ) as client:
        cache = ImageCache(Path(path), client)
        try:
            await run_sync(cache.initialize)
        except sqlite3.Error:
            LOGGER.exception("Cannot initialize the optional artwork image cache")
        else:
            app[IMAGE_CACHE] = cache
        yield
