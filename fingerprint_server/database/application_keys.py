"""Independently versioned application credentials; never stores raw keys."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from fingerprint_server.database import Storage
    from fingerprint_server.database.postgres import Connection

REGISTRY_VERSION = 1


def initialize(db: Connection | sqlite3.Connection) -> None:
    """Create or validate the credential registry inside the caller's locked transaction."""
    if isinstance(db, sqlite3.Connection):
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_key_schema'"
        ).fetchone()
    else:
        exists = db.execute(
            "SELECT 1 WHERE to_regclass('application_key_schema') IS NOT NULL"
        ).fetchone()
    if exists is None:
        db.execute(
            "CREATE TABLE application_key_schema ("
            "id integer PRIMARY KEY CHECK(id=1), version integer NOT NULL)"
        )
        db.execute(
            "CREATE TABLE application_keys ("
            "id text PRIMARY KEY, name text NOT NULL, key_hash text NOT NULL UNIQUE, "
            "created_at double precision NOT NULL, revoked_at double precision)"
        )
        marker = "?" if isinstance(db, sqlite3.Connection) else "%s"
        db.execute(f"INSERT INTO application_key_schema VALUES(1,{marker})", (REGISTRY_VERSION,))
    validate(db)


def validate(db: Connection | sqlite3.Connection) -> None:
    """Require the migrated registry without silently repairing missing credential data."""
    rows = db.execute("SELECT id,version FROM application_key_schema").fetchall()
    if len(rows) != 1 or rows[0]["id"] != 1 or rows[0]["version"] != REGISTRY_VERSION:
        raise RuntimeError("Unsupported application key registry version")
    db.execute("SELECT id,name,key_hash,created_at,revoked_at FROM application_keys LIMIT 0")


class ApplicationKeys:
    """Issue, list and revoke application identities using uncached credential checks."""

    def __init__(self, store: Storage) -> None:
        self.store = store

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[Connection | sqlite3.Connection]:
        """Use the backend's transaction and serialization rules."""
        from fingerprint_server.database.sqlite import Store

        if isinstance(self.store, Store):
            with self.store.connect() as db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        else:
            with self.store.connect(write=write) as db:
                yield db

    def sql(self, statement: str) -> str:
        """Use the parameter style of the selected backend."""
        from fingerprint_server.database.sqlite import Store

        return statement if isinstance(self.store, Store) else statement.replace("?", "%s")

    def list(self) -> list[dict[str, Any]]:
        """Return identities and lifecycle dates, never secrets or their hashes."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT id,name,created_at,revoked_at FROM application_keys "
                "ORDER BY created_at DESC,id"
            ).fetchall()
            return [dict(row) for row in rows]

    def issue(self, name: str) -> tuple[str, str]:
        """Create a named application and return its secret exactly once."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Use 1 to 64 letters, digits, underscores or hyphens")
        identifier, key = uuid4().hex, "fpapp_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self.connection(write=True) as db:
            db.execute(
                self.sql(
                    "INSERT INTO application_keys(id,name,key_hash,created_at) VALUES(?,?,?,?)"
                ),
                (identifier, name, digest, time.time()),
            )
        return identifier, key

    def revoke(self, identifier: str) -> None:
        """Revoke an existing registry key, preserving its audit identity."""
        with self.connection(write=True) as db:
            row = db.execute(
                self.sql(
                    "UPDATE application_keys SET revoked_at=coalesce(revoked_at,?) "
                    "WHERE id=? RETURNING id"
                ),
                (time.time(), identifier),
            ).fetchone()
            if row is None:
                raise ValueError("Application key no longer exists; refresh the page")

    def authenticate(self, key: str) -> str | None:
        """Resolve an active key to its identity; revocation applies to the next check."""
        if not re.fullmatch(r"fpapp_[A-Za-z0-9_-]{43}", key):
            return None
        with self.connection() as db:
            if not isinstance(db, sqlite3.Connection):
                db.execute("SET LOCAL statement_timeout='5s'")
            row = db.execute(
                self.sql("SELECT id FROM application_keys WHERE key_hash=? AND revoked_at IS NULL"),
                (hashlib.sha256(key.encode()).hexdigest(),),
            ).fetchone()
        return "app-" + row["id"] if row else None
