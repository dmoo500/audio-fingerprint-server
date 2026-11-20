"""Session and database dependencies for the private admin API."""

from aiohttp import web

from fingerprint_server.admin.state import AUTH_KEY, DATA_KEY, Data
from fingerprint_server.aiohttp_support import HTTPError


def data_for(request: web.Request) -> Data:
    """Return the data adapter configured for the current admin service."""
    return request.app[DATA_KEY]


async def require_session(request: web.Request) -> None:
    """Reject requests without an active administrator session."""
    if not await request.app[AUTH_KEY].is_authenticated(request):
        raise HTTPError(401, "Administrator session required")
