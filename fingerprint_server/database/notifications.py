"""Best-effort cross-process notifications for committed SQLite writes."""

import errno
import hashlib
import logging
import socket
import sqlite3
from pathlib import Path

LOGGER = logging.getLogger(__name__)
RECOGNITION_TABLE = "recognition_metrics"
TRACKED_TABLES = {
    "submissions",
    "reference_tracks",
    "musicbrainz_reviews",
    "artwork_requests",
    "recognition_metrics",
}


def socket_name(path: Path) -> str:
    """Return a short socket name unique to the database file."""
    digest = hashlib.sha256(path.name.encode()).hexdigest()[:16]
    return f".fp-live-{digest}.sock"


def postgres_channel(schema: str) -> str:
    """Return a database-schema-specific notification channel."""
    digest = hashlib.sha256(schema.encode()).hexdigest()[:32]
    return f"fingerprint_admin_{digest}"


def track_changes(db: sqlite3.Connection) -> set[str]:
    """Track tables affected by statements on a connection."""
    changed: set[str] = set()

    def authorize(action: int, table: str | None, *_args: str | None) -> int:
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            if table in TRACKED_TABLES:
                changed.add(table)
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorize)
    return changed


def notify_sqlite(path: Path, tables: set[str]) -> None:
    """Notify the local administrator after a successful transaction."""
    if not tables:
        return
    category = "all" if tables - {RECOGNITION_TABLE} else "recognition"
    socket_path = path.resolve().parent / socket_name(path)
    if not socket_path.exists():
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            sender.sendto(category.encode(), str(socket_path))
    except OSError as err:
        if err.errno not in (errno.ENOENT, errno.ECONNREFUSED):
            LOGGER.warning("Could not send admin change notification: %s", err)
