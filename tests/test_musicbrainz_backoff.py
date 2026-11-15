"""Service-wide MusicBrainz cooldown and durable retry behavior."""

import asyncio
import json
import time
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_metadata_gate import document
from test_musicbrainz_validation import MBID, api, online_settings, online_store

import fingerprint_server.musicbrainz as worker_module
import fingerprint_server.store as store_module
from fingerprint_server.musicbrainz import MusicBrainzUnavailable
from fingerprint_server.worker import Worker


class Clock:
    def __init__(self) -> None:
        self.monotonic = 100.0
        self.wall = 1_700_000_000.0

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += seconds


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120),
        ("0", 30),
        ("invalid", 30),
        ("-1", 30),
        ("3600", 3600),
        (format_datetime(datetime.fromtimestamp(1_700_000_120, UTC), usegmt=True), 120),
        (format_datetime(datetime.fromtimestamp(1_699_999_900, UTC), usegmt=True), 30),
    ],
)
async def test_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: str, expected: int
) -> None:
    clock = Clock()
    monkeypatch.setattr(
        worker_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock.monotonic, time=lambda: clock.wall),
    )
    store = online_store(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": header})
        )
    ) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        with pytest.raises(MusicBrainzUnavailable) as caught:
            await worker.musicbrainz.lookup("recording", MBID)
        assert caught.value.status == 429
        assert caught.value.delay == expected
        assert worker.musicbrainz.cooldown_until == clock.monotonic + expected
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM enrichment_cache").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("statuses", "delays"),
    [
        ([429, 502, 429, 502, 429, 502, 429], [30, 60, 120, 240, 480, 900, 900]),
        ([503] * 7, [60, 70, 80, 90, 100, 110, 120]),
        ([503] * 86, [*range(60, 901, 10), 900]),
        ([429, 502, 503, 503, 502, 503, 503], [30, 60, 60, 70, 140, 60, 70]),
    ],
)
async def test_backoff_growth_cache_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statuses: list[int], delays: list[int]
) -> None:
    clock = Clock()
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance(delay)

    monkeypatch.setattr(
        worker_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock.monotonic, time=lambda: clock.wall),
    )
    monkeypatch.setattr(
        worker_module,
        "asyncio",
        SimpleNamespace(sleep=sleep, Event=asyncio.Event, Lock=asyncio.Lock),
    )
    store = online_store(tmp_path)
    responses = iter([*statuses, 200, 503])
    calls: list[float] = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(clock.monotonic)
        status = next(responses)
        return api(request) if status == 200 else httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        for expected in delays:
            with pytest.raises(MusicBrainzUnavailable) as caught:
                await worker.musicbrainz.lookup("recording", MBID)
            assert caught.value.delay == expected
        assert sleeps == delays[:-1]
        assert all(b - a >= 1.1 for a, b in zip(calls, calls[1:], strict=False))
        assert (await worker.musicbrainz.lookup("recording", MBID))["status"] == "resolved"
        assert worker.musicbrainz.backoff == 0
        with pytest.raises(MusicBrainzUnavailable) as caught:
            await worker.musicbrainz.lookup("recording", "another")
        assert caught.value.delay == 60
        assert (await worker.musicbrainz.lookup("recording", MBID))["status"] == "resolved"
        assert worker.musicbrainz.backoff == 60
        assert len(calls) == len(statuses) + 2


