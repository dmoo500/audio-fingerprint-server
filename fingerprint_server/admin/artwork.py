"""Read-only artwork cache coverage for the private dashboard."""

import time

from fingerprint_server.database import Storage
from fingerprint_server.database.sqlite import Store


def artwork_statistics(store: Storage) -> dict[str, int] | None:
    """Count current cache results for published references eligible for artwork lookup."""
    sqlite = isinstance(store, Store)
    with store.connect() as db:
        if db.execute("SELECT 1 FROM service_state WHERE key='artwork_schema'").fetchone() is None:
            return None
        album = "json_extract(t.metadata,'$.album')" if sqlite else "t.metadata::jsonb->>'album'"
        mbid = (
            "json_extract(t.metadata,'$.recording_mbid')"
            if sqlite
            else "t.metadata::jsonb->>'recording_mbid'"
        )
        rows = db.execute(
            "SELECT a.state,a.expires_at,count(*) AS count FROM reference_tracks t "
            "LEFT JOIN artwork_requests a ON a.reference_key=t.reference_key "
            f"WHERE t.publication_ready=1 AND trim({album})<>'' AND trim({mbid})<>'' "
            "GROUP BY a.state,a.expires_at"
        ).fetchall()
    counts: dict[str, int] = dict.fromkeys(
        ("eligible", "ready", "missing", "pending", "unavailable", "expired", "unqueried"), 0
    )
    now = time.time()
    for row in rows:
        counts["eligible"] += row["count"]
        state = row["state"]
        if state is None:
            state = "unqueried"
        elif row["expires_at"] <= now:
            state = "expired"
        if state not in counts or state == "eligible":
            raise ValueError(f"Unknown artwork cache state: {state}")
        counts[state] += row["count"]
    return counts
