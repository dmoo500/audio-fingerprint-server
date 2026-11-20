"""Bounded HTTP transport to the fingerprint API."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from fingerprint_server.aiohttp_support import HTTPError
from fingerprint_server.recognition.settings import RecognitionSettings

LOGGER = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 64_000


@asynccontextmanager
async def open_client(
    settings: RecognitionSettings, transport: httpx.AsyncBaseTransport | None
) -> AsyncIterator[httpx.AsyncClient]:
    """Open the server-owned upstream client for one service lifespan."""
    headers = {"X-API-Key": settings.read_key.get_secret_value()}
    if settings.application_key is not None:
        headers["X-Application-Key"] = settings.application_key.get_secret_value()
    async with httpx.AsyncClient(
        base_url=str(settings.api_url),
        headers=headers,
        timeout=httpx.Timeout(25, connect=5),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        yield client


async def upstream(
    client: httpx.AsyncClient, method: str, path: str, *, content: bytes | None = None
) -> bytes:
    """Return a bounded upstream response or a redacted HTTP error."""
    try:
        async with client.stream(
            method,
            path,
            content=content,
            headers={"Content-Type": "application/octet-stream"} if content is not None else {},
        ) as response:
            if response.status_code != 200:
                LOGGER.warning("Recognition upstream returned HTTP %s", response.status_code)
                if response.status_code == 422:
                    raise HTTPError(422, "The recording could not be processed. Try again.")
                if response.status_code == 429:
                    raise HTTPError(
                        429,
                        "Recognition is busy. Please try again in a minute.",
                        headers={"Retry-After": "60"},
                    )
                raise HTTPError(503, "Recognition is temporarily unavailable.")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                    LOGGER.error("Recognition upstream response exceeds the size limit")
                    raise HTTPError(502, "Recognition returned an invalid response.")
                data.extend(chunk)
            return bytes(data)
    except httpx.RequestError as err:
        LOGGER.error("Recognition upstream request failed: %s", type(err).__name__)
        raise HTTPError(503, "Recognition is temporarily unavailable.") from err
