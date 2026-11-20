"""Verify shared live sampling, revocation and failure visibility without a live database."""

import asyncio
import importlib
import json
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest
from aiohttp_client import AsyncClient
from test_admin import ADMIN_KEY, AdminData, configuration, create_admin

from fingerprint_server.admin.state import AUTH_KEY, LIVE_KEY

psycopg = pytest.importorskip("psycopg")
ORIGIN = {"Origin": "http://test"}


@pytest.fixture
def samples(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def connect(self, **kwargs):
        yield None

    monkeypatch.setattr(
        importlib.import_module("fingerprint_server.admin.app"),
        "validate_application_keys",
        lambda db: None,
    )
    monkeypatch.setattr("fingerprint_server.database.postgres.PostgresStore.connect", connect)
    overview = {
        "submissions": {"processed": 1, "pending_metadata": 2},
        "references": {"published": 1, "stored": 2},
        "database_bytes": 2048,
        "format_version": 1,
        "worker_available": True,
    }
    counters = {"submissions": [1, 0, 0, 0], "references": [2, 0, 0, 0]}
    read_overview = Mock(side_effect=lambda: overview)
    read_revisions = Mock(side_effect=lambda: counters)
    monkeypatch.setattr(AdminData, "overview", lambda self: read_overview())
    monkeypatch.setattr(AdminData, "revisions", lambda self: read_revisions())
    return overview, counters, read_overview, read_revisions


async def _read_event(lines):
    event = None
    payload = None
    async for line in lines:
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            payload = line[6:]
        elif not line and event is not None:
            return event, payload
    raise AssertionError("The event stream ended before an event was received")


async def _login(client: AsyncClient) -> None:
    response = await client.post(
        "/admin/api/login",
        headers=ORIGIN,
        json={"username": "admin", "password": ADMIN_KEY},
    )
    assert response.status_code == 200


async def test_shared_streams_receive_latest_snapshot_and_end_on_logout(samples):
    overview, counters, read_overview, read_revisions = samples
    async with AsyncClient(create_admin(configuration())) as client:
        await _login(client)
        app = client.server.app
        live = app[LIVE_KEY]
        auth = app[AUTH_KEY]
        async with client.stream("GET", "/admin/events") as first_response:
            assert first_response.status_code == 200
            assert first_response.headers["cache-control"] == "no-store"
            content_security_policy = first_response.headers["content-security-policy"]
            assert content_security_policy.startswith("default-src 'none'")
            first_lines = first_response.aiter_lines()
            first_initial = await _read_event(first_lines)
            assert first_initial[0] == "snapshot"
            async with client.stream("GET", "/admin/events") as second_response:
                assert second_response.status_code == 200
                second_lines = second_response.aiter_lines()
                assert await _read_event(second_lines) == first_initial
                assert live.subscribers == 2
                assert read_overview.call_count == read_revisions.call_count == 1
                before = live.snapshot["revisions"].copy()
                counters["submissions"][2] += 1
                await live.sample()
                event, payload = await _read_event(first_lines)
                assert event == "snapshot"
                assert (event, payload) == await _read_event(second_lines)
                assert read_overview.call_count == read_revisions.call_count == 2
                assert live.snapshot["revisions"]["submissions"] != before["submissions"]
                assert live.snapshot["revisions"]["references"] == before["references"]
                assert (
                    live.snapshot["revisions"]["unresolved"]
                    == live.snapshot["revisions"]["submissions"]
                )
                assert json.loads(payload)["overview"] == overview
                auth.sessions.clear()
                live.changed.set()
                assert await _read_event(first_lines) == ("session-expired", "{}")
                with pytest.raises(StopAsyncIteration):
                    await anext(first_lines)
        assert live.subscribers == 0


async def test_sampling_failure_is_visible_and_recovers(samples, caplog):
    _, _, read_overview, _ = samples
    async with AsyncClient(create_admin(configuration())) as client:
        await _login(client)
        app = client.server.app
        live = app[LIVE_KEY]
        snapshot = live.snapshot
        read_overview.side_effect = psycopg.OperationalError("database temporarily unavailable")
        await live.sample()
        assert live.failed
        assert live.snapshot is snapshot
        assert "Admin live data sampling failed" in caplog.text
        async with client.stream("GET", "/admin/events") as response:
            lines = response.aiter_lines()
            assert await _read_event(lines) == ("unavailable", "{}")
            read_overview.side_effect = lambda: snapshot["overview"]
            await live.sample()
            event, _ = await _read_event(lines)
            assert event == "snapshot"
            app[AUTH_KEY].sessions.clear()
            live.changed.set()
            assert await _read_event(lines) == ("session-expired", "{}")
        assert live.subscribers == 0


async def test_sampler_is_idle_without_browsers_and_stops_cleanly(samples, monkeypatch):
    _, _, read_overview, _ = samples
    from fingerprint_server.admin import live as module

    original_sleep = asyncio.sleep
    ticks = asyncio.Queue()

    async def tick(_delay):
        await ticks.get()

    monkeypatch.setattr(module.asyncio, "sleep", tick)
    app = create_admin(configuration())
    async with asynccontextmanager(app.cleanup_ctx[0])(app):
        live = app[LIVE_KEY]
        await ticks.put(None)
        await original_sleep(0)
        assert read_overview.call_count == 1
        live.subscribers = 2
        await ticks.put(None)
        async with asyncio.timeout(2):
            while read_overview.call_count < 2:
                await original_sleep(0.01)
        assert read_overview.call_count == 2
