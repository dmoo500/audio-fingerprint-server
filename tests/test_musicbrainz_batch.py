"""Batch identifier evidence must stay exact and preserve direct lookup fallbacks."""

import json

import httpx
import pytest
from test_musicbrainz_validation import ISRC, MBID, OTHER, online_settings, online_store, recording

from fingerprint_server.musicbrainz import MusicBrainzService, MusicBrainzUnavailable


def search_recording(mbid=MBID):
    item = {**recording(mbid), "length": 12000}
    item.pop("isrcs")
    item.pop("release-count")
    return item


@pytest.mark.parametrize("confirmed", [True, False])
async def test_real_search_candidates_require_hydrated_isrc_evidence(tmp_path, confirmed):
    calls = []

    def reply(request):
        calls.append(request)
        if "query" in request.url.params:
            return httpx.Response(
                200, json={"count": 1, "offset": 0, "recordings": [search_recording()]}
            )
        if "/recording/" in request.url.path:
            return httpx.Response(
                200, json={**recording(), "length": 12000, "isrcs": [ISRC] if confirmed else []}
            )
        return httpx.Response(
            200, json={"isrc": ISRC, "recordings": [{**recording(), "length": 12000}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
        metadata = {"title": "Title", "artists": ["Artist"], "duration": 12, "album": "Album"}
        assert (await service.verify({**metadata, "isrc": ISRC}))["recording_mbid"] == MBID
    assert calls[1].url.path.endswith("/recording/" + MBID)
    assert len(calls) == (2 if confirmed else 3)
    if not confirmed:
        assert calls[-1].url.path.endswith("/isrc/" + ISRC)


@pytest.mark.parametrize("variant", ["ambiguous", "mismatch", "incomplete"])
async def test_unusable_search_candidates_do_not_trigger_hydration(tmp_path, variant):
    calls = []
    candidate = search_recording()
    if variant == "mismatch":
        candidate["title"] = "Another title"
    candidates = [candidate, search_recording(OTHER)] if variant == "ambiguous" else [candidate]

    def reply(request):
        calls.append(request)
        if "query" in request.url.params:
            return httpx.Response(
                200,
                json={
                    "count": 101 if variant == "incomplete" else len(candidates),
                    "offset": 0,
                    "recordings": candidates,
                },
            )
        return httpx.Response(200, json={"isrc": ISRC, "recordings": [recording()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
        await service.verify(
            {
                "isrc": ISRC,
                "title": "Title",
                "artists": ["Artist"],
                "duration": 12,
                "album": "Album",
            }
        )
    assert len(calls) == 2
    assert calls[-1].url.path.endswith("/isrc/" + ISRC)


@pytest.mark.parametrize("variant", ["changed", "wrong_id", "missing_isrcs", "not_found"])
async def test_hydration_cannot_approve_stale_or_incomplete_evidence(tmp_path, variant):
    calls = []

    def reply(request):
        calls.append(request)
        if "query" in request.url.params:
            return httpx.Response(
                200, json={"count": 1, "offset": 0, "recordings": [search_recording()]}
            )
        if "/recording/" in request.url.path:
            item = {**recording(OTHER if variant == "wrong_id" else MBID), "length": 12000}
            if variant == "changed":
                item["title"] = "Live version"
            if variant == "missing_isrcs":
                item.pop("isrcs")
            return httpx.Response(404 if variant == "not_found" else 200, json=item)
        return httpx.Response(200, json={"isrc": ISRC, "recordings": [recording()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
        await service.verify(
            {
                "isrc": ISRC,
                "title": "Title",
                "artists": ["Artist"],
                "duration": 12,
                "album": "Album",
            }
        )
    assert len(calls) == 3
    assert calls[-1].url.path.endswith("/isrc/" + ISRC)
    assert not service.batch_isrcs


async def test_hydration_cooldown_does_not_fall_through_to_isrc(tmp_path):
    calls = []

    def reply(request):
        calls.append(request)
        if "query" in request.url.params:
            return httpx.Response(
                200, json={"count": 1, "offset": 0, "recordings": [search_recording()]}
            )
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
        with pytest.raises(MusicBrainzUnavailable):
            await service.verify(
                {"isrc": ISRC, "title": "Title", "artists": ["Artist"], "duration": 12}
            )
    assert len(calls) == 2
    assert not service.batch_isrcs
    assert service.cooldown_until > 0


async def test_hydrated_recording_is_reused_and_single_row_clears_batch(tmp_path):
    calls = []

    def reply(request):
        calls.append(request)
        if "query" in request.url.params:
            return httpx.Response(
                200, json={"count": 1, "offset": 0, "recordings": [search_recording()]}
            )
        return httpx.Response(200, json={**recording(), "length": 12000})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
        metadata = {"isrc": ISRC, "title": "Title", "artists": ["Artist"], "duration": 12}
        assert (await service.verify(metadata))["album"] == "Album"
        await service.verify(metadata)
        await service.verify({"recording_mbid": MBID})
        assert len(calls) == 2
        await service.prefetch(rows()[:1])
        assert not service.batch_candidates
        assert not service.batch_recordings
        assert not service.batch_isrcs


def rows():
    return [
        {"id": "a", "document": json.dumps({"metadata": {"isrc": ISRC}})},
        {"id": "b", "document": json.dumps({"metadata": {"recording_mbid": MBID}})},
    ]


async def test_batch_replaces_positive_isrc_and_mbid_lookups(tmp_path):
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(
            200, json={"count": 1, "offset": 0, "recordings": [{**recording(), "length": 12000}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
        metadata = {"title": "Title", "artists": ["Artist"], "duration": 12, "album": "Album"}
        assert (await service.verify({**metadata, "isrc": ISRC}))["recording_mbid"] == MBID
        assert (await service.verify({**metadata, "recording_mbid": MBID}))[
            "recording_mbid"
        ] == MBID
    assert len(calls) == 1
    assert calls[0].url.params["limit"] == "100"
    assert f'isrc:"{ISRC}"' in calls[0].url.params["query"]
    assert 'rid:"' + MBID.replace("-", r"\-") + '"' in calls[0].url.params["query"]


@pytest.mark.parametrize("count", [0, 101])
async def test_missing_and_truncated_batches_use_direct_lookup(tmp_path, count):
    calls = []

    def reply(request):
        calls.append(request)
        if "query" in request.url.params:
            return httpx.Response(200, json={"count": count, "offset": 0, "recordings": []})
        return httpx.Response(200, json=recording())

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
        await service.verify({"recording_mbid": MBID})
    assert len(calls) == 2
    assert calls[-1].url.path.endswith(MBID)


async def test_unrelated_results_never_populate_identifier_evidence(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "count": 1,
                    "offset": 0,
                    "recordings": [{**recording(OTHER), "isrcs": ["GBABC2600001"]}],
                },
            )
        )
    ) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
    assert not service.batch_recordings and not service.batch_isrcs


async def test_batch_uses_shared_cooldown_without_negative_evidence(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        with pytest.raises(MusicBrainzUnavailable):
            await service.prefetch(rows())
    assert not service.batch_submissions
    assert not service.batch_recordings and not service.batch_isrcs
    assert service.cooldown_until > 0
