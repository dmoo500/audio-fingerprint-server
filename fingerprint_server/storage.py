"""Explicit storage selection without a silent fallback between databases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fingerprint_server.settings import Settings
from fingerprint_server.store import Store

if TYPE_CHECKING:
    from fingerprint_server.postgres_store import PostgresStore

type Storage = Store | PostgresStore


def create_store(settings: Settings) -> Storage:
    """Create the configured backend without opening or migrating storage."""
    if settings.database_backend == "postgres":
        try:
            from fingerprint_server.postgres_store import PostgresStore
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
