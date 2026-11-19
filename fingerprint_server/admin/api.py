"""Private same-origin API for the administrator frontend."""

import json
import logging
import re
import secrets
import time
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool

from fingerprint_server.admin.data import AdminData, Dataset
from fingerprint_server.admin.errors import ActionFailed, LoginFailed
from fingerprint_server.admin.history import QUEUE, RECORDINGS, chart
from fingerprint_server.admin.sqlite_data import SQLiteAdminData
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database.application_keys import ApplicationKeys
from fingerprint_server.database.metrics import HOUR

type Data = AdminData | SQLiteAdminData
LOGGER = logging.getLogger(__name__)


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=4096)


class TableAction(BaseModel):
    operation: Literal["retry", "delete"]
    ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=100
    )
    confirmed: Literal[True]


class ReviewSelection(BaseModel):
    submission_id: str = Field(max_length=128)
    selected_mbid: str = Field(max_length=36)


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


def data_for(request: Request) -> Data:
    return request.app.state.data  # type: ignore[no-any-return]


async def require_session(request: Request) -> None:
    if not await request.app.state.auth.is_authenticated(request):
        raise HTTPException(401, "Administrator session required")


router = APIRouter(prefix="/admin/api")
private = APIRouter(dependencies=[Depends(require_session)])


@router.get("/session")
async def session(request: Request) -> dict[str, bool]:
    return {"authenticated": await request.app.state.auth.is_authenticated(request)}


@router.post("/login")
async def login(request: Request, body: Login) -> dict[str, Any]:
    try:
        await request.app.state.auth.login(body.username, body.password, request)
    except LoginFailed as err:
        raise HTTPException(400, str(err)) from err
    return {}


@private.post("/logout")
async def logout(request: Request) -> dict[str, Any]:
    await request.app.state.auth.logout(request)
    return {}


@private.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    live = request.app.state.live
    return {**live.snapshot, "failed": live.failed}


@private.get("/history")
async def history(request: Request) -> dict[str, Any]:
    hour = int(time.time()) // HOUR
    samples = await run_sync(data_for(request).hourly_history, hour)
    return {
        "charts": [
            ("Searchable recordings", chart(samples, hour, RECORDINGS)),
            ("Submission backlog", chart(samples, hour, QUEUE)),
        ],
    }


@private.post("/pending-review")
async def pending_review(request: Request) -> dict[str, Any]:
    await run_sync(data_for(request).store.request_pending_recheck)
    LOGGER.info("Admin requested non-destructive pending review")
    return {}


@private.get("/tables/{dataset}")
async def table(
    request: Request,
    dataset: Dataset,
    skip: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    where: Annotated[str, Query(max_length=5000)] = "null",
    order_by: Annotated[list[str] | None, Query(max_length=16)] = None,
) -> dict[str, Any]:
    try:
        filters = json.loads(where)
    except (ValueError, RecursionError) as err:
        raise HTTPException(422, "Invalid filter expression") from err
    if filters is not None and not isinstance(filters, (dict, str)):
        raise HTTPException(422, "Invalid filter expression")
    data = data_for(request)
    fields = [
        "id",
        "title",
        "artists",
        "album",
        "isrc",
        "recording_mbid",
        "created_at",
        *(
            ("publication_ready", "verification_version", "recording_key")
            if dataset == "references"
            else ("state", "error", "attempts", "pending_rechecks", "reference_id")
        ),
    ]
    try:
        rows = await run_sync(
            data.read,
            dataset,
            filters,
            skip=skip,
            limit=limit,
            order_by=[order.replace(":", " ", 1) for order in order_by or []],
        )
        total = await run_sync(data.read, dataset, filters, count=True)
    except ActionFailed as err:
        raise HTTPException(422, str(err)) from err
    assert isinstance(rows, list) and isinstance(total, int)
    return {
        "items": [{field: row.get(field) for field in fields} for row in rows],
        "total": total,
        "fields": fields,
        "searchable": data.fields(dataset),
        "sortable": data.sortable_fields(dataset),
    }


