"""Administrative provisioning for the optional APISIX deployment."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from typing import Any

import httpx
from pydantic import ValidationError

from fingerprint_server.settings import Settings

ROLES = ("read", "upload", "both")

# Authentication exits during rewrite, before ordinary access-phase rate limiters run.
PREAUTH_LIMIT = """
local limit_req = require("resty.limit.req")
return function(_, ctx)
    local limiter, err = limit_req.new("plugin-limit-req", 5, 10)
    if not limiter then
        ngx.log(ngx.ERR, "fingerprint admission initialization failed: ", err)
        return 503
    end
    local delay, err = limiter:incoming("fingerprint-admission:" .. ctx.var.remote_addr, true)
    if not delay then
        if err == "rejected" then
            return 429
        end
        ngx.log(ngx.ERR, "fingerprint admission check failed: ", err)
        return 503
    end
end
"""


def configure(client: httpx.Client, settings: Settings) -> None:
    """Install gateway routes without changing existing personal credentials."""
    if not settings.gateway_enabled:
        raise ValueError("Enable FP_GATEWAY_ENABLED=true before configuring the gateway")
    client.put(
        "global_rules/fingerprint-admission",
        json={
            "plugins": {
                "serverless-pre-function": {
                    "phase": "rewrite",
                    "functions": [PREAUTH_LIMIT],
                },
                "limit-conn": {
                    "conn": 8,
                    "burst": 0,
                    "default_conn_delay": 0.1,
                    "key": "remote_addr",
                    "policy": "local",
                    "rejected_code": 429,
                    "allow_degradation": False,
                },
            }
        },
    ).raise_for_status()
    for role in ROLES:
        client.put(
            f"consumer_groups/{role}", json={"desc": f"Fingerprint {role} access", "plugins": {}}
        ).raise_for_status()
    client.put(
        "upstreams/fingerprints",
        json={
            "type": "roundrobin",
            "nodes": dict.fromkeys(settings.gateway_backends, 1),
            "timeout": {"connect": 5, "send": 30, "read": 60},
            "retries": 0,
            "checks": {
                "active": {
                    "type": "http",
                    "http_path": "/health",
                    "timeout": 2,
                    "healthy": {"interval": 2, "successes": 2, "http_statuses": [200]},
                    "unhealthy": {
                        "interval": 1,
                        "http_failures": 2,
                        "tcp_failures": 2,
                        "timeouts": 2,
                        "http_statuses": [500, 502, 503, 504],
                    },
                },
            },
        },
    ).raise_for_status()
    routes = [
        ("upload", "/api/v1/dejavu/submissions", "POST", "upload", 60),
        ("status", "/api/v1/dejavu/submissions/*", "GET", "upload", 60),
        ("capabilities", "/api/v1/dejavu/capabilities", "GET", "upload", 60),
        ("availability", "/api/v1/dejavu/availability", "POST", "upload", 60),
        ("recognize", "/api/v1/dejavu/recognize", "POST", "read", 30),
        ("recognize-landmarks", "/api/v1/dejavu/recognize/landmarks", "POST", "read", 30),
        ("statistics", "/api/v1/dejavu/statistics", "GET", "read", 30),
        ("artwork", "/api/v1/dejavu/artwork/*", "GET", "read", 60),
    ]
    for name, uri, method, scope, count in routes:
        internal = settings.upload_key if scope == "upload" else settings.read_key
        document: dict[str, Any] = {
            "uri": uri,
            "methods": [method],
            "upstream_id": "fingerprints",
            "plugins": {
                "key-auth": {"header": "X-API-Key", "query": "", "hide_credentials": True},
                "consumer-restriction": {
                    "type": "consumer_group_id",
                    "whitelist": [scope, "both"],
                },
                "limit-count": {
                    "count": count,
                    "time_window": 60,
                    "key_type": "var",
                    "key": "consumer_name",
                    "policy": "local",
                    "rejected_code": 429,
                    "allow_degradation": False,
                },
                "proxy-rewrite": {
                    "headers": {
                        "remove": ["X-Credential-Identifier"],
                        "set": {"X-API-Key": internal.get_secret_value()},
                    }
                },
            },
        }
        client.put(f"routes/{name}", json=document).raise_for_status()
    client.put(
        "routes/public",
        json={
            "uris": ["/health"],
            "methods": ["GET"],
            "upstream_id": "fingerprints",
            "plugins": {
                "proxy-rewrite": {
                    "headers": {
                        "remove": [
                            "X-API-Key",
                            "X-Application-Key",
                            "X-Consumer-Username",
                            "X-Credential-Identifier",
                        ]
                    }
                }
            },
        },
    ).raise_for_status()


def provision(client: httpx.Client, username: str, role: str, key: str) -> None:
    """Create a personal credential or explicitly replace it for rotation."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username):
        raise ValueError("Consumer name must be 1 to 64 letters, digits, underscores or hyphens")
    if role not in ROLES:
        raise ValueError("Role must be read, upload or both")
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", key):
        raise ValueError("Personal key must contain at least 24 URL-safe characters")
    client.put(
        f"consumers/{username}",
        json={"username": username, "group_id": role, "plugins": {"key-auth": {"key": key}}},
    ).raise_for_status()


def main() -> None:
    """Configure routes, issue personal keys or revoke a consumer through the local Admin API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-port", type=int, default=9180, help="Loopback Admin API port")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure")
    create = commands.add_parser("issue")
    create.add_argument("username")
    create.add_argument("--role", choices=ROLES, required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("username")
    args = parser.parse_args()
    if not 1 <= args.admin_port <= 65535:
        parser.error("--admin-port must be between 1 and 65535")
    admin_key = os.environ.get("FP_GATEWAY_ADMIN_KEY", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", admin_key):
        parser.error("FP_GATEWAY_ADMIN_KEY must contain at least 24 URL-safe characters")
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{args.admin_port}/apisix/admin/",
            headers={"X-API-KEY": admin_key},
            timeout=15,
            trust_env=False,
        ) as client:
            if args.command == "configure":
                configure(client, Settings())  # type: ignore[call-arg]
                print("Gateway routes configured; existing personal keys preserved.")
            elif args.command == "issue":
                key = secrets.token_urlsafe(32)
                provision(client, args.username, args.role, key)
                print(key)
            else:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.username):
                    raise ValueError("Invalid consumer name")
                client.delete(f"consumers/{args.username}").raise_for_status()
                print("Consumer revoked.")
    except ValidationError as err:
        for error in err.errors(include_input=False):
            print(f"Invalid gateway configuration: {error['loc']}: {error['msg']}", file=sys.stderr)
        raise SystemExit(1) from err
    except (httpx.HTTPError, ValueError) as err:
        print(f"Gateway administration failed: {err}", file=sys.stderr)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
