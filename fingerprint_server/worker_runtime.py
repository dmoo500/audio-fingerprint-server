"""Run the single PostgreSQL ingestion worker without an HTTP listener."""

import asyncio
import json
import logging.config
from pathlib import Path

from fingerprint_server.runtime import runtime
from fingerprint_server.settings import Settings


async def main() -> None:
    """Run until cancellation or a worker/ownership failure."""
    settings = Settings()  # type: ignore[call-arg]
    if settings.database_backend != "postgres" or settings.service_role != "worker":
        raise ValueError("Standalone worker requires PostgreSQL and FP_SERVICE_ROLE=worker")
    async with runtime(settings) as (_, _, worker_task, monitor):
        assert worker_task is not None and monitor is not None
        done, _ = await asyncio.wait((worker_task, monitor), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise RuntimeError("Ingestion runtime unexpectedly stopped")


if __name__ == "__main__":
    logging.config.dictConfig(json.loads(Path(__file__).with_name("logging.json").read_text()))
    asyncio.run(main())
