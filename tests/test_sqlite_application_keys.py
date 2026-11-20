"""Verify SQLite application credentials survive restart and reject revoked keys."""

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest
from aiohttp_client import AsyncClient
from pydantic import SecretStr
from test_server import READ, settings

from fingerprint_server.api_state import STORE
from fingerprint_server.app import create_app
from fingerprint_server.database import create_store
from fingerprint_server.database.application_keys import ApplicationKeys, initialize, validate
from fingerprint_server.database.sqlite import Store
from fingerprint_server.worker import Worker


def test_registry_initialization_issue_and_revocation(tmp_path: Path) -> None:
    store = Store(tmp_path / "fingerprints.db")
    store.initialize()
    keys = ApplicationKeys(store)
    with keys.connection(write=True) as db:
        initialize(db)
        initialize(db)
    identity, secret = keys.issue("test")
    assert keys.authenticate(secret) == "app-" + identity
    with store.connect() as db:
        validate(db)
        row = db.execute("SELECT key_hash FROM application_keys").fetchone()
        assert row["key_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert "key_hash" not in keys.list()[0]
    store.initialize()
    assert keys.authenticate(secret) == "app-" + identity
    keys.revoke(identity)
    assert keys.authenticate(secret) is None


def test_partial_registry_is_never_recreated(tmp_path: Path) -> None:
    path = tmp_path / "fingerprints.db"
    store = Store(path)
    store.initialize()
    with store.connect() as db:
        db.execute("CREATE TABLE application_keys (id TEXT)")
    with pytest.raises(RuntimeError, match="Incomplete application key registry"):
        store.initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='application_keys'").fetchone()


def test_empty_registry_version_is_not_repaired(tmp_path: Path) -> None:
    store = Store(tmp_path / "fingerprints.db")
    store.initialize()
    keys = ApplicationKeys(store)
    with keys.connection(write=True) as db:
        initialize(db)
        db.execute("DELETE FROM application_key_schema")
    with pytest.raises(RuntimeError, match="Unsupported"):
        with keys.connection(write=True) as db:
            initialize(db)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM application_key_schema").fetchone()[0] == 0


async def test_gateway_authentication_and_revocation(tmp_path: Path, monkeypatch) -> None:
    config = settings(tmp_path, gateway_enabled=True, application_key=SecretStr("l" * 40))
    app = create_app(config)
    async with AsyncClient(app) as client:
        keys = ApplicationKeys(create_store(config))
        identifier, secret = keys.issue("sqlite-client")
        headers = {"X-Application-Key": secret, "X-API-Key": READ}
        response = await client.get("/api/v1/dejavu/statistics", headers=headers)
        assert response.status_code == 200

        def unavailable(self, supplied):
            raise sqlite3.OperationalError("temporary registry outage")

        with monkeypatch.context() as patch:
            patch.setattr(ApplicationKeys, "authenticate", unavailable)
            assert (
                await client.get("/api/v1/dejavu/statistics", headers=headers)
            ).status_code == 503
        keys.revoke(identifier)
        response = await client.get("/api/v1/dejavu/statistics", headers=headers)
        assert response.status_code == 401


async def test_health_reports_sqlite_database_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep the worker alive without letting it race the health probe for the failing connection.
    async def idle_worker(self: Worker) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Worker, "run", idle_worker)
    app = create_app(settings(tmp_path))
    async with AsyncClient(app) as client:
        assert (await client.get("/health")).status_code == 200
        with monkeypatch.context() as patch:

            def unavailable():
                raise sqlite3.OperationalError("database unavailable")

            patch.setattr(app[STORE], "connect", unavailable)
            response = await client.get("/health")
            assert response.status_code == 503
            assert response.json() == {"status": "failed"}
        assert (await client.get("/health")).status_code == 200
