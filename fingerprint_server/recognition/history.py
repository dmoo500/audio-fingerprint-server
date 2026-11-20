"""Separate SQLite storage for recognition accounts and their private history."""

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TypedDict

from aiohttp import web

SESSION_SECONDS = 7 * 86400
COOKIE = "fp_recognition"
APPLICATION_ID = 0x46505248


class Hit(TypedDict):
    title: str
    artists: list[str]
    recognized_at: int


class HistoryItem(Hit):
    id: int
    has_artwork: bool


class History:
    """Manage accounts, single-use invitations, sessions and recent matches."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def initialize(self) -> None:
        """Create or upgrade the history schema."""
        with self.connection(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            identity = db.execute("PRAGMA application_id").fetchone()[0]
            if identity == APPLICATION_ID and version in (1, 2):
                if version == 1:
                    db.execute("ALTER TABLE hits ADD COLUMN artwork_reference TEXT")
                    db.execute("PRAGMA user_version = 2")
                return
            if identity or version or db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                raise RuntimeError("Not a supported recognition history database")
            db.execute(
                "CREATE TABLE accounts (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
                "salt BLOB NOT NULL, password_hash BLOB NOT NULL)"
            )
            db.execute("CREATE TABLE invites (digest BLOB PRIMARY KEY, expires INTEGER NOT NULL)")
            db.execute(
                "CREATE TABLE sessions (digest BLOB PRIMARY KEY, expires INTEGER NOT NULL, "
                "account INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE)"
            )
            db.execute(
                "CREATE TABLE hits (id INTEGER PRIMARY KEY, "
                "account INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, "
                "at INTEGER NOT NULL, title TEXT NOT NULL, artists TEXT NOT NULL, "
                "artwork_reference TEXT)"
            )
            db.execute("CREATE INDEX hits_account ON hits(account, id DESC)")
            db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            db.execute("PRAGMA user_version = 2")

    def invite(self) -> str:
        """Issue a one-time invitation valid for seven days."""
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.connection(write=True) as db:
            db.execute("DELETE FROM invites WHERE expires <= ?", (now,))
            db.execute("INSERT INTO invites VALUES (?, ?)", (_digest(token), now + 7 * 86400))
        return token

    def register(self, username: str, password: str, invite: str) -> bool:
        """Atomically consume a valid invitation and create the account."""
        digest = _digest(invite)
        with self.connection() as db:
            if not db.execute(
                "SELECT 1 FROM invites WHERE digest = ? AND expires > ?", (digest, int(time.time()))
            ).fetchone():
                return False
        salt = secrets.token_bytes(16)
        password_hash = _hash(password, salt)
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM accounts WHERE username = ?", (username,)).fetchone():
                return False
            if not db.execute(
                "DELETE FROM invites WHERE digest = ? AND expires > ?", (digest, int(time.time()))
            ).rowcount:
                return False
            db.execute(
                "INSERT INTO accounts(username,salt,password_hash) VALUES (?,?,?)",
                (username, salt, password_hash),
            )
        return True

    def login(self, username: str, password: str, previous: str) -> str | None:
        """Verify credentials and replace the current session on success."""
        with self.connection() as db:
            account = db.execute(
                "SELECT id,salt,password_hash FROM accounts WHERE username = ?", (username,)
            ).fetchone()
        supplied = _hash(password, account[1] if account else bytes(16))
        if not hmac.compare_digest(supplied, account[2] if account else bytes(32)) or not account:
            return None
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.connection(write=True) as db:
            db.execute(
                "DELETE FROM sessions WHERE expires <= ? OR digest = ?", (now, _digest(previous))
            )
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?)",
                (_digest(token), now + SESSION_SECONDS, account[0]),
            )
        return token

    def logout(self, token: str) -> None:
        """Revoke one session."""
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE digest = ?", (_digest(token),))

    def username(self, token: str) -> str | None:
        """Resolve the current authenticated username."""
        with self.connection() as db:
            row = db.execute(
                "SELECT username FROM accounts JOIN sessions ON sessions.account = accounts.id "
                "WHERE digest = ? AND expires > ?",
                (_digest(token), int(time.time())),
            ).fetchone()
        return str(row[0]) if row else None

    def save(self, token: str, hit: Hit, artwork_reference: str | None = None) -> None:
        """Save a match only if the initiating session remains valid."""
        with self.connection(write=True) as db:
            cursor = db.execute(
                "INSERT INTO hits(account,at,title,artists,artwork_reference) "
                "SELECT account,?,?,?,? FROM sessions WHERE digest = ? AND expires > ?",
                (
                    hit["recognized_at"],
                    hit["title"],
                    json.dumps(hit["artists"]),
                    artwork_reference,
                    _digest(token),
                    int(time.time()),
                ),
            )
            if cursor.rowcount:
                db.execute(
                    "DELETE FROM hits WHERE account = "
                    "(SELECT account FROM sessions WHERE digest = ?) "
                    "AND id < (SELECT id FROM hits WHERE account = "
                    "(SELECT account FROM sessions WHERE digest = ?) "
                    "ORDER BY id DESC LIMIT 1 OFFSET 499)",
                    (_digest(token), _digest(token)),
                )

    def recent(self, token: str) -> list[HistoryItem]:
        """Return at most 500 private matches, newest first."""
        with self.connection() as db:
            return [
                {
                    "id": hit_id,
                    "title": title,
                    "artists": json.loads(artists),
                    "recognized_at": at,
                    "has_artwork": reference is not None,
                }
                for hit_id, title, artists, at, reference in db.execute(
                    "SELECT id,title,artists,at,artwork_reference FROM hits WHERE account = "
                    "(SELECT account FROM sessions WHERE digest = ? AND expires > ?) "
                    "ORDER BY id DESC LIMIT 500",
                    (_digest(token), int(time.time())),
                )
            ]

    def artwork_reference(self, token: str, hit_id: int) -> str | None:
        """Resolve artwork only for a hit owned by the active session."""
        with self.connection() as db:
            row = db.execute(
                "SELECT artwork_reference FROM hits JOIN sessions USING (account) "
                "WHERE hits.id = ? AND digest = ? AND expires > ?",
                (hit_id, _digest(token), int(time.time())),
            ).fetchone()
            return row[0] if row else None

    def clear(self, token: str) -> None:
        """Delete the history of the current account."""
        with self.connection(write=True) as db:
            db.execute(
                "DELETE FROM hits WHERE account = "
                "(SELECT account FROM sessions WHERE digest = ? AND expires > ?)",
                (_digest(token), int(time.time())),
            )


HISTORY_KEY = web.AppKey("recognition_history", History)


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=32768, r=8, p=3, dklen=32, maxmem=64 << 20
    )
