"""Verify SQLite coverage statistics do not retain all fingerprint documents."""

import json
import tracemalloc
from pathlib import Path

from audio_fingerprint import SAMPLE_RATE

from fingerprint_server.database.codes import SubmissionState
from fingerprint_server.database.sqlite import Store


def test_statistics_streams_large_documents(tmp_path: Path) -> None:
    store = Store(tmp_path / "statistics.db")
    store.initialize()
    document = json.dumps(
        {
            "windows": [{"start_sample": 0, "sample_count": SAMPLE_RATE}],
            "fingerprints": "x" * 1_000_000,
        }
    )
    metadata = json.dumps({"duration": 1})
    with store.connect() as db:
        for index in range(32):
            reference = f"reference-{index}"
            db.execute(
                "INSERT INTO reference_tracks(reference_key,metadata,created_at,publication_ready) "
                "VALUES(?,?,0,1)",
                (reference, metadata),
            )
            db.execute(
                "INSERT INTO submissions(id,document,state,reference_id,created_at) "
                "VALUES(?,?,?,?,0)",
                (f"submission-{index}", document, SubmissionState.PROCESSED, reference),
            )
    tracemalloc.start()
    try:
        result = store.statistics()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result["references"] == {"total": 32, "full": 32, "partial": 0, "excluded": 0}
    assert peak < 12_000_000
