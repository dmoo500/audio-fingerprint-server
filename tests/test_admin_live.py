"""Verify shared live sampling, revocation and failure visibility without a live database."""

import asyncio
import importlib
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp_client import AsyncClient
from test_admin import ADMIN_KEY, AdminData, configuration, create_admin

from fingerprint_server.admin.live import AdminLive
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

    async def idle(self):
        await asyncio.Future()

    monkeypatch.setattr(AdminLive, "listen", idle)
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


async def test_shared_streams_receive_latest_snapshot_and_end_on_logout(samples, monkeypatch):
    async def idle(self):
        await asyncio.Future()

    monkeypatch.setattr(AdminLive, "run", idle)
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


async def test_sampling_failure_is_visible_and_recovers(samples, caplog, monkeypatch):
    async def idle(self):
        await asyncio.Future()

    monkeypatch.setattr(AdminLive, "run", idle)
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


@pytest.mark.parametrize("authenticated", [True, False])
async def test_disconnected_stream_releases_subscriber(monkeypatch, authenticated):
    module = importlib.import_module("fingerprint_server.admin.app")
    live = AdminLive(AdminData(configuration()))
    auth = Mock(is_authenticated=AsyncMock(return_value=authenticated))
    request = Mock(app={LIVE_KEY: live, AUTH_KEY: auth})
    response = Mock(
        headers={},
        prepare=AsyncMock(),
        write=AsyncMock(side_effect=ConnectionResetError("client disconnected")),
    )
    monkeypatch.setattr(module.web, "StreamResponse", lambda **kwargs: response)
    assert await module.events(request) is response
    assert live.subscribers == 0


async def test_sampler_is_idle_without_browsers_and_stops_cleanly(samples, monkeypatch):
    _, _, read_overview, _ = samples
    from fingerprint_server.admin import live as module

    monkeypatch.setattr(module, "UPDATE_INTERVAL", 0.1)
    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 10)
    live = AdminLive(AdminData(configuration()))
    await live.sample()
    task = asyncio.create_task(live.run())
    try:
        live.invalidate("all")
        await asyncio.sleep(0.02)
        assert read_overview.call_count == 1
        live.subscribers = 2
        live.invalidate("all")
        async with asyncio.timeout(2):
            while live.recognition_revision < 1:
                await asyncio.sleep(0.01)
        assert read_overview.call_count == 2
        live.invalidate("recognition")
        live.invalidate("recognition")
        await asyncio.sleep(0.03)
        assert live.recognition_revision == 1
        async with asyncio.timeout(2):
            while live.recognition_revision < 2:
                await asyncio.sleep(0.01)
        assert read_overview.call_count == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_real_update_interval_coalesces_burst(samples):
    live = AdminLive(AdminData(configuration()))
    await live.sample()
    live.subscribers = 1
    task = asyncio.create_task(live.run())
    try:
        live.invalidate("recognition")
        async with asyncio.timeout(2):
            while live.recognition_revision < 1:
                await asyncio.sleep(0.01)
        first = time.monotonic()
        for _ in range(10):
            live.invalidate("recognition")
        async with asyncio.timeout(7):
            while live.recognition_revision < 2:
                await asyncio.sleep(0.01)
        elapsed = time.monotonic() - first
        assert elapsed >= 4.9
        assert elapsed < 6.5
        assert live.recognition_revision == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_listener_retries_after_clean_disconnect(monkeypatch, caplog):
    from fingerprint_server.admin import live as module

    calls = 0

    async def disconnected(self):
        nonlocal calls
        calls += 1

    async def pause(delay):
        assert delay == 10
        raise asyncio.CancelledError

    monkeypatch.setattr(AdminLive, "_listen_postgres", disconnected)
    monkeypatch.setattr(module.asyncio, "sleep", pause)
    live = AdminLive(AdminData(configuration()))
    with pytest.raises(asyncio.CancelledError):
        await live.listen()
    assert calls == 1
    assert "listener stopped" in caplog.text
