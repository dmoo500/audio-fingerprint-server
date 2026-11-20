"""Bound and validate administrator request values."""

import json
from typing import cast

from aiohttp import web
from pydantic import BaseModel, ValidationError

from fingerprint_server.admin.data import Dataset, Filter
from fingerprint_server.aiohttp_support import HTTPError


async def parse_model[Model: BaseModel](request: web.Request, model: type[Model]) -> Model:
    """Parse a JSON request using the public validation error used by the admin API."""
    try:
        value = await request.json()
        return model.model_validate(value)
    except (ValueError, UnicodeDecodeError, ValidationError, web.HTTPBadRequest) as err:
        raise HTTPError(422, "Invalid administrator request") from err


def query_int(
    request: web.Request,
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read an integer query parameter and enforce its supported bounds."""
    try:
        value = int(request.query.get(name, str(default)))
    except ValueError as err:
        raise HTTPError(422, "Invalid administrator request") from err
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise HTTPError(422, "Invalid administrator request")
    return value


def query_dataset(request: web.Request) -> Dataset:
    """Return one of the datasets available to the administrator tables."""
    dataset = request.match_info["dataset"]
    if dataset not in ("submissions", "unresolved", "references"):
        raise HTTPError(422, "Invalid administrator request")
    return cast(Dataset, dataset)


def parse_filters(value: str) -> Filter:
    """Decode and type-check the bounded table filter expression."""
    try:
        filters = json.loads(value)
    except (ValueError, RecursionError) as err:
        raise HTTPError(422, "Invalid filter expression") from err
    if filters is not None and not isinstance(filters, (dict, str)):
        raise HTTPError(422, "Invalid filter expression")
    return filters
