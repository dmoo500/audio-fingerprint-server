"""Verify durable dashboard sampling and gaps in hourly plots."""

from pathlib import Path

from fingerprint_server.admin.history import ARTWORK, QUEUE, RECORDINGS, chart
from fingerprint_server.artwork import ArtworkStore
from fingerprint_server.database.metrics import HISTORY_HOURS, capture_hour
from fingerprint_server.database.sqlite import Store


def assert_hourly_sampling(store) -> None:
    """Exercise the same aggregate query on both storage backends."""
    with store.connect() as db:
        db.execute(
            "INSERT INTO reference_tracks(reference_key,metadata,created_at,"
            "publication_ready,recording_key) VALUES "
            "('ref-a','{}',0,1,'same'),('ref-b','{}',0,1,'same'),"
            "('ref-c','{}',0,1,NULL),('ref-d','{}',0,1,NULL),('hidden','{}',0,0,'other')"
        )
        db.execute(
            "INSERT INTO submissions(id,document,state,created_at) VALUES "
            "('waiting','{}','queued',0),('busy','{}','processing',0),"
            "('incomplete','{}','pending_metadata',0),('ambiguous','{}','pending_metadata',0),"
            "('done','{}','processed',0)"
        )
        db.execute("INSERT INTO musicbrainz_reviews VALUES ('ambiguous','[]',NULL)")
    capture_hour(store, 1000)
    with store.connect() as db:
        db.execute("UPDATE submissions SET state='processed' WHERE id='waiting'")
    capture_hour(store, 1000)
    capture_hour(store, 1002)
    store.initialize()
    with store.connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM admin_hourly_metrics ORDER BY hour")]
    assert rows == [
        dict(
            hour=1000,
            recordings=3,
            queued=1,
            processing=1,
            pending_metadata=2,
            manual_review=1,
            cached_artwork=None,
        ),
        dict(
            hour=1002,
            recordings=3,
            queued=0,
            processing=1,
            pending_metadata=2,
            manual_review=1,
            cached_artwork=None,
        ),
    ]
    capture_hour(store, 1000 + HISTORY_HOURS + 1)
    with store.connect() as db:
        hours = [
            row["hour"] for row in db.execute("SELECT hour FROM admin_hourly_metrics ORDER BY hour")
        ]
    assert hours == [1002, 1000 + HISTORY_HOURS + 1]


def test_sqlite_hourly_sampling(tmp_path: Path) -> None:
    store = Store(tmp_path / "fingerprints.db", musicbrainz_enabled=False)
    store.initialize()
    assert_hourly_sampling(store)


def test_chart_does_not_connect_missing_hours() -> None:
    assert not chart([], 1004, RECORDINGS)
    samples = [
        dict(hour=1000, recordings=2),
        dict(hour=1001, recordings=4),
        dict(hour=1003, recordings=6),
    ]
    result = chart(samples, 1004, RECORDINGS)
    assert result["maximum"] == 6
    assert result["series"][0]["path"] == "M0.0,66.67 L150.0,33.33 M450.0,0.0"
    assert result["series"][0]["dots"] == [(0, 66.67), (450, 0)]


def test_artwork_history_migration_and_capture(tmp_path: Path) -> None:
    store = Store(tmp_path / "fingerprints.db")
    store.initialize()
    with store.connect() as db:
        db.execute("DROP TABLE admin_hourly_metrics")
        db.execute(
            "CREATE TABLE admin_hourly_metrics(hour bigint PRIMARY KEY,recordings bigint NOT NULL,"
            "queued bigint NOT NULL,processing bigint NOT NULL,pending_metadata bigint NOT NULL,"
            "manual_review bigint NOT NULL)"
        )
        db.execute("INSERT INTO admin_hourly_metrics VALUES(1000,1,2,3,4,5)")
    store.initialize()
    store.initialize()
    ArtworkStore(store).initialize()
    with store.connect() as db:
        db.execute(
            "INSERT INTO reference_tracks(reference_key,metadata,created_at,publication_ready) "
            'VALUES(\'ready\',\'{"album":"Album","recording_mbid":"id"}\',0,1),'
            '(\'unpublished\',\'{"album":"Album","recording_mbid":"id"}\',0,0),'
            "('ineligible','{}',0,1)"
        )
        db.executemany(
            "INSERT INTO artwork_requests VALUES(?,'ready','https://archive.org/a',0,?,NULL)",
            [("ready", 9999999999), ("unpublished", 9999999999), ("ineligible", 9999999999)],
        )
    capture_hour(store, 1001)
    with store.connect() as db:
        rows = db.execute(
            "SELECT hour,cached_artwork FROM admin_hourly_metrics ORDER BY hour"
        ).fetchall()
    assert [tuple(row) for row in rows] == [(1000, None), (1001, 1)]
    capture_hour(store, 1000)
    with store.connect() as db:
        row = db.execute("SELECT queued,cached_artwork FROM admin_hourly_metrics WHERE hour=1000")
        assert tuple(row.fetchone()) == (2, 1)


def test_backlog_chart_uses_separate_scale_and_gaps_for_artwork() -> None:
    base = dict(queued=4, processing=2, pending_metadata=1, manual_review=1)
    samples = [
        dict(hour=1000, cached_artwork=None, **base),
        dict(hour=1001, cached_artwork=800, **base),
        dict(hour=1002, cached_artwork=None, **base),
        dict(hour=1003, cached_artwork=1000, **base),
    ]
    result = chart(samples, 1003, QUEUE, ARTWORK)
    assert result["maximum"] == 4
    assert result["secondary_maximum"] == 1000
    cover = result["series"][-1]
    assert cover["last"] == 1000
    assert cover["axis"] == "right"
    assert cover["path"] == "M200.0,20.0 M600.0,0.0"
