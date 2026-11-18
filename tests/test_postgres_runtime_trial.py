"""Smoke-test the complete rehearsal before running it on a full collection."""

import json
import os
import runpy
from pathlib import Path

import pytest

from fingerprint_server.database.sqlite import Store


def test_isolated_runtime_and_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("FP_TEST_POSTGRES") != "1":
        pytest.skip("Set FP_TEST_POSTGRES=1 for disposable PostgreSQL rehearsal tests")
    pytest.importorskip("psycopg")
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    trial = runpy.run_path(str(scripts / "postgres_runtime_trial.py"))
    source = tmp_path / "source.db"
    store = Store(source)
    store.initialize()
    for index in range(4):
        pairs = [(f"{index * 1000 + offset:020x}", offset) for offset in range(50)]
        metadata = {
            "title": f"Trial {index}",
            "artists": ["Trial"],
            "album": "Trial",
            "duration": 12,
            "recording_mbid": f"00000000-0000-4000-8000-{index + 10:012d}",
        }
        document = {
            "format_id": "ma-dejavu-landmarks-v2",
            "sample_rate": 44100,
            "offset_unit": "fft_hops",
            "windows": [{"start_sample": 0, "sample_count": 529200}],
            "fingerprints": pairs,
            "metadata": metadata,
        }
        store.submit(str(index), json.dumps(document), 10)
        store.finish(str(index), str(index), metadata, pairs, verified=True)
    before = source.read_bytes()
    report = tmp_path / "result.json"
    trial["run_trial"](source, report, 4, 1)
    result = json.loads(report.read_text())
    assert result["status"] == "passed"
    assert result["import_counts"]["landmarks"] == 200
    assert result["recognition"]["samples"] == 4
    assert result["backup"]["recognition_samples"] == 4
    assert result["runtime"]["synthetic_pcm_match"] == "passed"
    assert result["reverse_export"]["counts"]["landmarks"] > 200
    assert result["reverse_export"]["recognition"]["samples"] == 4
    assert result["stub_musicbrainz_calls"] == 1
    assert source.read_bytes() == before
