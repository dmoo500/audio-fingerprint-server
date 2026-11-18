"""Independently versioned PostgreSQL application credentials; never stores raw keys."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from fingerprint_server.database.postgres import Connection, PostgresStore

REGISTRY_VERSION = 1


def initialize(db: Connection) -> None:
    """Create or validate the credential registry inside the caller's locked transaction."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS application_key_schema ("
        "id integer PRIMARY KEY CHECK(id=1), version integer NOT NULL)"
    )
    row = db.execute("SELECT version FROM application_key_schema WHERE id=1").fetchone()
    if row is None:
        db.execute(
            "CREATE TABLE application_keys ("
            "id text PRIMARY KEY, name text NOT NULL, key_hash text NOT NULL UNIQUE, "
            "created_at double precision NOT NULL, revoked_at double precision)"
        )
        db.execute("INSERT INTO application_key_schema VALUES(1,%s)", (REGISTRY_VERSION,))
    elif row["version"] != REGISTRY_VERSION:
        raise RuntimeError("Unsupported application key registry version")
    validate(db)


def validate(db: Connection) -> None:
    """Require the migrated registry without silently repairing missing credential data."""
    rows = db.execute("SELECT id,version FROM application_key_schema").fetchall()
    if len(rows) != 1 or rows[0] != {"id": 1, "version": REGISTRY_VERSION}:
        raise RuntimeError("Unsupported application key registry version")
    db.execute("SELECT id,name,key_hash,created_at,revoked_at FROM application_keys LIMIT 0")


class ApplicationKeys:
    """Issue, list and revoke application identities using uncached credential checks."""

    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    def list(self) -> list[dict[str, Any]]:
        """Return identities and lifecycle dates, never secrets or their hashes."""
        with self.store.connect() as db:
            return db.execute(
                "SELECT id,name,created_at,revoked_at FROM application_keys "
                "ORDER BY created_at DESC,id"
            ).fetchall()

    def issue(self, name: str) -> tuple[str, str]:
        """Create a named application and return its secret exactly once."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Use 1 to 64 letters, digits, underscores or hyphens")
        identifier, key = uuid4().hex, "fpapp_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO application_keys(id,name,key_hash,created_at) VALUES(%s,%s,%s,%s)",
                (identifier, name, digest, time.time()),
            )
        return identifier, key

    def revoke(self, identifier: str) -> None:
        """Revoke an existing registry key, preserving its audit identity."""
        with self.store.connect(write=True) as db:
            row = db.execute(
                "UPDATE application_keys SET revoked_at=coalesce(revoked_at,%s) "
                "WHERE id=%s RETURNING id",
                (time.time(), identifier),
            ).fetchone()
            if row is None:
                raise ValueError("Application key no longer exists; refresh the page")

    def authenticate(self, key: str) -> str | None:
        """Resolve an active key to its identity; revocation applies to the next check."""
        if not re.fullmatch(r"fpapp_[A-Za-z0-9_-]{43}", key):
            return None
        with self.store.connect() as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            row = db.execute(
                "SELECT id FROM application_keys WHERE key_hash=%s AND revoked_at IS NULL",
                (hashlib.sha256(key.encode()).hexdigest(),),
            ).fetchone()
        return "app-" + row["id"] if row else None
