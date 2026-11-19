"""Additive MusicBrainz review schema shared by both storage backends."""

REVIEW_TABLE = """
CREATE TABLE IF NOT EXISTS musicbrainz_reviews (
    submission_id TEXT PRIMARY KEY REFERENCES submissions(id) ON DELETE CASCADE,
    candidates TEXT NOT NULL,
    selected_mbid TEXT
)
"""
