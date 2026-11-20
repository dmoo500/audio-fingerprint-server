"""Admin personal and application credential routes."""

import logging
import re
import secrets
from typing import Any, Literal

import httpx
from aiohttp import web
from pydantic import BaseModel, Field, StrictBool

from fingerprint_server.admin.api.dependencies import data_for
from fingerprint_server.admin.api.request import parse_model
from fingerprint_server.admin.audit import ACTION
from fingerprint_server.admin.state import CONFIG_KEY, GATEWAY_KEY
from fingerprint_server.aiohttp_support import HTTPError
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database.application_keys import ApplicationKeys

LOGGER = logging.getLogger(__name__)


class KeyAction(BaseModel):
    operation: Literal["issue", "revoke"]
    username: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    role: Literal["read", "upload", "both"] | None = None
    confirmed: StrictBool = False


class ApplicationAction(BaseModel):
    operation: Literal["issue", "revoke"]
    name: str = Field(default="", max_length=100)
    id: str = Field(default="", max_length=32)
    confirmed: StrictBool = False


async def gateway_call(
    request: web.Request, method: str, path: str, **kwargs: Any
) -> httpx.Response:
    """Make one authenticated request to the configured API gateway."""
    gateway = request.app[GATEWAY_KEY]
    response = await gateway.request(method, path, **kwargs)
    response.raise_for_status()
    return response


def gateway_document(response: httpx.Response) -> dict[str, Any]:
    """Decode a gateway object and reject malformed administration data."""
    try:
        body = response.json()
    except ValueError as err:
        raise HTTPError(502, "Gateway returned invalid administration data") from err
    if not isinstance(body, dict):
        raise HTTPError(502, "Gateway returned invalid administration data")
    return body


PERSONAL_ROLES = ("read", "upload", "both")


async def list_keys(request: web.Request) -> web.Response:
    """List personal gateway keys without exposing their secret values."""
    document = gateway_document(await gateway_call(request, "GET", "consumers"))
    entries = document.get("list", [])
    if not isinstance(entries, list):
        raise HTTPError(502, "Gateway returned invalid administration data")
    items = []
    for item in entries:
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(value, dict) or not isinstance(value.get("username"), str):
            raise HTTPError(502, "Gateway returned invalid administration data")
        if value.get("group_id") in PERSONAL_ROLES:
            items.append({"username": value["username"], "role": value["group_id"]})
    return web.json_response({"items": sorted(items, key=lambda item: item["username"])})


async def change_key(request: web.Request) -> web.Response:
    """Issue a personal gateway key or revoke a confirmed key."""
    body = await parse_model(request, KeyAction)
    request[ACTION] = body.operation
    if body.operation == "issue":
        if body.role is None or len(body.username) > 31:
            raise HTTPError(400, "Choose a role and enter at most 31 characters")
        username = f"{body.username}-{secrets.token_hex(16)}"
        key = secrets.token_urlsafe(32)
        await gateway_call(
            request,
            "PUT",
            f"consumers/{username}",
            json={
                "username": username,
                "group_id": body.role,
                "plugins": {"key-auth": {"key": key}},
            },
        )
        LOGGER.info("Admin issued personal key user=%s role=%s", username, body.role)
        return web.json_response({"new_key": key})
    if not body.confirmed:
        raise HTTPError(400, "Confirm revocation first")
    path = f"consumers/{body.username}"
    existing = gateway_document(await gateway_call(request, "GET", path)).get("value")
    if not isinstance(existing, dict):
        raise HTTPError(502, "Gateway returned invalid administration data")
    if existing.get("group_id") not in PERSONAL_ROLES:
        raise HTTPError(400, "This is not a fingerprint personal consumer")
    await gateway_call(request, "DELETE", path)
    LOGGER.info("Admin revoked personal key user=%s", body.username)
    return web.json_response({})


async def applications(request: web.Request) -> web.Response:
    """List application credential metadata without returning any key values."""
    config = request.app[CONFIG_KEY]
    return web.json_response(
        {
            "items": await run_sync(ApplicationKeys(data_for(request).store).list),
            "legacy_configured": config.gateway_enabled and config.application_key is not None,
        }
    )


async def change_application(request: web.Request) -> web.Response:
    """Issue an application key or revoke a specifically confirmed key."""
    body = await parse_model(request, ApplicationAction)
    request[ACTION] = body.operation
    registry = ApplicationKeys(data_for(request).store)
    if body.operation == "issue":
        try:
            identifier, key = await run_sync(registry.issue, body.name)
        except ValueError as err:
            raise HTTPError(400, str(err)) from err
        LOGGER.info("Admin issued application key id=%s", identifier)
        return web.json_response({"new_key": key})
    if not body.confirmed:
        raise HTTPError(400, "Confirm revocation first")
    if not re.fullmatch(r"[a-f0-9]{32}", body.id):
        raise HTTPError(400, "Invalid application identity")
    try:
        await run_sync(registry.revoke, body.id)
    except ValueError as err:
        raise HTTPError(400, str(err)) from err
    LOGGER.info("Admin revoked application key id=%s", body.id)
    return web.json_response({})
