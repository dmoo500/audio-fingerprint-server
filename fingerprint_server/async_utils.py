"""Cancellation-safe bounded synchronous work."""

import asyncio
from collections.abc import Callable


async def run_sync[**P, R](function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Wait for synchronous work to stop before releasing its caller's resources."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise
