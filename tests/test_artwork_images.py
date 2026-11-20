"""Verify local artwork reuse without contacting the archive."""

import asyncio
from pathlib import Path

import httpx
import pytest

from fingerprint_server import artwork_images
from fingerprint_server.artwork_images import ImageCache

URL = "https://archive.org/download/example/front.jpg"
IMAGE = b"\xff\xd8\xff" + b"example"


class ImageStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self):
        yield self.content


async def test_cache_coalesces_downloads_and_is_shared_on_disk(tmp_path: Path) -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert str(request.url) == URL
        calls += 1
        return httpx.Response(200, stream=ImageStream(IMAGE))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        cache = ImageCache(tmp_path / "images.sqlite", client)
        cache.initialize()
        peer = ImageCache(cache.path, client)
        peer.initialize()
        first, second = await asyncio.gather(cache.response(URL), cache.response(URL))
        shared = await peer.response(URL)
        assert calls == 1
        for result in (first, second, shared):
            assert result.body == IMAGE
            assert result.content_type == "image/jpeg"


@pytest.mark.parametrize("kind", ["oversized", "html", "redirect"])
async def test_cache_falls_back_without_storing_invalid_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(artwork_images, "MAX_BYTES", 32)
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert str(request.url) == URL
        calls += 1
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://archive.org/other"})
        return httpx.Response(
            200, stream=ImageStream(b"\xff\xd8\xff" * 16 if kind == "oversized" else b"<html>")
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), follow_redirects=True
    ) as client:
        cache = ImageCache(tmp_path / "images.sqlite", client)
        cache.initialize()
        for _ in range(2):
            result = await cache.response(URL)
            assert result.status == 302
            assert result.headers["Location"] == URL
        assert calls == 2


async def test_cache_expires_and_evicts_old_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artwork_images, "MAX_IMAGES", 1)
    now = 100.0
    monkeypatch.setattr(artwork_images, "time", type("Clock", (), {"time": lambda: now}))
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, stream=ImageStream(IMAGE))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        cache = ImageCache(tmp_path / "images.sqlite", client)
        cache.initialize()
        other = f"{URL}?other"
        assert (await cache.response(URL)).body == IMAGE
        now += 1
        assert (await cache.response(other)).body == IMAGE
        now += 1
        assert (await cache.response(URL)).body == IMAGE
        now += artwork_images.TTL
        assert (await cache.response(URL)).body == IMAGE
        assert calls == [URL, other, URL, URL]
