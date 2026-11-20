"""Real PostgreSQL registry migration, cross-replica authentication and revocation."""

import hashlib
import importlib
import json
from uuid import uuid4

import pytest
from aiohttp_client import AsyncClient
from pydantic import SecretStr
from test_postgres_migration import postgres as postgres
from test_server import READ, settings

from fingerprint_server.app import create_app

psycopg = pytest.importorskip("psycopg")
ApplicationKeys = importlib.import_module(
    "fingerprint_server.database.application_keys"
).ApplicationKeys
PostgresStore = importlib.import_module("fingerprint_server.database.postgres").PostgresStore

LEGACY = "legacy-app-" + "c" * 32


async def test_registry_migration_two_replicas_revocation_and_legacy(
    postgres, tmp_path, caplog, monkeypatch
):
    config = settings(
        tmp_path,
        database_backend="postgres",
        postgres_schema="apps_" + uuid4().hex,
        service_role="api",
        api_replicas_enabled=True,
        gateway_enabled=True,
        application_key=SecretStr(LEGACY),
    )
    store = PostgresStore(config)
    # Upgrade an existing fingerprint database rather than only testing empty creation.
    old = PostgresStore(
        config.model_copy(update={"gateway_enabled": False, "application_key": None})
    )
    old.initialize()
    store.initialize()
    keys = ApplicationKeys(store)
    identifier, secret = keys.issue("test-client")
    store.initialize()
    assert keys.authenticate(secret) == "app-" + identifier
    with store.connect() as db:
        row = dict(db.execute("SELECT * FROM application_keys").fetchone())
        assert secret not in json.dumps(row)
        assert row["key_hash"] == hashlib.sha256(secret.encode()).hexdigest()
        assert (
            db.execute("SELECT format_version FROM migration_info").fetchone()["format_version"]
            == 1
        )
    assert keys.list()[0]["id"] == identifier
    assert "key_hash" not in keys.list()[0]
    with pytest.raises(ValueError):
        keys.issue("invalid name")
    first, second = create_app(config), create_app(config)
    async with AsyncClient(first) as a, AsyncClient(second) as b:
        caplog.set_level("INFO", logger="uvicorn.error.fingerprint.auth")
        headers = {"X-Application-Key": secret, "X-API-Key": READ}
        for client in (a, b):
            assert (
                await client.get("/api/v1/dejavu/statistics", headers=headers)
            ).status_code == 200

        def unavailable(self, supplied):
            raise psycopg.OperationalError("temporary registry outage")

        with monkeypatch.context() as patch:
            patch.setattr(ApplicationKeys, "authenticate", unavailable)
            assert (await a.get("/api/v1/dejavu/statistics", headers=headers)).status_code == 503
        assert "application=app-" + identifier in caplog.text
        assert secret not in caplog.text
        assert (
            await a.get("/api/v1/dejavu/statistics", headers={"X-Application-Key": secret})
        ).status_code == 401
        keys.revoke(identifier)
        keys.revoke(identifier)
        for client in (a, b):
            assert (
                await client.get("/api/v1/dejavu/statistics", headers=headers)
            ).status_code == 401
            assert (
                await client.get(
                    "/api/v1/dejavu/statistics",
                    headers={
                        "X-Application-Key": LEGACY,
                        "X-API-Key": READ,
                    },
                )
            ).status_code == 200
    assert keys.list()[0]["revoked_at"] is not None
    assert keys.authenticate("fpapp_" + "X" * 43) is None


def test_registry_rejects_unknown_version_without_overwriting_credentials(postgres, tmp_path):
    config = settings(
        tmp_path,
        database_backend="postgres",
        postgres_schema="apps_" + uuid4().hex,
        gateway_enabled=True,
        application_key=SecretStr(LEGACY),
    )
    store = PostgresStore(config)
    store.initialize()
    keys = ApplicationKeys(store)
    _, secret = keys.issue("keep-me")
    with store.connect(write=True) as db:
        db.execute("UPDATE application_key_schema SET version=999")
    with pytest.raises(RuntimeError, match="Unsupported"):
        store.initialize()
    assert keys.authenticate(secret)
