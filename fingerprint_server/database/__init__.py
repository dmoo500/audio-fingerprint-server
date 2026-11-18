"""SQLite/PostgreSQL storage selection without silent backend fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fingerprint_server.database.sqlite import Store
from fingerprint_server.settings import Settings

if TYPE_CHECKING:
    from fingerprint_server.database.postgres import PostgresStore

type Storage = Store | PostgresStore


def create_store(settings: Settings) -> Storage:
    """Create the configured backend without opening or migrating storage."""
    if settings.database_backend == "postgres":
        try:
            from fingerprint_server.database.postgres import PostgresStore
        except ModuleNotFoundError as err:
            if err.name != "psycopg":
                raise
            raise RuntimeError(
                "PostgreSQL requires installation with the [postgres] extra"
            ) from err

        return PostgresStore(settings)
    return Store(
        settings.database,
        settings.musicbrainz_enabled,
        settings.housekeeping_enabled,
        settings.pending_max_rechecks,
        settings.pending_recheck_days,
        settings.background_recheck_queue_window,
    )
