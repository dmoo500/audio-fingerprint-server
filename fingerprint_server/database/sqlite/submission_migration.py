"""Lossless SQLite submission state migration."""

import re
import sqlite3

from fingerprint_server.database.codes import SUBMISSION_ERRORS, SubmissionState
from fingerprint_server.database.sqlite.metadata import install_projection


def _migrate_submission_codes(db: sqlite3.Connection) -> None:
    """Rebuild the submission tables with integer-affinity state columns."""
    objects = db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE tbl_name IN ('submissions','submission_metadata') "
        "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    for row in objects:
        if row[0] == "trigger":
            db.execute('DROP TRIGGER "' + row[1].replace('"', '""') + '"')

    states = " ".join(
        f"WHEN '{state.name.lower()}' THEN {state.value}"
        for state in SubmissionState
        if state is not SubmissionState.UNKNOWN
    )
    known = ",".join(
        "'" + state.name.lower() + "'"
        for state in SubmissionState
        if state is not SubmissionState.UNKNOWN
    )
    state_sql = (
        f"CASE WHEN typeof(state)='integer' AND state BETWEEN 0 AND "
        f"{SubmissionState.UNKNOWN.value} THEN state "
        f"ELSE CASE state {states} ELSE {SubmissionState.UNKNOWN.value} END END"
    )
    error_sql = SUBMISSION_ERRORS.encoded_sql("error")
    projection_exists = (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='submission_metadata'"
        ).fetchone()
        is not None
    )
    for table in ("submissions", "submission_metadata") if projection_exists else ("submissions",):
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Missing SQLite table {table} during migration")
        definition, name_count = re.subn(
            rf'(?i)^(CREATE\s+TABLE\s+)(?:IF\s+NOT\s+EXISTS\s+)?(?:"{table}"|{table})(?=\s*\()',
            rf'\1"{table}_new"',
            row[0],
        )
        definition, type_count = re.subn(
            r"(?i)\bstate\s+(?:TEXT|INTEGER)\b", "state INTEGER", definition, count=1
        )
        if name_count != 1 or type_count != 1:
            raise RuntimeError(f"Unrecognized SQLite table definition: {table}")
        db.execute(definition)
        columns = [item[1] for item in db.execute(f"PRAGMA table_info({table})")]
        had_raw = "state_raw" in columns
        if not had_raw:
            db.execute(f'ALTER TABLE "{table}_new" ADD COLUMN state_raw TEXT')
            columns.append("state_raw")
        raw_sql = (
            f"CASE WHEN typeof(state)='integer' AND state={SubmissionState.UNKNOWN.value} "
            f"THEN {'state_raw' if had_raw else 'CAST(state AS TEXT)'} "
            f"WHEN typeof(state)='integer' AND state BETWEEN 0 AND "
            f"{SubmissionState.PENDING_METADATA.value} THEN NULL "
            f"WHEN state IN ({known}) THEN NULL ELSE state END"
        )
        names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
        values = ",".join(
            (
                f'(SELECT "{name}" FROM submissions WHERE id="{table}".id)'
                if table == "submission_metadata" and name in ("state", "state_raw", "error")
                else state_sql
                if name == "state"
                else raw_sql
                if name == "state_raw"
                else error_sql
                if name == "error"
                else f'"{name}"'
            )
            for name in columns
        )
        db.execute(f'INSERT INTO "{table}_new"(rowid,{names}) SELECT rowid,{values} FROM "{table}"')
        db.execute(f'DROP TABLE "{table}"')
        db.execute(f'ALTER TABLE "{table}_new" RENAME TO "{table}"')

    for row in objects:
        db.execute(row[3])
    if not projection_exists:
        install_projection(db)
    else:
        # Older projection triggers need to propagate the newly added state_raw column.
        for name in ("submission_metadata_insert", "submission_metadata_fields"):
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
        from fingerprint_server.database.sqlite.metadata import TRIGGERS

        for trigger in TRIGGERS:
            db.execute(trigger)
