"""Bounded HTTP request handling shared by the API and public recognition app."""

import asyncio
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def trusted_image(url: str) -> bool:
    """Accept only credential-free HTTPS images hosted by the cover archive."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443)
        and (
            host == "archive.org" or host.endswith(".archive.org") or host == "coverartarchive.org"
        )
    )


async def bounded_body(request: Request, limit: int) -> bytes:
    """Enforce byte limits even for streamed requests without Content-Length."""
    body = bytearray()
    try:
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise HTTPException(413, "Request exceeds the size limit")
                body.extend(chunk)
    except TimeoutError as err:
        raise HTTPException(408, "Request body timed out") from err
    return bytes(body)
