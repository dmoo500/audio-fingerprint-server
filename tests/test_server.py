"""Real reference ingestion, transient PCM recognition and durable queue regression tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import soxr
from aiohttp_client import AsyncClient
from audio_fingerprint import FORMAT_ID, HOP_SIZE, SAMPLE_RATE, fingerprint, plan_windows
from pydantic import SecretStr, ValidationError

from fingerprint_server.api_state import STORE, WORKER
from fingerprint_server.app import MAX_DOCUMENT_BYTES, create_app
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database.sqlite import QueueFullError, Store
from fingerprint_server.settings import Settings
from fingerprint_server.worker import Worker

UPLOAD = "test-upload-key-with-at-least-24-characters"
READ = "test-recognition-key-with-at-least-24-characters"


def settings(path: Path, **kwargs: Any) -> Settings:
    """Use a temporary database and explicit test credentials, never developer settings."""
    return Settings(
        database=path / "test.db", upload_key=SecretStr(UPLOAD), read_key=SecretStr(READ), **kwargs
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app(settings(tmp_path))
    async with AsyncClient(application) as connection:
        yield connection


def audio(seconds: int = 12) -> np.ndarray[Any, Any]:
    """A deterministic acoustic fixture without copyrighted reference audio."""
    return np.random.default_rng(42).normal(0, 0.1, SAMPLE_RATE * seconds).astype("<f4")


def document(samples: np.ndarray[Any, Any] | None = None) -> dict[str, Any]:
    """Build the exact MA producer document shape from real landmark extraction."""
    samples = audio() if samples is None else samples
    return {
        "format_id": FORMAT_ID,
        "sample_rate": SAMPLE_RATE,
        "offset_unit": "fft_hops",
        "windows": [{"start_sample": 0, "sample_count": len(samples)}],
        "fingerprints": [list(pair) for pair in fingerprint(samples)],
        "metadata": {
            "duration": len(samples) / SAMPLE_RATE,
            "title": "Synthetic reference",
            "artists": ["Test signal"],
            "album": "Synthetic album",
            "isrc": "USAAA2600001",
            "recording_mbid": None,
        },
    }


def canonical(payload: dict[str, Any]) -> tuple[bytes, str]:
    """Serialize identically to the MA provider."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return body, hashlib.sha256(body).hexdigest()


