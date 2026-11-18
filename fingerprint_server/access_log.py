"""HTTP access logging with end-to-end application duration."""

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOGGER = logging.getLogger("uvicorn.error.fingerprint.access")


class AccessLogMiddleware:
    """Log HTTP status and elapsed time without credentials or query strings."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        complete = False

        async def timed_send(message: Message) -> None:
            nonlocal status, complete
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, receive, timed_send)
        finally:
            LOGGER.info(
                '"%s %s HTTP/%s" %s duration_ms=%.2f complete=%s',
                scope["method"],
                scope["path"],
                scope["http_version"],
                status,
                (time.perf_counter() - started) * 1000,
                str(complete).lower(),
            )
