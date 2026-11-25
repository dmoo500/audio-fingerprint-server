"""Verify the isolated PostgreSQL benchmark fixture handling."""

import json
import runpy
import sqlite3
import struct
from pathlib import Path

import pytest
from test_metadata_gate import document, use_legacy_landmark_layout

from fingerprint_server.database.sqlite import Store


def test_snapshot_and_sample_preserve_original(tmp_path: Path) -> None:
    pytest.importorskip("psycopg")
    script = Path(__file__).resolve().parents[1] / "scripts" / "postgres_benchmark.py"
    functions = runpy.run_path(str(script))
    samples, snapshot = functions["samples"], functions["snapshot"]

    original = tmp_path / "source.db"
    copied = tmp_path / "copy.db"
    store = Store(original)
    store.initialize()
    payload = document()
    fingerprints = [(f"{index:020x}", index) for index in range(32)]
    payload["fingerprints"] = fingerprints
    store.submit("sample", json.dumps(payload), 10)
    store.finish("sample", "reference", payload["metadata"], fingerprints)
    with store.connect() as db:
        use_legacy_landmark_layout(db)
        db.execute("DROP INDEX landmarks_reference")
        db.execute("PRAGMA user_version=7")
    snapshot(original, copied)
    with sqlite3.connect(original) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='landmarks_reference'"
            ).fetchone()[0]
            == 0
        )
    with sqlite3.connect(copied) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='landmarks_reference'"
            ).fetchone()[0]
            == 1
        )
        assert len(samples(db, 1)[0]) == 32
        with pytest.raises(ValueError, match="suitable published samples"):
            samples(db, 2)


def test_packed_hash_preserves_all_bits(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("psycopg")
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    functions = runpy.run_path(str(scripts / "postgres_layout_experiment.py"))
    split_hash = functions["split_hash"]
    for digest in (
        bytes(10),
        bytes.fromhex("ffffffffffffffffffff"),
        bytes.fromhex("80000000000000008000"),
        bytes.fromhex("7fffffffffffffff7fff"),
        bytes.fromhex("0123456789abcdef1234"),
    ):
        assert struct.pack("!qh", *split_hash(digest)) == digest
    with pytest.raises(ValueError, match="10 bytes"):
        split_hash(bytes(9))
