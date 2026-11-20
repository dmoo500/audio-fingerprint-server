"""HTTP responses and bounded request bodies for aiohttp services."""

import asyncio
from collections.abc import Mapping
from ipaddress import ip_address

from aiohttp import web
from aiohttp.typedefs import Handler, Middleware


class HTTPError(Exception):
    """Return a structured API error with the given HTTP status."""

    def __init__(
        self, status_code: int, detail: object, headers: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


@web.middleware
async def error_responses(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Keep the JSON error format for application and router errors."""
    try:
        return await handler(request)
    except HTTPError as err:
        return web.json_response(
            {"detail": err.detail}, status=err.status_code, headers=err.headers
        )
    except web.HTTPException as err:
        if err.status < 400:
            raise
        headers = err.headers.copy()
        headers.pop("Content-Type", None)
        headers.pop("Content-Length", None)
        return web.json_response({"detail": err.reason}, status=err.status, headers=headers)


async def bounded_body(request: web.Request, limit: int) -> bytes:
    """Read up to the byte limit within the request-body deadline."""
    body = bytearray()
    try:
        async with asyncio.timeout(30):
            async for chunk in request.content.iter_chunked(64 * 1024):
                if len(body) + len(chunk) > limit:
                    raise HTTPError(413, "Request exceeds the size limit")
                body.extend(chunk)
    except TimeoutError as err:
        raise HTTPError(408, "Request body timed out") from err
    return bytes(body)


def trusted_proxy(address: str) -> Middleware:
    """Accept forwarding information only from one configured proxy address."""
    proxy_ip = str(ip_address(address))

    @web.middleware
    async def proxy_headers(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.remote != proxy_ip:
            return await handler(request)

        schemes = request.headers.getall("X-Forwarded-Proto", [])
        addresses = request.headers.getall("X-Forwarded-For", [])
        scheme = request.scheme
        remote = proxy_ip
        if len(schemes) == 1 and schemes[0] in ("http", "https"):
            scheme = schemes[0]
        if len(addresses) == 1 and "," not in addresses[0] and "%" not in addresses[0]:
            try:
                remote = str(ip_address(addresses[0].strip()))
            except ValueError as err:
                raise HTTPError(400, "Invalid forwarded client address") from err
        return await handler(request.clone(scheme=scheme, remote=remote))

    return proxy_headers
