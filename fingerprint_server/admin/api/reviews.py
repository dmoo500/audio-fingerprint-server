"""Admin MusicBrainz review routes."""

from aiohttp import web
from pydantic import BaseModel, Field

from fingerprint_server.admin.api.dependencies import data_for
from fingerprint_server.admin.api.request import parse_model, query_int
from fingerprint_server.async_utils import run_sync


class ReviewSelection(BaseModel):
    submission_id: str = Field(max_length=128)
    selected_mbid: str = Field(max_length=36)


async def reviews(request: web.Request) -> web.Response:
    """Return one bounded page of pending MusicBrainz reviews."""
    skip = query_int(request, "skip", 0)
    items = await run_sync(data_for(request).musicbrainz_reviews, skip)
    return web.json_response({"items": items[:50], "has_more": len(items) > 50})


async def select_review(request: web.Request) -> web.Response:
    """Select one of the returned candidates for a pending MusicBrainz review."""
    body = await parse_model(request, ReviewSelection)
    await run_sync(
        data_for(request).select_musicbrainz_candidate, body.submission_id, body.selected_mbid
    )
    return web.json_response({})
