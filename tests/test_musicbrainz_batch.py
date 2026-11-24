"""Batch recording lookups and authoritative ISRC verification."""

import json

import httpx
import pytest
from test_musicbrainz_validation import ISRC, MBID, OTHER, online_settings, online_store, recording

from fingerprint_server.musicbrainz import MusicBrainzService, MusicBrainzUnavailable


def rows():
    return [
        {"id": "a", "document": json.dumps({"metadata": {"recording_mbid": MBID}})},
        {"id": "b", "document": json.dumps({"metadata": {"recording_mbid": OTHER}})},
    ]


async def test_batch_recordings_are_reused(tmp_path):
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json={"count": 1, "offset": 0, "recordings": [recording()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
        assert (await service.verify({"recording_mbid": MBID}))["recording_mbid"] == MBID
        await service.prefetch(rows()[:1])
        assert not service.batch_recordings
    assert len(calls) == 1
    assert 'rid:"' + MBID.replace("-", r"\-") + '"' in calls[0].url.params["query"]


async def test_batch_cannot_hide_ambiguous_isrc_recordings(tmp_path):
    calls = []

    def reply(request):
        calls.append(request.url.path)
        if "query" in request.url.params:
            return httpx.Response(200, json={"count": 1, "offset": 0, "recordings": [recording()]})
        return httpx.Response(
            200,
            json={
                "isrc": ISRC,
                "recording-count": 2,
                "recordings": [recording(), recording(OTHER)],
            },
        )

    metadata = {"isrc": ISRC, "title": "Title", "artists": ["Artist"], "album": "Album"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
        assert await service.verify(metadata) == metadata
    assert len(calls) == 2
    assert calls[-1].endswith("/isrc/" + ISRC)


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
                    "recordings": [recording("00000000-0000-4000-8000-000000000003")],
                },
            )
        )
    ) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        await service.prefetch(rows())
    assert not service.batch_recordings


async def test_batch_uses_shared_cooldown_without_negative_evidence(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as client:
        service = MusicBrainzService(online_store(tmp_path), online_settings(tmp_path), client)
        with pytest.raises(MusicBrainzUnavailable):
            await service.prefetch(rows())
    assert not service.batch_submissions
    assert not service.batch_recordings
    assert service.cooldown_until > 0
