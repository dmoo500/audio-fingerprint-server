"""Use httpx assertions and request streaming against a real aiohttp listener."""

from contextlib import AbstractContextManager, ExitStack
from functools import partial
from types import TracebackType
from typing import Any

import httpx
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiohttp.typedefs import Handler
from anyio.from_thread import start_blocking_portal


class _LoopbackTransport(httpx.AsyncHTTPTransport):
    def __init__(self, server: TestServer) -> None:
        super().__init__()
        self.server = server

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        listener = self.server.make_url("/")
        headers = request.headers.copy()
        headers["X-Test-Scheme"] = request.url.scheme
        loopback = httpx.Request(
            request.method,
            request.url.copy_with(scheme="http", host=listener.host, port=listener.port),
            headers=headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        return await super().handle_async_request(loopback)


class AsyncClient(httpx.AsyncClient):
    """Run an aiohttp application on a disposable loopback port."""

    def __init__(
        self,
        app: web.Application,
        *,
        base_url: str = "http://test",
        follow_redirects: bool = True,
    ) -> None:
        app.middlewares.insert(0, _test_origin)
        self.server = TestServer(app)
        super().__init__(
            base_url=base_url,
            follow_redirects=follow_redirects,
            transport=_LoopbackTransport(self.server),
            trust_env=False,
        )

    async def __aenter__(self) -> "AsyncClient":
        try:
            await self.server.start_server()
        except (RuntimeError, ValueError, OSError):
            await self.server.close()
            raise
        await super().__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        try:
            await super().__aexit__(exc_type, exc_value, traceback)
        finally:
            await self.server.close()


class TestClient(AbstractContextManager["TestClient"]):
    """Use the asynchronous loopback client from synchronous tests."""

    __test__ = False

    def __init__(self, app: web.Application, **kwargs: Any) -> None:
        self.client = AsyncClient(app, **kwargs)
        self.stack = ExitStack()

    def __enter__(self) -> "TestClient":
        with ExitStack() as stack:
            self.portal = stack.enter_context(start_blocking_portal())
            stack.enter_context(self.portal.wrap_async_context_manager(self.client))
            self.stack = stack.pop_all()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stack.__exit__(*exc)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.portal.call(partial(self.client.get, url, **kwargs))

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.portal.call(partial(self.client.post, url, **kwargs))


@web.middleware
async def _test_origin(request: web.Request, handler: Handler) -> web.StreamResponse:
    return await handler(request.clone(scheme=request.headers["X-Test-Scheme"]))
