"""Run an aiohttp service with the fingerprint server's logging configuration."""

import argparse
import json
import logging.config
from importlib import import_module
from ipaddress import ip_address
from pathlib import Path

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from aiohttp.web_request import BaseRequest

from fingerprint_server.aiohttp_support import trusted_proxy


class AccessLogger(AbstractAccessLogger):
    """Log the request path and timing without credentials or query strings."""

    def log(self, request: BaseRequest, response: web.StreamResponse, time: float) -> None:
        self.logger.info(
            '"%s %s HTTP/%s.%s" %s duration_ms=%.2f',
            request.method,
            request.path,
            request.version.major,
            request.version.minor,
            response.status,
            time * 1000,
        )


def main() -> None:
    """Start one aiohttp application on the requested address."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", help="Module exporting an aiohttp application named app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--trusted-proxy", type=ip_address, help="Exact peer IP of the reverse proxy"
    )
    args = parser.parse_args()
    logging.config.dictConfig(json.loads(Path(__file__).with_name("logging.json").read_text()))
    app: web.Application = import_module(args.module).app
    if args.trusted_proxy is not None:
        app.middlewares.append(trusted_proxy(str(args.trusted_proxy)))
    web.run_app(
        app,
        host=args.host,
        port=args.port,
        access_log=logging.getLogger("fingerprint_server.access"),
        access_log_class=AccessLogger,
        print=None,
    )


if __name__ == "__main__":
    main()
