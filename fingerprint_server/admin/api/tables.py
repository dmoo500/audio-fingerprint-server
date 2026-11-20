"""Admin metadata tables and guarded submission actions."""

from typing import Annotated, Literal

from aiohttp import web
from pydantic import BaseModel, Field, ValidationError

from fingerprint_server.admin.api.dependencies import data_for
from fingerprint_server.admin.api.request import parse_filters, query_dataset, query_int
from fingerprint_server.admin.audit import ACTION
from fingerprint_server.admin.errors import ActionFailed
from fingerprint_server.aiohttp_support import HTTPError
from fingerprint_server.async_utils import run_sync


class TableAction(BaseModel):
    operation: Literal["retry", "delete"]
    ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=100
    )
    confirmed: Literal[True]


async def table(request: web.Request) -> web.Response:
    """Return one bounded page of metadata-only table entries."""
    dataset = query_dataset(request)
    skip = query_int(request, "skip", 0, 0, 1_000_000)
    limit = query_int(request, "limit", 50, 1, 100)
    where = request.query.get("where", "null")
    if len(where) > 5000:
        raise HTTPError(422, "Invalid administrator request")
    filters = parse_filters(where)
    order_by = request.query.getall("order_by", [])
    if len(order_by) > 16:
        raise HTTPError(422, "Invalid administrator request")
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
            order_by=[order.replace(":", " ", 1) for order in order_by],
        )
        total = await run_sync(data.read, dataset, filters, count=True)
    except ActionFailed as err:
        raise HTTPError(422, str(err)) from err
    assert isinstance(rows, list) and isinstance(total, int)
    return web.json_response(
        {
            "items": [{field: row.get(field) for field in fields} for row in rows],
            "total": total,
            "fields": fields,
            "searchable": data.fields(dataset),
            "sortable": data.sortable_fields(dataset),
        }
    )


async def detail(request: web.Request) -> web.Response:
    """Return the metadata-only detail for one table entry."""
    dataset = query_dataset(request)
    rows = await run_sync(
        data_for(request).read, dataset, pks=[request.match_info["identifier"]], limit=1
    )
    assert isinstance(rows, list)
    if not rows:
        raise HTTPError(404, "Entry not found")
    return web.json_response(rows[0])


async def action(request: web.Request) -> web.Response:
    """Run one confirmed retry or deletion operation."""
    dataset = query_dataset(request)
    try:
        body = TableAction.model_validate(await request.json())
    except (ValueError, UnicodeDecodeError, ValidationError, web.HTTPBadRequest) as err:
        raise HTTPError(422, "Invalid administrator request") from err
    request[ACTION] = body.operation
    if dataset == "references":
        raise HTTPError(400, "References are read-only")
    await run_sync(data_for(request).mutate, body.ids, body.operation)
    return web.json_response({})
