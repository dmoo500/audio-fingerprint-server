"""Artwork metrics distinguish resolved lookups from collection coverage."""

import json
import time
from pathlib import Path

from test_postgres_migration import postgres as postgres
from test_server import settings

from fingerprint_server.admin.artwork import artwork_statistics
from fingerprint_server.artwork import ArtworkStore
from fingerprint_server.database.codes import ArtworkState, encode_artwork_state
from fingerprint_server.database.metrics import capture_hour
from fingerprint_server.database.sqlite import Store


def test_artwork_metrics_exclude_unpublished_ineligible_and_expired_results(tmp_path: Path) -> None:
    store = Store(tmp_path / "metrics.db")
    store.initialize()
    assert artwork_statistics(store) is None
    ArtworkStore(store).initialize()
    now = time.time()
    with store.connect() as db:
        for index, state in enumerate(
            [
                "ready",
                "missing",
                "pending",
                "unavailable",
                "expired",
                "unqueried",
                "hidden",
                "no-id",
                "unknown-state",
            ]
        ):
            metadata = {"album": "Example", "recording_mbid": "test-recording"}
            if state == "no-id":
                metadata.pop("recording_mbid")
            db.execute(
                "INSERT INTO reference_tracks(reference_key,metadata,created_at,publication_ready)"
                "VALUES(?,?,0,?)",
                (str(index), json.dumps(metadata), int(state != "hidden")),
            )
            if state != "unqueried":
                db.execute(
                    "INSERT INTO artwork_requests(reference_key,state,state_raw,url,due_at,"
                    "expires_at) "
                    "VALUES(?,?,?,NULL,0,?)",
                    (
                        str(index),
                        encode_artwork_state(
                            "ready" if state in ("expired", "hidden", "no-id") else state
                        ),
                        state if state == "unknown-state" else None,
                        now - 1 if state == "expired" else now + 60,
                    ),
                )
    assert artwork_statistics(store) == {
        "eligible": 7,
        "ready": 1,
        "missing": 1,
        "pending": 1,
        "unavailable": 1,
        "expired": 1,
        "unqueried": 1,
        "unknown": 1,
    }


def test_postgres_artwork_metrics(tmp_path: Path, postgres: object) -> None:
    from fingerprint_server.database.postgres import PostgresStore

    config = settings(tmp_path, database_backend="postgres", postgres_schema="artwork_metrics")
    store = PostgresStore(config)
    store.initialize()
    ArtworkStore(store).initialize()
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO reference_tracks(reference_key,metadata,created_at,publication_ready)"
            "VALUES('example',%s,0,1)",
            (json.dumps({"album": "Example", "recording_mbid": "test-recording"}),),
        )
        db.execute(
            "INSERT INTO artwork_requests(reference_key,state,due_at,expires_at)"
            "VALUES('example',%s,0,%s)",
            (ArtworkState.READY, time.time() + 60),
        )
        db.execute(
            "INSERT INTO reference_tracks(reference_key,metadata,created_at,publication_ready)"
            "VALUES('other',%s,0,1)",
            (json.dumps({"album": "Example", "recording_mbid": "test-recording"}),),
        )
        db.execute(
            "INSERT INTO artwork_requests(reference_key,state,state_raw,due_at,expires_at)"
            "VALUES('other',%s,'unexpected',0,%s)",
            (ArtworkState.UNKNOWN, time.time() + 60),
        )
    counts = artwork_statistics(store)
    assert counts is not None
    assert counts["ready"] == counts["unknown"] == 1
    assert counts["eligible"] == 2
    capture_hour(store, 1000)
    with store.connect() as db:
        assert (
            db.execute("SELECT cached_artwork FROM admin_hourly_metrics").fetchone()[
                "cached_artwork"
            ]
            == 1
        )
