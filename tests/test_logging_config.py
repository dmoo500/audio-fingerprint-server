"""Console logging uses a consistent level and Unix timestamp."""

import asyncio
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from fingerprint_server.access_log import AccessLogMiddleware


def test_console_format_has_unix_timestamp_and_level() -> None:
    config = json.loads(
        (Path(__file__).parents[1] / "fingerprint_server" / "logging.json").read_text()
    )
    formatter = logging.Formatter(config["formatters"]["standard"]["format"])
    for name in ("fingerprint_server.worker", "uvicorn.error", "uvicorn.access"):
        record = logging.LogRecord(name, logging.INFO, __file__, 1, "verified", (), None)
        record.created = 1_794_764_000.125
        assert formatter.format(record) == f"1794764000.125 INFO: [{name}] verified"
    assert config["root"]["level"] == "INFO"
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert config["loggers"][name]["propagate"]
        assert config["loggers"][name]["handlers"] == []
    assert config["loggers"]["uvicorn.access"]["level"] == "WARNING"


async def test_access_log_measures_complete_response(caplog: pytest.LogCaptureFixture) -> None:
    async def delayed(request):
        await asyncio.sleep(0.03)
        return Response(status_code=202)

    app = AccessLogMiddleware(Starlette(routes=[Route("/submission", delayed, methods=["POST"])]))
    with caplog.at_level(logging.INFO, logger="uvicorn.error.fingerprint.access"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            response = await client.post("http://test/submission?secret=hidden")
    assert response.status_code == 202
    message = caplog.records[-1].getMessage()
    assert '"POST /submission HTTP/1.1" 202' in message
    assert "secret" not in message and "hidden" not in message
    duration = re.search(r"duration_ms=([0-9.]+) complete=true", message)
    assert duration is not None
    assert float(duration[1]) >= 30


async def test_access_log_preserves_failure(caplog: pytest.LogCaptureFixture) -> None:
    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("Broken request")

    with caplog.at_level(logging.INFO, logger="uvicorn.error.fingerprint.access"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=AccessLogMiddleware(broken))
        ) as client:
            with pytest.raises(ValueError, match="Broken request"):
                await client.get("http://test/broken")
    assert '"GET /broken HTTP/1.1" 500 duration_ms=' in caplog.records[-1].getMessage()
    assert "complete=false" in caplog.records[-1].getMessage()