async def test_deferral_preserves_attempts_and_blocks_next_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    waiting = asyncio.Event()
    resume = asyncio.Event()

    async def sleep(delay: float) -> None:
        waiting.set()
        await resume.wait()
        clock.advance(delay)

    monkeypatch.setattr(
        worker_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock.monotonic, time=lambda: clock.wall),
    )
    monkeypatch.setattr(
        worker_module,
        "asyncio",
        SimpleNamespace(
            sleep=sleep, Event=asyncio.Event, Lock=asyncio.Lock, wait_for=asyncio.wait_for
        ),
    )
    store = online_store(tmp_path)
    payload = json.dumps(document())
    store.submit("first", payload, 10)
    store.submit("second", payload, 10)
    with store.connect() as db:
        db.execute("UPDATE submissions SET attempts=2 WHERE id='first'")
    calls: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(503, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            first = store.get_submission("first")
            second = store.get_submission("second")
            assert first["state"] == "queued"
            assert first["attempts"] == 2
            assert first["error"] == "MusicBrainz service unavailable"
            assert second["state"] == "queued" and second["attempts"] == 0
            with store.connect() as db:
                retry_at = db.execute(
                    "SELECT retry_at FROM submissions WHERE id='first'"
                ).fetchone()[0]
            assert retry_at >= time.time() + 115
            assert "HTTP status=503" in caplog.text
            assert "retrying in 120 seconds" in caplog.text
            assert "attempt preserved" in caplog.text
            assert "api_call=GET /isrc/USAAA2600001" in caplog.text
            assert len(calls) == 1
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_lookup_wait_is_shared_and_cancellable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def sleep(delay: float) -> None:
        waiting.set()
        await release.wait()
        clock.advance(delay)

    monkeypatch.setattr(
        worker_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock.monotonic, time=lambda: clock.wall),
    )
    monkeypatch.setattr(
        worker_module,
        "asyncio",
        SimpleNamespace(sleep=sleep, Event=asyncio.Event, Lock=asyncio.Lock),
    )
    calls: list[float] = []

    def reply(_: httpx.Request) -> httpx.Response:
        calls.append(clock.monotonic)
        return httpx.Response(503)

    store = online_store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        with pytest.raises(MusicBrainzUnavailable):
            await worker.musicbrainz.lookup("recording", MBID)
        task = asyncio.create_task(worker.musicbrainz.lookup("recording", "other"))
        await asyncio.wait_for(waiting.wait(), 5)
        assert len(calls) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not worker.musicbrainz.lookup_lock.locked()
        release.set()
        with pytest.raises(MusicBrainzUnavailable):
            await worker.musicbrainz.lookup("recording", "other")
        assert calls == [100, 160]


async def test_extended_cooldown_cannot_be_shortened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    sleeps: list[float] = []
    worker: Worker

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 1:
            worker.musicbrainz.cooldown_until += 60
        clock.advance(delay)

    monkeypatch.setattr(
        worker_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock.monotonic, time=lambda: clock.wall),
    )
    monkeypatch.setattr(
        worker_module,
        "asyncio",
        SimpleNamespace(sleep=sleep, Event=asyncio.Event, Lock=asyncio.Lock),
    )
    calls: list[float] = []

    def reply(_: httpx.Request) -> httpx.Response:
        calls.append(clock.monotonic)
        return httpx.Response(404)

    store = online_store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        worker = Worker(store, online_settings(tmp_path), client)
        worker.musicbrainz.cooldown_until = 130
        worker.musicbrainz.backoff = 60
        assert (await worker.musicbrainz.lookup("recording", MBID))["status"] == "not found"
        assert sleeps == [30, 60]
        assert calls == [190]
        assert worker.musicbrainz.backoff == 0


async def test_repeated_outages_retry_exhausted_submission_until_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    waiting: asyncio.Queue[float] = asyncio.Queue()
    resume: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(delay: float) -> None:
        waiting.put_nowait(delay)
        await resume.get()
        clock.advance(delay)

    fake_time = SimpleNamespace(monotonic=lambda: clock.monotonic, time=lambda: clock.wall)
    monkeypatch.setattr(worker_module, "time", fake_time)
    monkeypatch.setattr(store_module, "time", fake_time)
    monkeypatch.setattr(
        worker_module,
        "asyncio",
        SimpleNamespace(
            sleep=sleep, Event=asyncio.Event, Lock=asyncio.Lock, wait_for=asyncio.wait_for
        ),
    )
    store = online_store(tmp_path)
    store.submit("first", json.dumps(document()), 10)
    with store.connect() as db:
        db.execute("UPDATE submissions SET attempts=2 WHERE id='first'")
    calls: list[float] = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(clock.monotonic)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "120"})
        return httpx.Response(502) if len(calls) == 2 else api(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        task = asyncio.create_task(Worker(store, online_settings(tmp_path), client).run())
        try:
            assert await asyncio.wait_for(waiting.get(), 5) == 120
            assert store.get_submission("first")["attempts"] == 2
            resume.put_nowait(None)
            assert await asyncio.wait_for(waiting.get(), 5) == 120
            assert store.get_submission("first")["attempts"] == 2
            resume.put_nowait(None)
            async with asyncio.timeout(5):
                while store.get_submission("first")["state"] != "processed":
                    await asyncio.sleep(0.01)
            assert store.get_submission("first")["attempts"] == 3
            assert calls == [100, 220, 340]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
