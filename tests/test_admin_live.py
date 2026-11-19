"""Verify shared live sampling, revocation and failure visibility without a live database."""

import asyncio
import importlib
import json
import time
from unittest.mock import Mock

import pytest
from starlette.requests import Request
from test_admin import AdminData, configuration, create_admin

psycopg = pytest.importorskip("psycopg")


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


async def test_shared_streams_receive_latest_snapshot_and_end_on_logout(samples):
    overview, counters, read_overview, read_revisions = samples
    app = create_admin(configuration())
    async with app.router.lifespan_context(app):
        live = app.state.live
        auth = app.state.auth
        auth.sessions["session"] = time.monotonic() + 60
        request = Request({"type": "http", "session": {"token": "session"}, "app": app})
        endpoint = next(
            route.endpoint for route in app.routes if getattr(route, "name", None) == "events"
        )
        first = (await endpoint(request)).body_iterator
        second = (await endpoint(request)).body_iterator
        initial = await anext(first)
        assert initial == await anext(second)
        assert live.subscribers == 2
        assert read_overview.call_count == read_revisions.call_count == 1
        before = live.snapshot["revisions"].copy()
        # A metadata-only UPDATE changes the hint despite identical row/lifecycle counts.
        counters["submissions"][2] += 1
        await live.sample()
        update = await anext(first)
        assert update == await anext(second)
        assert read_overview.call_count == read_revisions.call_count == 2
        assert live.snapshot["revisions"]["submissions"] != before["submissions"]
        assert live.snapshot["revisions"]["references"] == before["references"]
        assert live.snapshot["revisions"]["unresolved"] == live.snapshot["revisions"]["submissions"]
        assert json.loads(update.split("data: ")[1])["overview"] == overview
        auth.sessions.clear()
        live.changed.set()
        assert "event: session-expired" in await anext(first)
        with pytest.raises(StopAsyncIteration):
            await anext(first)
        await second.aclose()
        assert live.subscribers == 0


async def test_sampling_failure_is_visible_and_recovers(samples, caplog):
    _, _, read_overview, _ = samples
    app = create_admin(configuration())
    async with app.router.lifespan_context(app):
        live = app.state.live
        snapshot = live.snapshot
        read_overview.side_effect = psycopg.OperationalError("database temporarily unavailable")
        await live.sample()
        assert live.failed
        assert live.snapshot is snapshot
        assert "Admin live data sampling failed" in caplog.text
        auth = app.state.auth
        auth.sessions["session"] = time.monotonic() + 60
        request = Request({"type": "http", "session": {"token": "session"}, "app": app})
        endpoint = next(
            route.endpoint for route in app.routes if getattr(route, "name", None) == "events"
        )
        stream = (await endpoint(request)).body_iterator
        assert await anext(stream) == "event: unavailable\ndata: {}\n\n"
        read_overview.side_effect = lambda: snapshot["overview"]
        await live.sample()
        assert not live.failed
        assert "event: snapshot" in await anext(stream)
        await stream.aclose()
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
    async with app.router.lifespan_context(app):
        await ticks.put(None)
        await original_sleep(0)
        assert read_overview.call_count == 1
        app.state.live.subscribers = 2
        await ticks.put(None)
        async with asyncio.timeout(2):
            while read_overview.call_count < 2:
                await original_sleep(0.01)
        assert read_overview.call_count == 2
