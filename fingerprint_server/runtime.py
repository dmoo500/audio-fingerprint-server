"""Process ownership and fail-closed monitoring for the ingestion runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager

import httpx

from fingerprint_server.artwork import ArtworkStore
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database import Storage, create_store
from fingerprint_server.database.metrics import sample_history
from fingerprint_server.settings import Settings
from fingerprint_server.worker import Worker


@asynccontextmanager
async def runtime(
    settings: Settings,
) -> AsyncIterator[
    tuple[Storage, Worker | None, asyncio.Task[None] | None, asyncio.Task[None] | None]
]:
    """Start selected roles and retain ownership until their blocking work has stopped."""
    store = create_store(settings)
    await run_sync(store.initialize)
    await run_sync(ArtworkStore(store).initialize)
    owners = ExitStack()
    worker_task: asyncio.Task[None] | None = None
    history_task: asyncio.Task[None] | None = None
    monitor: asyncio.Task[None] | None = None
    worker: Worker | None = None
    try:
        if settings.database_backend == "postgres":
            from fingerprint_server.database.postgres import PostgresStore

            assert isinstance(store, PostgresStore)
            postgres_store = store
            connections = []
            roles = (
                ("api", "worker") if settings.service_role == "all" else (settings.service_role,)
            )
            for role in roles:
                connection = await run_sync(owners.enter_context, store.owner(role))
                connections.append((role, connection))

            async def monitor_owners() -> None:
                while True:
                    await asyncio.sleep(5)
                    if worker_task is not None and worker_task.done():
                        raise RuntimeError("Ingestion worker stopped; stopping ownership heartbeat")
                    for role, connection in connections:
                        await run_sync(postgres_store.heartbeat, connection, role)

            monitor = asyncio.create_task(monitor_owners(), name="fingerprint-ownership")
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            if settings.service_role != "api":
                worker = Worker(store, settings, client)
                worker_task = asyncio.create_task(worker.run(), name="fingerprint-ingestion")
                history_task = asyncio.create_task(
                    sample_history(store), name="fingerprint-admin-history"
                )
            if monitor is not None and worker_task is not None:
                running_worker = worker_task

                def stop_worker_on_owner_loss(task: asyncio.Task[None]) -> None:
                    if task.cancelled() or task.exception() is not None:
                        running_worker.cancel()

                monitor.add_done_callback(stop_worker_on_owner_loss)
            try:
                yield store, worker, worker_task, monitor
            finally:
                try:
                    await stop_task(worker_task)
                finally:
                    try:
                        await stop_task(history_task)
                    finally:
                        await stop_task(monitor)
    finally:
        await run_sync(owners.close)


async def stop_task(task: asyncio.Task[None] | None) -> None:
    """Cancel and join a task, preserving any unexpected failure."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
