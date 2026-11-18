"""Availability only exposes verified, published recording coverage."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from audio_fingerprint import FORMAT_ID, SAMPLE_RATE
from pydantic import SecretStr
from test_server import READ, UPLOAD, settings

from fingerprint_server.app import create_app
from fingerprint_server.database.sqlite import Store
from fingerprint_server.musicbrainz import MusicBrainzService
from fingerprint_server.schemas import AvailabilityResponse

MBID = "e9c2a2a3-4f4b-4b6a-9c31-3a8b2f3c7f10"
OTHER = "a61ad03c-8df0-44bf-b999-25d9997d2a24"
APPLICATION = "test-application-key-with-at-least-24-characters"


def publish(store: Store, identifier: str, mbid: str | None, seconds: int) -> None:
    metadata = {
        "duration": 2,
        "title": "Synthetic recording",
        "artists": ["Test artist"],
        "album": "Test album",
        "recording_mbid": mbid,
        "isrc": "USAAA2600001",
    }
    doc = {
        "format_id": FORMAT_ID,
        "windows": [{"start_sample": 0, "sample_count": seconds * SAMPLE_RATE}],
    }
    store.submit(identifier, json.dumps(doc), 10)
    store.finish(identifier, identifier, metadata, [], verified=bool(mbid))


def test_coverage_identity_and_format(tmp_path: Path) -> None:
    store = Store(tmp_path / "availability.db")
    store.initialize()
    publish(store, "partial", MBID, 1)
    item = {
        "format_id": FORMAT_ID,
        "recording_mbid": MBID,
        "isrc": "USAAA2600001",
        "title": "Synthetic recording",
        "artists": ["Test artist"],
        "duration": 2,
    }
    assert store.availability([item])[0]["status"] == "partial"
    publish(store, "full", MBID, 2)
    assert [
        r["status"]
        for r in store.availability(
            [
                item,
                {**item, "recording_mbid": None},
                {**item, "recording_mbid": OTHER},
                {**item, "isrc": "USAAA2600002"},
                {**item, "recording_mbid": None, "isrc": None},
                {**item, "recording_mbid": None, "title": "Another recording"},
            ]
        )
    ] == ["full", "full", "unknown", "unknown", "unknown", "unknown"]
    with store.connect() as db:
        db.execute("UPDATE reference_tracks SET recording_key=NULL")
    assert store.availability([item])[0]["status"] == "unknown"


async def test_auth_limits_and_capability(tmp_path: Path) -> None:
    app = create_app(
        settings(tmp_path, gateway_enabled=True, application_key=SecretStr(APPLICATION))
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = "/api/v1/dejavu/availability"
            body = {
                "version": 1,
                "items": [{"index": 0, "format_id": FORMAT_ID, "recording_mbid": MBID}],
            }
            headers = {"X-API-Key": UPLOAD, "X-Application-Key": APPLICATION}
            assert (await client.post(path, json=body)).status_code == 401
            assert (
                await client.post(path, json=body, headers={"X-API-Key": UPLOAD})
            ).status_code == 401
            assert (
                await client.post(path, json=body, headers={**headers, "X-API-Key": READ})
            ).status_code == 401
            response = await client.post(path, json=body, headers=headers)
            assert response.status_code == 200
            assert AvailabilityResponse.model_validate(response.json()).items[0].status == "unknown"
            assert (
                await client.post(
                    path, json={**body, "items": body["items"] * 101}, headers=headers
                )
            ).status_code == 422
            assert (
                await client.post(
                    path,
                    content=b" " * 128001,
                    headers={**headers, "Content-Type": "application/json"},
                )
            ).status_code == 413
            assert (await client.get("/api/v1/dejavu/capabilities", headers=headers)).json()[
                "availability_version"
            ] == 1


async def test_preflight_uses_local_published_data_without_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight resolves verified records locally and does not change statistics."""
    app = create_app(settings(tmp_path))
    lookup = AsyncMock(side_effect=AssertionError("unexpected MusicBrainz request"))
    monkeypatch.setattr(MusicBrainzService, "verify", lookup)
    async with app.router.lifespan_context(app):
        store = app.state.store
        publish(store, "known", MBID, 2)
        before = store.statistics()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/dejavu/availability",
                headers={"X-API-Key": UPLOAD},
                json={
                    "version": 1,
                    "items": [
                        {"index": 0, "format_id": FORMAT_ID, "recording_mbid": MBID},
                        {"index": 1, "format_id": FORMAT_ID, "recording_mbid": OTHER},
                    ],
                },
            )
        assert [item["status"] for item in response.json()["items"]] == ["full", "unknown"]
        assert store.statistics() == before
        lookup.assert_not_awaited()
