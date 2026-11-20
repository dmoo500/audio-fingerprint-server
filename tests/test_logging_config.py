"""Verify console formatting and aiohttp access logging."""

import asyncio
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from fingerprint_server.aiohttp_runner import AccessLogger


def test_console_format_has_unix_timestamp_and_level() -> None:
    config = json.loads(
        (Path(__file__).parents[1] / "fingerprint_server" / "logging.json").read_text()
    )
    formatter = logging.Formatter(config["formatters"]["standard"]["format"])
    for name in ("fingerprint_server.worker", "aiohttp.server", "fingerprint_server.access"):
        record = logging.LogRecord(name, logging.INFO, __file__, 1, "verified", (), None)
        record.created = 1_794_764_000.125
        assert formatter.format(record) == f"1794764000.125 INFO: [{name}] verified"
    assert config["root"]["level"] == "INFO"


@pytest.mark.parametrize("status", [202, 500])
async def test_access_log_measures_response(caplog: pytest.LogCaptureFixture, status: int) -> None:
    async def delayed(request: web.Request) -> web.Response:
        await asyncio.sleep(0.03)
        if status == 500:
            raise ValueError("Broken request")
        return web.Response(status=status)

    app = web.Application()
    app.router.add_post("/submission", delayed)
    logger = logging.getLogger("fingerprint_server.access")
    server = TestServer(app)
    with caplog.at_level(logging.INFO, logger=logger.name):
        try:
            await server.start_server(access_log=logger, access_log_class=AccessLogger)
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(str(server.make_url("/submission?test=hidden")))
        finally:
            await server.close()
    assert response.status_code == status
    (message,) = [record.getMessage() for record in caplog.records if record.name == logger.name]
    assert f'"POST /submission HTTP/1.1" {status}' in message
    assert "hidden" not in message
    duration = re.search(r"duration_ms=([0-9.]+)", message)
    assert duration is not None
    assert float(duration[1]) >= 30
