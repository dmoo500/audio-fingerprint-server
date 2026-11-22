"""Derived SQLite submission metadata for administrator queries."""

import sqlite3

TABLE = """
CREATE TABLE IF NOT EXISTS submission_metadata (
    id TEXT PRIMARY KEY REFERENCES submissions(id) ON DELETE CASCADE,
    metadata TEXT,
    state TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL,
    pending_rechecks INTEGER NOT NULL,
    created_at REAL NOT NULL,
    retry_at REAL NOT NULL,
    pending_due_at REAL NOT NULL,
    reference_id TEXT
)
"""

EXTRACT = "CASE WHEN json_valid(NEW.document) THEN json_extract(NEW.document,'$.metadata') END"
FIELDS = (
    "state",
    "error",
    "attempts",
    "pending_rechecks",
    "created_at",
    "retry_at",
    "pending_due_at",
    "reference_id",
)
FIELD_LIST = ",".join(FIELDS)
NEW_VALUES = ",".join(f"NEW.{field}" for field in FIELDS)
UPDATES = ",".join(f"{field}=NEW.{field}" for field in FIELDS)

TRIGGERS = (
    f"""CREATE TRIGGER IF NOT EXISTS submission_metadata_insert AFTER INSERT ON submissions
        BEGIN INSERT INTO submission_metadata(id,metadata,{FIELD_LIST})
        VALUES(NEW.id,{EXTRACT},{NEW_VALUES}); END""",
    f"""CREATE TRIGGER IF NOT EXISTS submission_metadata_update
        AFTER UPDATE OF document ON submissions
        BEGIN UPDATE submission_metadata SET metadata={EXTRACT} WHERE id=NEW.id; END""",
    f"""CREATE TRIGGER IF NOT EXISTS submission_metadata_fields
        AFTER UPDATE OF {FIELD_LIST} ON submissions
        BEGIN UPDATE submission_metadata SET {UPDATES} WHERE id=NEW.id; END""",
    """CREATE TRIGGER IF NOT EXISTS submission_metadata_delete AFTER DELETE ON submissions
        BEGIN DELETE FROM submission_metadata WHERE id=OLD.id; END""",
)


def install_projection(db: sqlite3.Connection) -> None:
    """Create and populate the derived submission metadata projection."""
    db.execute(TABLE)
    for trigger in TRIGGERS:
        db.execute(trigger)
    db.execute("DELETE FROM submission_metadata")
    db.execute(
        f"INSERT INTO submission_metadata(id,metadata,{FIELD_LIST}) "
        "SELECT id,CASE WHEN json_valid(document) THEN json_extract(document,'$.metadata') END "
        f",{FIELD_LIST} FROM submissions"
    )
