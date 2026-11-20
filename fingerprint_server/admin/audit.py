"""Audit administrator actions without recording request or response payloads."""

import logging

from aiohttp import web
from aiohttp.typedefs import Handler

LOGGER = logging.getLogger(__name__)
AUTHENTICATED = web.RequestKey("admin_audit_authenticated", bool)
ACTION = web.RequestKey("admin_audit_action", str)


@web.middleware
async def audit_admin(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Record the outcome of every routed Admin write, including denied requests."""
    event = request.match_info.route.name
    if request.method != "POST" or not event or not event.startswith("audit-"):
        return await handler(request)

    status = 500
    try:
        response = await handler(request)
        status = response.status
        return response
    finally:
        success = 200 <= status < 300
        authenticated = request.get(AUTHENTICATED) or (event == "audit-login" and success)
        outcome = "success" if success else ("failed" if status >= 500 else "rejected")
        LOGGER.log(
            logging.INFO if success else logging.WARNING,
            "event=%s action=%s actor=%s outcome=%s status=%d",
            event.removeprefix("audit-"),
            request.get(ACTION, "-"),
            "admin" if authenticated else "-",
            outcome,
            status,
        )
