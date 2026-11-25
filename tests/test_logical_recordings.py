"""Verified logical recordings retain independent acoustic references."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID

from audio_fingerprint import SAMPLE_RATE

from fingerprint_server.database.sqlite import Store
from fingerprint_server.database.sqlite.migrations import DB_SCHEMA_VERSION

RECORDING_ID = "e9c2a2a3-4f4b-4b6a-9c31-3a8b2f3c7f10"
OTHER_RECORDING_ID = "a61ad03c-8df0-44bf-b999-25d9997d2a24"


def metadata(recording_id: str = RECORDING_ID, duration: float = 2) -> dict[str, object]:
    """Return complete synthetic metadata with a verified recording identifier."""
    return {
        "duration": duration,
        "title": "Synthetic recording",
        "artists": ["Test artist"],
        "album": "Test album",
        "recording_mbid": recording_id,
    }


def publish(
    store: Store,
    submission_id: str,
    reference_id: str,
    reference_metadata: dict[str, object],
    windows: list[dict[str, int]],
    hashes: list[tuple[str, int]] | None = None,
    verified: bool = True,
) -> None:
    """Publish a reference from a minimal retained submission document."""
    document = json.dumps({"windows": windows})
    store.submit(submission_id, document, 10)
    store.finish(
        submission_id,
        reference_id,
        reference_metadata,
        hashes or [],
        verified=verified,
    )


def test_verified_variants_count_as_one_recording_and_full_variant_upgrades_coverage(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "recordings.db")
    store.initialize()
    publish(
        store,
        "partial-submission",
        "partial-reference",
        metadata(),
        [{"start_sample": 0, "sample_count": SAMPLE_RATE}],
    )
    publish(
        store,
        "full-submission",
        "full-reference",
        metadata(RECORDING_ID.upper(), duration=1),
        [{"start_sample": 0, "sample_count": SAMPLE_RATE}],
    )
    with store.connect() as db:
        db.executemany(
            "INSERT INTO reference_tracks"
            "(reference_key,metadata,created_at,publication_ready,verification_version,"
            "recording_key) VALUES(?,?,?,0,1,?)",
            [
                (
                    "excluded-variant",
                    json.dumps(metadata()),
                    3,
                    UUID(RECORDING_ID).hex,
                ),
                (
                    "excluded-only-recording",
                    json.dumps(metadata(OTHER_RECORDING_ID)),
                    4,
                    UUID(OTHER_RECORDING_ID).hex,
                ),
            ],
        )

    assert store.statistics()["references"] == {
        "total": 2,
        "partial": 1,
        "full": 1,
        "excluded": 2,
    }
    assert store.statistics()["recordings"] == {
        "total": 1,
        "partial": 0,
        "full": 1,
        "excluded": 1,
    }
    with store.connect() as db:
        rows = db.execute(
            "SELECT reference_key,recording_key FROM reference_tracks "
            "WHERE publication_ready=1 ORDER BY reference_key"
        ).fetchall()
    assert [(row["reference_key"], row["recording_key"]) for row in rows] == [
        ("full-reference", UUID(RECORDING_ID).hex),
        ("partial-reference", UUID(RECORDING_ID).hex),
    ]


def test_match_returns_best_variant_and_keeps_distinct_recording_competitor(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "matches.db")
    store.initialize()
    query = [(f"{index:020x}", index) for index in range(50)]
    publish(
        store,
        "best-submission",
        "best-variant",
        metadata(),
        [{"start_sample": 0, "sample_count": 2 * SAMPLE_RATE}],
        query[:30],
    )
    publish(
        store,
        "alternate-submission",
        "alternate-variant",
        {**metadata(), "title": "Alternate title"},
        [{"start_sample": 0, "sample_count": 2 * SAMPLE_RATE}],
        query[:29],
    )
    publish(
        store,
        "other-submission",
        "other-recording",
        metadata(OTHER_RECORDING_ID),
        [{"start_sample": 0, "sample_count": 2 * SAMPLE_RATE}],
        query[:20],
    )

    matches = store.match(query)
    assert [row["reference_id"] for row in matches] == ["best-variant", "other-recording"]
    assert [row["matched_landmarks"] for row in matches] == [30, 20]


def test_verified_duplicate_completes_existing_excluded_reference(tmp_path: Path) -> None:
    store = Store(tmp_path / "incomplete.db")
    store.initialize()
    store.submit("old-submission", "{}", 10)
    store.pending_metadata("old-submission")
    store.submit(
        "new-submission",
        json.dumps({"windows": [{"start_sample": 0, "sample_count": SAMPLE_RATE}]}),
        10,
    )
    with store.connect() as db:
        db.execute(
            "INSERT INTO reference_tracks"
            "(reference_key,metadata,created_at,publication_ready,verification_version) "
            "VALUES('same-acoustic-reference',?,1,0,0)",
            (json.dumps({"duration": 1, "title": "", "artists": [], "album": None}),),
        )
        reference = db.execute(
            "SELECT id FROM reference_tracks WHERE reference_key='same-acoustic-reference'"
        ).fetchone()
        db.execute(
            "INSERT INTO landmarks VALUES(?,?,?)", (bytes.fromhex("0" * 20), reference[0], 0)
        )

    store.finish(
        "new-submission",
        "same-acoustic-reference",
        metadata(duration=1),
        [("0" * 20, 0)],
        verified=True,
    )

    with store.connect() as db:
        reference = db.execute(
            "SELECT metadata,publication_ready,recording_key FROM reference_tracks "
            "WHERE reference_key='same-acoustic-reference'"
        ).fetchone()
        assert db.execute("SELECT count(*) FROM landmarks").fetchone()[0] == 1
    assert json.loads(reference["metadata"])["recording_mbid"] == RECORDING_ID
    assert reference["publication_ready"] == 1
    assert reference["recording_key"] == UUID(RECORDING_ID).hex
    assert store.get_submission("old-submission")["state"] == "pending_metadata"
    assert store.get_submission("new-submission")["state"] == "processed"


def test_schema_six_upgrade_groups_only_currently_verified_mbid_without_data_loss(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "schema-six.db")
    store.initialize()
    publish(
        store,
        "verified-submission",
        "verified-reference",
        metadata(RECORDING_ID.upper()),
        [{"start_sample": 0, "sample_count": SAMPLE_RATE}],
        [("0" * 20, 0)],
    )
    publish(
        store,
        "verified-alternate-submission",
        "verified-alternate-reference",
        metadata(RECORDING_ID),
        [{"start_sample": 0, "sample_count": SAMPLE_RATE}],
        [("3" * 20, 3)],
    )
    publish(
        store,
        "unverified-submission",
        "unverified-reference",
        metadata(RECORDING_ID),
        [{"start_sample": 0, "sample_count": SAMPLE_RATE}],
        [("1" * 20, 1)],
        verified=False,
    )
    with store.connect() as db:
        db.execute(
            "INSERT INTO reference_tracks"
            "(reference_key,metadata,created_at,publication_ready,verification_version) "
            "VALUES('malformed-reference','[',3,0,1)"
        )
        db.execute(
            "INSERT INTO landmarks VALUES(?,?,?)",
            (
                bytes.fromhex("2" * 20),
                db.execute(
                    "SELECT id FROM reference_tracks WHERE reference_key='malformed-reference'"
                ).fetchone()[0],
                2,
            ),
        )
        before_references = db.execute(
            "SELECT reference_key,metadata,created_at,publication_ready,verification_version "
            "FROM reference_tracks ORDER BY reference_key"
        ).fetchall()
        before_landmarks = db.execute(
            "SELECT hash,reference_id,offset FROM landmarks ORDER BY hash,reference_id,offset"
        ).fetchall()
        before_submissions = db.execute(
            "SELECT id,reference_id FROM submissions ORDER BY id"
        ).fetchall()
        db.execute("DROP INDEX references_recording")
        db.execute("ALTER TABLE reference_tracks DROP COLUMN recording_key")
        db.execute("PRAGMA user_version=6")

    store.initialize()
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT reference_key,metadata,created_at,publication_ready,verification_version "
                "FROM reference_tracks ORDER BY reference_key"
            )
        ] == [tuple(row) for row in before_references]
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT hash,reference_id,offset FROM landmarks ORDER BY hash,reference_id,offset"
            )
        ] == [tuple(row) for row in before_landmarks]
        assert [
            tuple(row) for row in db.execute("SELECT id,reference_id FROM submissions ORDER BY id")
        ] == [tuple(row) for row in before_submissions]
        recording_keys = {
            row["reference_key"]: row["recording_key"]
            for row in db.execute("SELECT reference_key,recording_key FROM reference_tracks")
        }
    assert recording_keys == {
        "verified-reference": UUID(RECORDING_ID).hex,
        "verified-alternate-reference": UUID(RECORDING_ID).hex,
        "unverified-reference": None,
        "malformed-reference": None,
    }
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 6
        assert backup.execute("SELECT count(*) FROM reference_tracks").fetchone()[0] == 4
        assert backup.execute("SELECT count(*) FROM landmarks").fetchone()[0] == 4
        assert {row[0] for row in backup.execute("SELECT id FROM submissions")} == {
            "verified-submission",
            "verified-alternate-submission",
            "unverified-submission",
        }
