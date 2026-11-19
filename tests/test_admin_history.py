"""Verify durable dashboard sampling and gaps in hourly plots."""

from pathlib import Path

from fingerprint_server.admin.history import RECORDINGS, chart
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
        dict(hour=1000, recordings=3, queued=1, processing=1, pending_metadata=2, manual_review=1),
        dict(hour=1002, recordings=3, queued=0, processing=1, pending_metadata=2, manual_review=1),
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
