"""Durable artwork migration, album selection, privacy and external failure tests."""

import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from aiohttp_client import TestClient
from test_recognition import HEADERS, PCM, matched, settings
from test_server import settings as api_settings

from fingerprint_server.artwork import ArtworkResolver, ArtworkStore, normalized
from fingerprint_server.database.sqlite import Store
from fingerprint_server.http_utils import trusted_image
from fingerprint_server.musicbrainz import MusicBrainzService
from fingerprint_server.recognition.app import create_app

REFERENCE = "b" * 64
RECORDING = str(uuid4())
GROUP = str(uuid4())
IMAGE = "https://archive.org/download/example/cover-500.jpg"


def store_at(path: Path) -> Store:
    """Create an isolated published reference without copyrighted audio."""
    store = Store(path / "artwork.db")
    store.initialize()
    queue = ArtworkStore(store)
    queue.initialize()
    with store.connect() as db:
        db.execute(
            "INSERT INTO reference_tracks(reference_key,metadata,created_at,publication_ready) "
            "VALUES(?,?,?,1)",
            (REFERENCE, json.dumps({"recording_mbid": RECORDING, "album": "Example album"}), 0),
        )
    return store


def test_additive_migration_and_queue_survive_restart(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    queue = ArtworkStore(store)
    assert queue.request("c" * 64) == {"status": "missing", "url": None}
    assert queue.request(REFERENCE) == {"status": "pending", "url": None}
    store.initialize()
    queue.initialize()
    assert queue.next()["reference_key"] == REFERENCE
    queue.finish(REFERENCE, IMAGE)
    queue.initialize()
    assert queue.request(REFERENCE) == {"status": "ready", "url": IMAGE}
    with store.connect() as db:
        db.execute("UPDATE artwork_requests SET expires_at=0")
    assert queue.request(REFERENCE)["status"] == "pending"
    queue.invalidate(REFERENCE)
    assert queue.next() is None


def test_album_edition_qualifier_does_not_prevent_representative_cover() -> None:
    assert normalized("Scream Aim Fire (Deluxe Edition)") == normalized("Scream Aim Fire")
    assert normalized("Example (Special Edition)") == normalized("Example")
    assert normalized("Life Starts Now (Deluxe Version)") == normalized("Life Starts Now")
    assert normalized("Example (Live)") != normalized("Example")
    assert normalized("Example (Acoustic Version)") != normalized("Example")


@pytest.mark.parametrize("outcome", ["cover", "missing", "outage", "ambiguous", "untrusted"])
async def test_worker_resolves_album_and_distinguishes_absence_from_failure(
    tmp_path: Path, outcome: str
) -> None:
    store = store_at(tmp_path)
    queue = ArtworkStore(store)
    queue.request(REFERENCE)
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "musicbrainz.org":
            releases = [{"title": "Example album (Deluxe Version)", "release-group": {"id": GROUP}}]
            if outcome == "ambiguous":
                releases.append({"title": "Example album", "release-group": {"id": str(uuid4())}})
            return httpx.Response(200, json={"releases": releases})
        assert request.method == "HEAD"
        assert request.url.path == f"/release-group/{GROUP}/front-500"
        if outcome == "missing":
            return httpx.Response(404)
        if outcome == "outage":
            return httpx.Response(503)
        return httpx.Response(
            307,
            headers={
                "Location": "https://untrusted.example/cover" if outcome == "untrusted" else IMAGE
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        resolver = ArtworkResolver(store, MusicBrainzService(store, api_settings(tmp_path), client))
        await resolver.process_one()
        expected = (
            "ready"
            if outcome == "cover"
            else "unavailable"
            if outcome in ("outage", "untrusted")
            else "missing"
        )
        assert queue.request(REFERENCE) == {
            "status": expected,
            "url": IMAGE if outcome == "cover" else None,
        }
        count = len(calls)
        await resolver.process_one()
        assert len(calls) == count
        if outcome in ("cover", "missing"):
            assert await resolver.cover(GROUP) == (IMAGE if outcome == "cover" else None)
            assert len(calls) == count
        if outcome == "outage":
            assert resolver.next_cover > time.monotonic() + 50


def test_facade_uses_expiring_opaque_cover_token_and_rejects_untrusted_url() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if "/artwork/" in request.url.path:
            assert request.url.path.endswith(REFERENCE)
            return httpx.Response(200, json={"status": "ready", "url": IMAGE})
        result = matched()
        result["match"]["reference_id"] = REFERENCE
        return httpx.Response(200, json=result)

    with TestClient(create_app(settings(), transport=httpx.MockTransport(upstream))) as client:
        response = client.post("/api/recognize?sample_rate=8000", content=PCM, headers=HEADERS)
        path = response.json()["match"]["artwork"]
        assert REFERENCE not in response.text
        assert client.get(path).json() == {"status": "ready", "url": IMAGE}
        assert client.get("/api/artwork/unknown").status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "http://archive.org/a",
        "https://archive.org.evil.example/a",
        "https://user:password@archive.org/a",  # pragma: allowlist secret
        "https://127.0.0.1/a",
    ],
)
def test_artwork_image_sources_are_restricted(url: str) -> None:
    assert not trusted_image(url)