@private.get("/tables/{dataset}/{identifier}")
async def detail(request: Request, dataset: Dataset, identifier: str) -> dict[str, Any]:
    rows = await run_sync(data_for(request).read, dataset, pks=[identifier], limit=1)
    assert isinstance(rows, list)
    if not rows:
        raise HTTPException(404, "Entry not found")
    return rows[0]


@private.post("/tables/{dataset}/actions")
async def action(request: Request, dataset: Dataset, body: TableAction) -> dict[str, Any]:
    if dataset == "references":
        raise HTTPException(400, "References are read-only")
    await run_sync(data_for(request).mutate, body.ids, body.operation)
    return {}


@private.get("/reviews")
async def reviews(request: Request, skip: int = 0) -> dict[str, Any]:
    items = await run_sync(data_for(request).musicbrainz_reviews, skip)
    return {"items": items[:50], "has_more": len(items) > 50}


@private.post("/reviews")
async def select_review(request: Request, body: ReviewSelection) -> dict[str, Any]:
    await run_sync(
        data_for(request).select_musicbrainz_candidate, body.submission_id, body.selected_mbid
    )
    return {}


async def gateway_call(request: Request, method: str, path: str, **kwargs: Any) -> httpx.Response:
    gateway: httpx.AsyncClient = request.app.state.gateway
    response = await gateway.request(method, path, **kwargs)
    response.raise_for_status()
    return response


def gateway_document(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as err:
        raise HTTPException(502, "Gateway returned invalid administration data") from err
    if not isinstance(body, dict):
        raise HTTPException(502, "Gateway returned invalid administration data")
    return body


PERSONAL_ROLES = ("read", "upload", "both")


@private.get("/keys")
async def list_keys(request: Request) -> dict[str, Any]:
    document = gateway_document(await gateway_call(request, "GET", "consumers"))
    entries = document.get("list", [])
    if not isinstance(entries, list):
        raise HTTPException(502, "Gateway returned invalid administration data")
    items = []
    for item in entries:
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(value, dict) or not isinstance(value.get("username"), str):
            raise HTTPException(502, "Gateway returned invalid administration data")
        if value.get("group_id") in PERSONAL_ROLES:
            items.append({"username": value["username"], "role": value["group_id"]})
    return {"items": sorted(items, key=lambda item: item["username"])}


@private.post("/keys")
async def change_key(request: Request, body: KeyAction) -> dict[str, Any]:
    if body.operation == "issue":
        if body.role is None or len(body.username) > 31:
            raise HTTPException(400, "Choose a role and enter at most 31 characters")
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
        return {"new_key": key}
    if not body.confirmed:
        raise HTTPException(400, "Confirm revocation first")
    path = f"consumers/{body.username}"
    existing = gateway_document(await gateway_call(request, "GET", path)).get("value")
    if not isinstance(existing, dict):
        raise HTTPException(502, "Gateway returned invalid administration data")
    if existing.get("group_id") not in PERSONAL_ROLES:
        raise HTTPException(400, "This is not a fingerprint personal consumer")
    await gateway_call(request, "DELETE", path)
    LOGGER.info("Admin revoked personal key user=%s", body.username)
    return {}


@private.get("/applications")
async def applications(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    return {
        "items": await run_sync(ApplicationKeys(data_for(request).store).list),
        "legacy_configured": config.gateway_enabled and config.application_key is not None,
    }


@private.post("/applications")
async def change_application(request: Request, body: ApplicationAction) -> dict[str, Any]:
    registry = ApplicationKeys(data_for(request).store)
    if body.operation == "issue":
        try:
            identifier, key = await run_sync(registry.issue, body.name)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        LOGGER.info("Admin issued application key id=%s", identifier)
        return {"new_key": key}
    if not body.confirmed:
        raise HTTPException(400, "Confirm revocation first")
    if not re.fullmatch(r"[a-f0-9]{32}", body.id):
        raise HTTPException(400, "Invalid application identity")
    try:
        await run_sync(registry.revoke, body.id)
    except ValueError as err:
        raise HTTPException(400, str(err)) from err
    LOGGER.info("Admin revoked application key id=%s", body.id)
    return {}


router.include_router(private)
