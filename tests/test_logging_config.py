"""Console logging uses a consistent level and Unix timestamp."""

import json
import logging
from pathlib import Path


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