async def submit(client: httpx.AsyncClient, payload: dict[str, Any]) -> str:
    """Submit and wait for durable worker publication with a finite test deadline."""
    body, identifier = canonical(payload)
    response = await client.post(
        "/api/v1/dejavu/submissions",
        content=body,
        headers={
            "X-API-Key": UPLOAD,
            "Idempotency-Key": identifier,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code in (200, 202), response.text
    assert response.json()["submission_id"] == identifier
    async with asyncio.timeout(5):
        while True:
            state = await client.get(
                f"/api/v1/dejavu/submissions/{identifier}", headers={"X-API-Key": UPLOAD}
            )
            assert state.status_code == 200, state.text
            if state.json()["state"] == "processed":
                return identifier
            assert state.json()["state"] != "failed", state.text
            await asyncio.sleep(0.1)


async def recognize(
    client: httpx.AsyncClient, samples: np.ndarray[Any, Any], rate: int = SAMPLE_RATE
) -> httpx.Response:
    """Exercise the actual float32 wire contract."""
    return await client.post(
        f"/api/v1/dejavu/recognize?sample_rate={rate}",
        content=samples.tobytes(),
        headers={"X-API-Key": READ, "Content-Type": "application/octet-stream"},
    )


async def test_end_to_end_noisy_excerpt(client: httpx.AsyncClient) -> None:
    """A noisy excerpt resolves to its correct absolute reference offset."""
    samples = audio()
    identifier = await submit(client, document(samples))
    excerpt = samples[50 * HOP_SIZE : 50 * HOP_SIZE + 5 * SAMPLE_RATE].copy()
    excerpt += np.random.default_rng(7).normal(0, 0.002, len(excerpt)).astype("<f4")
    response = await recognize(client, excerpt)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "matched", result
    assert result["match"]["offset_seconds"] == pytest.approx(50 * HOP_SIZE / SAMPLE_RATE)
    assert result["match"]["metadata"]["title"] == "Synthetic reference"
    assert result["match"]["matched_landmarks"] >= 30
    duplicate_body, _ = canonical(document(samples))
    duplicate = await client.post(
        "/api/v1/dejavu/submissions",
        content=duplicate_body,
        headers={
            "X-API-Key": UPLOAD,
            "Idempotency-Key": identifier,
            "Content-Type": "application/json",
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "processed"


async def test_48khz_microphone_query(client: httpx.AsyncClient) -> None:
    """The server's real resampling path can recognize a 48 kHz capture."""
    samples = audio()
    await submit(client, document(samples))
    excerpt = samples[50 * HOP_SIZE : 50 * HOP_SIZE + 5 * SAMPLE_RATE]
    capture = soxr.resample(excerpt, SAMPLE_RATE, 48000).astype("<f4")
    response = await recognize(client, capture, 48000)
    assert response.json()["status"] == "matched", response.text


async def test_capture_not_aligned_to_reference_fft_hops(client: httpx.AsyncClient) -> None:
    """Real microphone capture need not start at a reference FFT-hop boundary."""
    samples = audio()
    await submit(client, document(samples))
    excerpt = samples[12345 : 12345 + 5 * SAMPLE_RATE]
    response = await recognize(client, excerpt)
    assert response.json()["status"] == "matched", response.text


@pytest.mark.parametrize("fails", [False, True])
async def test_cancellation_keeps_sync_work_bounded(fails: bool) -> None:
    """Cancellation waits for the CPU worker before releasing concurrency capacity."""
    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        release.wait(timeout=2)
        if fails:
            raise ValueError("Worker failed")

    capacity = asyncio.Semaphore(1)

    async def bounded() -> None:
        async with capacity:
            await run_sync(block)

    task = asyncio.create_task(bounded())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
            assert capacity.locked()
    finally:
        release.set()
        with pytest.raises(ValueError if fails else asyncio.CancelledError):
            await task
    assert not capacity.locked()


async def test_unindexed_and_silent_capture(client: httpx.AsyncClient) -> None:
    """Unknown audio and silence are honest no-match responses."""
    response = await recognize(client, audio(5))
    assert response.json() == {"status": "no_match", "match": None}
    response = await recognize(client, np.zeros(SAMPLE_RATE * 5, dtype="<f4"))
    assert response.json()["status"] == "no_match"


async def test_ambiguous_reference_is_not_claimed_as_match(client: httpx.AsyncClient) -> None:
    """Two similar acoustic references cannot produce a confident single match."""
    samples = audio()
    await submit(client, document(samples))
    second = document(samples)
    second["fingerprints"] = second["fingerprints"][:-1]
    second["metadata"]["title"] = "Another interpretation"
    await submit(client, second)
    response = await recognize(client, samples[50 * HOP_SIZE : 50 * HOP_SIZE + 5 * SAMPLE_RATE])
    assert response.json() == {"status": "ambiguous", "match": None}


@pytest.mark.parametrize("mutation", ["url", "format", "offset", "duplicate", "window", "nan"])
async def test_invalid_submissions_never_enter_queue(
    client: httpx.AsyncClient, mutation: str
) -> None:
    """Reject malformed references, unknown fields and invalid sampled coverage."""
    payload = document()
    if mutation == "url":
        payload["metadata"]["path"] = "https://private.example/audio"
    elif mutation == "format":
        payload["format_id"] = "another-format"
    elif mutation == "offset":
        payload["fingerprints"][0][1] = 50000
    elif mutation == "duplicate":
        payload["fingerprints"].append(payload["fingerprints"][0])
    elif mutation == "window":
        payload["windows"][0]["start_sample"] = 1
    else:
        payload["metadata"]["duration"] = float("nan")
    body = json.dumps(payload).encode()
    response = await client.post(
        "/api/v1/dejavu/submissions",
        content=body,
        headers={
            "X-API-Key": UPLOAD,
            "Idempotency-Key": hashlib.sha256(body).hexdigest(),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422


async def test_credentials_are_separate(client: httpx.AsyncClient) -> None:
    """A recognition credential cannot contribute or inspect pending submissions."""
    response = await client.post(
        "/api/v1/dejavu/submissions", content=b"{}", headers={"X-API-Key": READ}
    )
    assert response.status_code == 401
    response = await client.post(
        "/api/v1/dejavu/recognize?sample_rate=44100", content=b"", headers={"X-API-Key": UPLOAD}
    )
    assert response.status_code == 401


async def test_idempotency_key_must_match_document(client: httpx.AsyncClient) -> None:
    """A caller cannot reuse a document identity for other reference content."""
    body, _ = canonical(document())
    response = await client.post(
        "/api/v1/dejavu/submissions",
        content=body,
        headers={
            "X-API-Key": UPLOAD,
            "Idempotency-Key": "0" * 64,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422


async def test_streamed_body_limit_without_content_length(client: httpx.AsyncClient) -> None:
    """Chunked clients cannot bypass the actual body byte limit."""

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"x" * (MAX_DOCUMENT_BYTES // 2)

    response = await client.post(
        "/api/v1/dejavu/submissions",
        content=chunks(),
        headers={"X-API-Key": UPLOAD, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "raw",
    [
        b"x",
        np.zeros(2, dtype="<f4").tobytes(),
        np.full(SAMPLE_RATE * 3, np.nan, dtype="<f4").tobytes(),
    ],
)
async def test_invalid_microphone_pcm(client: httpx.AsyncClient, raw: bytes) -> None:
    """Partial frames, short captures and nonfinite audio fail explicitly."""
    response = await client.post(
        "/api/v1/dejavu/recognize?sample_rate=44100",
        content=raw,
        headers={"X-API-Key": READ, "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/static/capture.js",
        "/static/recording.js",
        "/static/microphone.js",
        "/static/style.css",
    ],
)
async def test_legacy_microphone_page_is_retired(client: httpx.AsyncClient, path: str) -> None:
    """Keep the browser UI and its assets off the API service."""
    assert (await client.get(path)).status_code == 404


def test_durable_queue_restart_and_atomic_visibility(tmp_path: Path) -> None:
    """A restart recovers claimed work; only published landmarks are recognizable."""
    store = Store(tmp_path / "test.db")
    store.initialize()
    payload = document()
    body, identifier = canonical(payload)
    store.submit(identifier, body.decode(), 10)
    assert store.claim()["id"] == identifier
    assert store.match([tuple(pair) for pair in payload["fingerprints"]]) == []
    restarted = Store(store.path)
    restarted.initialize()
    assert restarted.claim()["id"] == identifier
    restarted.finish(
        identifier,
        "reference",
        payload["metadata"],
        [tuple(pair) for pair in payload["fingerprints"]],
    )
    assert restarted.get_submission(identifier)["state"] == "processed"
    assert (
        restarted.match([tuple(pair) for pair in payload["fingerprints"]])[0]["reference_id"]
        == "reference"
    )


def test_queue_capacity_and_deduplication(tmp_path: Path) -> None:
    """Queue capacity rejects new work but still acknowledges already committed duplicates."""
    store = Store(tmp_path / "test.db")
    store.initialize()
    store.submit("first", "{}", 1)
    assert store.submit("first", "{}", 1)["state"] == "queued"
    with pytest.raises(QueueFullError):
        store.submit("second", "{}", 1)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://musicbrainz.org/ws/2",
        "https://metadata.example.invalid/musicbrainz/ws/2/",
        "http://localhost:5000/ws/2",
    ],
)
async def test_musicbrainz_enrichment_is_cached_without_guessing(
    tmp_path: Path, base_url: str
) -> None:
    """A unique ISRC lookup is cached while ambiguous recordings remain unresolved."""
    calls = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "isrc": "USAAA2600001",
                "recording-count": 1,
                "recordings": [{"id": "00000000-0000-4000-8000-000000000001"}],
            },
        )

    store = Store(tmp_path / "test.db")
    store.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as connection:
        worker = Worker(store, settings(tmp_path, musicbrainz_base_url=base_url), connection)
        result = await worker.musicbrainz.lookup("isrc", "USAAA2600001")
        assert result["status"] == "resolved"
        assert await worker.musicbrainz.lookup("isrc", "USAAA2600001") == result
        assert len(calls) == 1
        assert calls[0].url.path == f"{httpx.URL(base_url).path.rstrip('/')}/isrc/USAAA2600001"
        assert calls[0].url.params["inc"] == "artists+releases"


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://metadata.example.invalid/ws/2",
        "https://user:secret@metadata.example.invalid/ws/2",
        "https://metadata.example.invalid/ws/2?token=secret",
        "https://metadata.example.invalid/ws/2#fragment",
    ],
)
def test_musicbrainz_base_url_rejects_invalid_configuration(tmp_path: Path, base_url: str) -> None:
    """Invalid API bases fail startup instead of sending requests to unintended paths."""
    with pytest.raises(ValidationError):
        settings(tmp_path, musicbrainz_base_url=base_url)


@pytest.mark.parametrize("status", [404, 200])
async def test_musicbrainz_missing_or_multiple_results_never_fabricate_recording_id(
    tmp_path: Path, status: int
) -> None:
    """Missing or multiple ISRC candidates cannot fabricate a recording assignment."""
    store = Store(tmp_path / "test.db")
    store.initialize()
    response = {
        "isrc": "USAAA2600001",
        "recording-count": 2,
        "recordings": [
            {"id": "00000000-0000-4000-8000-000000000001"},
            {"id": "00000000-0000-4000-8000-000000000002"},
        ],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=response))
    ) as connection:
        worker = Worker(store, settings(tmp_path), connection)
        from fingerprint_server.musicbrainz import EnrichmentError

        if status == 404:
            with pytest.raises(EnrichmentError, match="not found"):
                await worker.musicbrainz.verify({"isrc": "USAAA2600001"})
        else:
            assert await worker.musicbrainz.verify({"isrc": "USAAA2600001"}) == {
                "isrc": "USAAA2600001"
            }


async def test_terminal_processing_failure_is_visible(tmp_path: Path) -> None:
    """Worker validation failures stay durable and never become recognition references."""
    application = create_app(settings(tmp_path))
    async with AsyncClient(application):
        store = application[STORE]
        worker = application[WORKER]
        assert worker is not None
        await run_sync(store.submit, "bad-reference", "{}", 10)
        worker.wakeup.set()
        async with asyncio.timeout(5):
            while True:
                row = await run_sync(store.get_submission, "bad-reference")
                if row["state"] == "failed":
                    break
                await asyncio.sleep(0.01)
        assert row["error"] == "Invalid reference or metadata response"
        assert await run_sync(store.match, [("0" * 20, 0)]) == []


async def test_online_lookup_failure_persists_retry(tmp_path: Path) -> None:
    """A transient enrichment failure returns durable work to the delayed queue."""
    configuration = settings(
        tmp_path, musicbrainz_enabled=True, musicbrainz_contact="local-test@example.invalid"
    )
    payload = document()
    payload["metadata"]["isrc"] = "USAAA2600001"
    body, identifier = canonical(payload)
    store = Store(configuration.database)
    store.initialize()
    store.submit(identifier, body.decode(), 10)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    ) as connection:
        worker = Worker(store, configuration, connection)
        task = asyncio.create_task(worker.run())
        try:
            async with asyncio.timeout(5):
                while True:
                    row = await run_sync(store.get_submission, identifier)
                    if row["state"] == "queued" and row["attempts"] == 1 and row["error"]:
                        break
                    await asyncio.sleep(0.01)
            assert store.claim() is None
            assert store.match([tuple(pair) for pair in payload["fingerprints"]]) == []
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_identical_acoustic_reference_keeps_original_metadata(
    client: httpx.AsyncClient,
) -> None:
    """Metadata variants do not duplicate identical acoustic references."""
    payload = document()
    await submit(client, payload)
    payload["metadata"]["title"] = "Conflicting metadata"
    await submit(client, payload)
    response = await recognize(client, audio()[50 * HOP_SIZE : 50 * HOP_SIZE + 5 * SAMPLE_RATE])
    assert response.json()["status"] == "matched"
    assert response.json()["match"]["metadata"]["title"] == "Synthetic reference"


async def test_recognition_rate_limit_is_enforced(client: httpx.AsyncClient) -> None:
    """Authenticated requests still obey the private pilot's read rate budget."""
    raw = np.zeros(3 * SAMPLE_RATE, dtype="<f4")
    for _ in range(30):
        response = await recognize(client, raw)
        assert response.status_code == 200
    response = await recognize(client, raw)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"


async def test_statistics_empty_and_authenticated(client: httpx.AsyncClient) -> None:
    """Only recognition credentials can read collection and queue statistics."""
    assert (await client.get("/api/v1/dejavu/statistics")).status_code == 401
    assert (
        await client.get("/api/v1/dejavu/statistics", headers={"X-API-Key": UPLOAD})
    ).status_code == 401
    response = await client.get("/api/v1/dejavu/statistics", headers={"X-API-Key": READ})
    assert response.json() == {
        "references": {"total": 0, "partial": 0, "full": 0, "excluded": 0},
        "recordings": {"total": 0, "partial": 0, "full": 0, "excluded": 0},
        "submissions": {
            "queued": 0,
            "processing": 0,
            "processed": 0,
            "failed": 0,
            "pending_metadata": 0,
        },
        "worker_available": True,
    }


async def test_statistics_do_not_consume_recognition_budget(client: httpx.AsyncClient) -> None:
    """Regular dashboard refreshes must not prevent microphone recognition."""
    for _ in range(30):
        response = await client.get("/api/v1/dejavu/statistics", headers={"X-API-Key": READ})
        assert response.status_code == 200
    response = await client.get("/api/v1/dejavu/statistics", headers={"X-API-Key": READ})
    assert response.status_code == 429
    assert (await recognize(client, np.zeros(3 * SAMPLE_RATE, dtype="<f4"))).status_code == 200


def test_statistics_measure_exact_coverage_and_deduplicate(tmp_path: Path) -> None:
    """Count references once, distinguish tiny gaps, and report uploads separately."""
    store = Store(tmp_path / "coverage.db")
    store.initialize()
    for duration in (10, 60, 180):
        windows = plan_windows(duration)
        payload = {
            "windows": [
                {"start_sample": window.start_sample, "sample_count": window.sample_count}
                for window in windows
            ],
            "metadata": {**document()["metadata"], "duration": duration},
        }
        identifier = str(duration)
        store.submit(identifier, json.dumps(payload), 10)
        store.finish(identifier, identifier, payload["metadata"], [])
    payload = {
        "windows": [{"start_sample": 0, "sample_count": 10 * SAMPLE_RATE}],
        "metadata": {**document()["metadata"], "duration": 11},
    }
    store.submit("duplicate", json.dumps(payload), 10)
    store.finish("duplicate", "10", payload["metadata"], [])
    store.submit("pending", "{}", 10)
    assert store.claim()["id"] == "pending"
    store.submit("retrying", "{}", 10)
    store.fail("retrying", "Retry scheduled", 300)
    store.submit("rejected", "{}", 10)
    store.fail("rejected", "Invalid", None)
    assert store.statistics() == {
        "references": {"total": 3, "partial": 0, "full": 3, "excluded": 0},
        "recordings": {"total": 3, "partial": 0, "full": 3, "excluded": 0},
        "submissions": {
            "queued": 1,
            "processing": 1,
            "processed": 4,
            "failed": 1,
            "pending_metadata": 0,
        },
    }


def test_statistics_zero_gap_windows_cover_full_track(tmp_path: Path) -> None:
    """Multiple adjacent windows are full coverage only if they reach the exact end."""
    store = Store(tmp_path / "adjacent.db")
    store.initialize()
    size = 100 * HOP_SIZE
    payload = {
        "windows": [
            {"start_sample": 0, "sample_count": size},
            {"start_sample": size, "sample_count": size},
        ],
        "metadata": {**document()["metadata"], "duration": 2 * size / SAMPLE_RATE},
    }
    store.submit("adjacent", json.dumps(payload), 10)
    store.finish("adjacent", "adjacent", payload["metadata"], [])
    assert store.statistics()["references"] == {"total": 1, "partial": 0, "full": 1, "excluded": 0}


def test_short_or_identical_secrets_are_rejected(tmp_path: Path) -> None:
    """The service cannot start with weak or shared contribution/read credentials."""
    with pytest.raises(ValidationError):
        Settings(
            database=tmp_path / "x.db", upload_key=SecretStr("short"), read_key=SecretStr(READ)
        )
    with pytest.raises(ValidationError):
        Settings(database=tmp_path / "x.db", upload_key=SecretStr(READ), read_key=SecretStr(READ))
