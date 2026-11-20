"""Verify optional account sessions and isolated recognition history."""

import json
from pathlib import Path

import httpx
import pytest
from aiohttp_client import AsyncClient
from test_recognition import HEADERS, ORIGIN, PCM, matched, settings

from fingerprint_server.recognition.app import create_app
from fingerprint_server.recognition.history import APPLICATION_ID, History

PASSWORD = "a temporary test passphrase"  # pragma: allowlist secret
ACCOUNT = "/api/account"


async def test_history_is_private_and_anonymous_recognition_is_not_saved(tmp_path: Path) -> None:
    path = tmp_path / "recognition.sqlite"
    history = History(path)
    history.initialize()
    invite = history.invite()
    other_invite = history.invite()
    config = settings(history_database=path, requests_per_minute=30)
    reference = "a" * 64
    cover = {"status": "ready", "url": "https://coverartarchive.org/cover"}

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/dejavu/artwork/{reference}":
            return httpx.Response(200, json=cover)
        result = matched()
        assert isinstance(result["match"], dict)
        result["match"]["reference_id"] = reference
        return httpx.Response(200, json=result)

    transport = httpx.MockTransport(upstream)
    headers = {"Origin": ORIGIN}
    async with AsyncClient(create_app(config, transport=transport), base_url=ORIGIN) as client:
        assert (await client.get(f"{ACCOUNT}/session")).json() == {
            "enabled": True,
            "username": None,
        }
        assert (await client.get(f"{ACCOUNT}/history")).status_code == 401

        async def recognize() -> httpx.Response:
            return await client.post(
                "/api/recognize?sample_rate=8000", headers=HEADERS, content=PCM
            )

        assert (await recognize()).json()["status"] == "matched"
        registration = {"username": "Listener", "password": PASSWORD, "invite": invite}
        assert (await client.post(f"{ACCOUNT}/register", json=registration)).status_code == 403
        assert (
            await client.post(f"{ACCOUNT}/register", headers=headers, json=registration)
        ).status_code == 201
        assert (
            await client.post(
                f"{ACCOUNT}/register", headers=headers, json={**registration, "username": "Other"}
            )
        ).status_code == 400
        credentials = {"username": "listener", "password": PASSWORD}
        login = await client.post(f"{ACCOUNT}/login", headers=headers, json=credentials)
        assert login.json() == {"username": "listener"}
        cookie = login.headers["set-cookie"].lower()
        assert all(flag in cookie for flag in ("httponly", "samesite=strict", "secure"))
        assert (await client.get(f"{ACCOUNT}/history")).json() == {"items": []}
        result = await recognize()
        assert result.json()["status"] == "matched"
        response = await client.get(f"{ACCOUNT}/history")
        (hit,) = response.json()["items"]
        assert hit["title"] == result.json()["match"]["title"]
        assert hit["artists"] == result.json()["match"]["artists"]
        assert set(hit) == {"id", "title", "artists", "recognized_at", "has_artwork"}
        assert hit["has_artwork"] is True
        artwork_path = f"{ACCOUNT}/history/{hit['id']}/artwork"
        assert (await client.get(artwork_path)).json() == cover
        assert isinstance(hit["recognized_at"], int)
        assert response.headers["cache-control"] == "no-store"
        session = client.cookies.get("fp_recognition")
        assert (await client.post(f"{ACCOUNT}/logout", headers=headers)).status_code == 200
        assert (await client.get(f"{ACCOUNT}/history")).status_code == 401
        assert (await client.get(artwork_path)).status_code == 404
        assert history.username(session or "") is None

        other = {"username": "other", "password": PASSWORD}
        assert (
            await client.post(
                f"{ACCOUNT}/register", headers=headers, json={**other, "invite": other_invite}
            )
        ).status_code == 201
        assert (
            await client.post(f"{ACCOUNT}/login", headers=headers, json=other)
        ).status_code == 200
        assert (await client.get(f"{ACCOUNT}/history")).json() == {"items": []}
        assert (await client.get(artwork_path)).status_code == 404
        assert (await client.post(f"{ACCOUNT}/history/clear")).status_code == 403
        assert (await client.post(f"{ACCOUNT}/history/clear", headers=headers)).status_code == 200

    async with AsyncClient(create_app(config, transport=transport), base_url=ORIGIN) as restarted:
        assert (
            await restarted.post(f"{ACCOUNT}/login", headers=headers, json=credentials)
        ).status_code == 200
        assert (await restarted.get(f"{ACCOUNT}/history")).json() == {"items": [hit]}
        assert (await restarted.get(artwork_path)).json() == cover
        assert (
            await restarted.post(f"{ACCOUNT}/history/clear", headers=headers)
        ).status_code == 200
        assert (await restarted.get(f"{ACCOUNT}/history")).json() == {"items": []}
        assert (await restarted.get(artwork_path)).status_code == 404


def test_history_retains_exactly_the_latest_500_matches(tmp_path: Path) -> None:
    history = History(tmp_path / "history.sqlite")
    history.initialize()
    assert history.register("listener", PASSWORD, history.invite())
    token = history.login("listener", PASSWORD, "")
    assert token
    for index in range(502):
        history.save(token, {"title": str(index), "artists": ["Artist"], "recognized_at": index})
    assert [hit["title"] for hit in history.recent(token)] == [
        str(index) for index in reversed(range(2, 502))
    ]


def test_history_refuses_unrelated_databases(tmp_path: Path) -> None:
    history = History(tmp_path / "unrelated.sqlite")
    with history.connection(write=True) as db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(RuntimeError, match="Not a supported recognition history database"):
        history.initialize()


def test_history_upgrades_existing_matches_without_artwork(tmp_path: Path) -> None:
    history = History(tmp_path / "history.sqlite")
    history.initialize()
    assert history.register("listener", PASSWORD, history.invite())
    token = history.login("listener", PASSWORD, "")
    assert token
    with history.connection(write=True) as db:
        db.execute("ALTER TABLE hits DROP COLUMN artwork_reference")
        db.execute("PRAGMA user_version = 1")
        db.execute(
            "INSERT INTO hits VALUES (1, (SELECT id FROM accounts), 42, ?, ?)",
            ("Old match", json.dumps(["Artist"])),
        )
    history.initialize()
    history.initialize()
    assert history.recent(token) == [
        {
            "id": 1,
            "title": "Old match",
            "artists": ["Artist"],
            "recognized_at": 42,
            "has_artwork": False,
        }
    ]
    assert history.artwork_reference(token, 1) is None
    with history.connection() as db:
        assert db.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
