"""Verify shared PostgreSQL budgets and opt-in multi-API ownership."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from test_postgres_store import configuration as configuration
from test_postgres_store import postgres as postgres
from test_postgres_store import store as store
from test_server import READ, settings

from fingerprint_server.app import create_app
from fingerprint_server.async_utils import run_sync
from fingerprint_server.runtime import runtime


@pytest.mark.parametrize(
    "options",
    [
        {"api_replicas_enabled": True},
        {"database_backend": "postgres", "api_replicas_enabled": True},
        {"database_backend": "postgres", "service_role": "worker", "api_replicas_enabled": True},
        {"gateway_backends": []},
        {"gateway_backends": ["http://host:80"]},
        {"gateway_backends": ["host:0"]},
        {"gateway_backends": ["host:8097", "host:8097"]},
    ],
)
def test_invalid_replica_configuration(tmp_path: Path, options: dict) -> None:
    with pytest.raises(ValidationError):
        settings(tmp_path, **options)


def test_shared_rate_limit_serializes_competing_instances(store) -> None:
    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = list(executor.map(lambda _: store.allow_request("read", 30), range(48)))
    assert sum(accepted) == 30
    assert not store.allow_request("read", 30)
    assert store.allow_request("upload", 60)
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE service_state SET value=%s WHERE key='api_rate:read'",
            (json.dumps([time.time() - 61] * 30),),
        )
    assert store.allow_request("read", 30)


def test_shared_slots_release_on_exception(store, configuration) -> None:
    from fingerprint_server.database.postgres import PostgresStore

    other = PostgresStore(configuration)
    with store.recognition_slot() as first, other.recognition_slot() as second:
        assert first and second
        with other.recognition_slot() as third:
            assert not third
    with pytest.raises(ValueError, match="test"), store.recognition_slot():
        raise ValueError("test")
    with store.recognition_slot() as first, other.recognition_slot() as second:
        assert first and second


def test_lost_slot_connection_never_returns_success(store) -> None:
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.OperationalError):
        with store.recognition_slot() as acquired:
            assert acquired
            with store.connect() as db:
                db.execute(
                    "SELECT pg_terminate_backend(l.pid) FROM pg_locks l "
                    "JOIN pg_stat_activity a ON a.pid=l.pid WHERE l.locktype='advisory' "
                    "AND l.granted AND l.pid!=pg_backend_pid() AND a.application_name=%s",
                    (f"fingerprint:{store.schema}",),
                )
    with store.recognition_slot() as acquired:
        assert acquired


async def test_two_apis_share_rate_budget_and_recognition_slots(configuration) -> None:
    from fingerprint_server.database.postgres import PostgresStore

    replica = configuration.model_copy(update={"service_role": "api", "api_replicas_enabled": True})
    first, second = create_app(replica), create_app(replica)
    async with runtime(configuration.model_copy(update={"service_role": "worker"})):
        async with first.router.lifespan_context(first), second.router.lifespan_context(second):
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=first), base_url="http://one"
                ) as one,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=second), base_url="http://two"
                ) as two,
            ):
                for index in range(31):
                    response = await (one if index % 2 else two).get(
                        "/api/v1/dejavu/statistics", headers={"X-API-Key": READ}
                    )
                    assert response.status_code == (200 if index < 30 else 429)
                store = PostgresStore(configuration)
                holders = ExitStack()
                try:
                    assert await run_sync(holders.enter_context, store.recognition_slot())
                    assert await run_sync(holders.enter_context, store.recognition_slot())
                    for client in (one, two):
                        response = await client.post(
                            "/api/v1/dejavu/recognize?sample_rate=44100",
                            content=b"",
                            headers={"X-API-Key": READ, "Content-Type": "application/octet-stream"},
                        )
                        assert response.status_code == 429
                        assert response.headers["Retry-After"] == "2"
                finally:
                    await run_sync(holders.close)
                response = await two.post(
                    "/api/v1/dejavu/recognize?sample_rate=44100",
                    content=b"",
                    headers={"X-API-Key": READ, "Content-Type": "application/octet-stream"},
                )
                assert response.status_code == 422  # Slot available; input now gets validated.
                assert first.state.worker_task is None and second.state.worker_task is None


async def test_multiple_api_owners_exclude_non_replica_process(store, configuration) -> None:
    from fingerprint_server.database.postgres import PostgresStore

    replica = PostgresStore(
        configuration.model_copy(update={"api_replicas_enabled": True, "service_role": "api"})
    )
    other = PostgresStore(replica.settings)
    with replica.owner("api"), other.owner("api"):
        with pytest.raises(RuntimeError, match="already active"), store.owner("api"):
            pass
        with replica.owner("worker"):
            with pytest.raises(RuntimeError, match="already active"), other.owner("worker"):
                pass
    with store.owner("api"):
        with pytest.raises(RuntimeError, match="already active"), replica.owner("api"):
            pass
