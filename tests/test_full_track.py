"""Full-track recognition, legacy references and bounded durable ingestion."""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest
import test_server
from audio_fingerprint import (
    FORMAT_ID,
    HOP_SIZE,
    LEGACY_FORMAT_ID,
    SAMPLE_RATE,
    WindowSampler,
    fingerprint,
)
from pydantic import ValidationError
from test_server import UPLOAD, audio, recognize, submit

from fingerprint_server.database.sqlite import QueueFullError, Store
from fingerprint_server.schemas import Submission

client = test_server.client


def full_document(samples: np.ndarray) -> dict:
    """Build a reference by feeding the production streaming sampler."""
    sampler = WindowSampler(len(samples) / SAMPLE_RATE, SAMPLE_RATE, 1, "pcm_f32le")
    hashes = []
    for start in range(0, len(samples), SAMPLE_RATE):
        hashes.extend(sampler.feed(samples[start : start + SAMPLE_RATE].tobytes()))
    hashes.extend(sampler.feed(b"", last=True))
    return {
        "format_id": FORMAT_ID,
        "sample_rate": SAMPLE_RATE,
        "offset_unit": "fft_hops",
        "windows": [
            {"start_sample": window.start_sample, "sample_count": window.sample_count}
            for window in sampler.completed
        ],
        "fingerprints": hashes,
        "metadata": {
            "duration": len(samples) / SAMPLE_RATE,
            "title": "Full reference",
            "artists": ["Test"],
            "album": "Synthetic album",
            "isrc": "USAAA2600001",
        },
    }


async def test_all_passages_and_window_boundaries_are_recognizable(
    client: httpx.AsyncClient,
) -> None:
    samples = audio(85)
    payload = full_document(samples)
    assert len(payload["windows"]) > 3
    identifier = await submit(client, payload)
    response = await client.get(
        f"/api/v1/dejavu/submissions/{identifier}", headers={"X-API-Key": UPLOAD}
    )
    assert response.json()["state"] == "processed"
    for start in [0, 8, 18, 19.5, 20, 28, 38, 39.5, 40, 49, 59, 60, 68, 78]:
        offset = round(start * SAMPLE_RATE / HOP_SIZE) * HOP_SIZE
        response = await recognize(client, samples[offset : offset + 5 * SAMPLE_RATE])
        assert response.json()["status"] == "matched", (start, response.text)
        assert response.json()["match"]["offset_seconds"] == pytest.approx(
            offset / SAMPLE_RATE, abs=HOP_SIZE / SAMPLE_RATE
        )


async def test_old_reference_does_not_make_new_full_reference_ambiguous(
    client: httpx.AsyncClient,
) -> None:
    samples = audio(65)
    full = full_document(samples)
    size = (30 * SAMPLE_RATE // HOP_SIZE) * HOP_SIZE
    legacy = {
        **full,
        "format_id": LEGACY_FORMAT_ID,
        "windows": [{"start_sample": 0, "sample_count": size}],
        "fingerprints": fingerprint(samples[:size]),
    }
    await submit(client, legacy)
    await submit(client, full)
    response = await recognize(client, samples[5 * SAMPLE_RATE : 10 * SAMPLE_RATE])
    assert response.json()["status"] == "matched", response.text


async def test_full_track_document_can_exceed_legacy_limits(client: httpx.AsyncClient) -> None:
    payload = full_document(audio(300))
    assert len(payload["fingerprints"]) > 100000
    assert len(json.dumps(payload).encode()) > 2_000_000
    await submit(client, payload)
    response = await recognize(client, audio(300)[270 * SAMPLE_RATE : 275 * SAMPLE_RATE])
    assert response.json()["status"] == "matched", response.text


@pytest.mark.parametrize("mutation", ["gap", "tail", "start", "out_of_range", "legacy_overlap"])
def test_invalid_full_coverage_is_rejected(mutation: str) -> None:
    payload = full_document(audio(35))
    if mutation == "gap":
        payload["windows"][1]["start_sample"] = 35 * SAMPLE_RATE
    elif mutation == "tail":
        payload["metadata"]["duration"] += 1
    elif mutation == "start":
        payload["windows"][0]["start_sample"] = HOP_SIZE
    elif mutation == "out_of_range":
        payload["fingerprints"].append(("0" * 20, 35 * SAMPLE_RATE // HOP_SIZE))
    else:
        payload["format_id"] = LEGACY_FORMAT_ID
    with pytest.raises(ValidationError):
        Submission.model_validate_json(json.dumps(payload))


def test_queue_byte_budget_includes_processing_and_preserves_duplicates(tmp_path) -> None:
    store = Store(tmp_path / "bounded.db")
    store.initialize()
    store.submit("first", "12345", 10, byte_limit=10)
    assert store.claim()["id"] == "first"
    store.submit("second", "67890", 10, byte_limit=10)
    assert store.submit("first", "12345", 10, byte_limit=10)["state"] == "processing"
    with pytest.raises(QueueFullError):
        store.submit("third", "x", 10, byte_limit=10)
