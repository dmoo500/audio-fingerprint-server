"""Durable artwork migration, album selection, privacy and external failure tests."""

import asyncio
import json
import time
from contextlib import suppress
from email.utils import formatdate
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from aiohttp_client import TestClient
from test_recognition import HEADERS, PCM, matched, settings
from test_server import settings as api_settings

from fingerprint_server import artwork, artwork_store
from fingerprint_server.artwork import ArtworkResolver, ArtworkStore, normalized
from fingerprint_server.database.codes import ARTWORK_REASONS, ArtworkState, decode_artwork_state
from fingerprint_server.database.sqlite import Store
from fingerprint_server.http_utils import trusted_image
from fingerprint_server.musicbrainz import MusicBrainzService
from fingerprint_server.recognition.app import create_app
from fingerprint_server.worker import Worker

REFERENCE = "b" * 64
RECORDING = str(uuid4())
GROUP = str(uuid4())
RELEASE = str(uuid4())
IMAGE = "https://archive.org/download/example/cover-500.jpg"


def store_at(path: Path) -> Store:
    """Create an isolated published reference without copyrighted audio."""
    store = Store(path / "artwork.db")
    store.initialize()
    queue = ArtworkStore(store)
    queue.initialize()
    with store.connect() as db:
        db.execute(
            "INSERT INTO reference_tracks(reference_key,metadata,created_at,publication_ready) "
            "VALUES(?,?,?,1)",
            (REFERENCE, json.dumps({"recording_mbid": RECORDING, "album": "Example album"}), 0),
        )
    return store


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("date_header", [False, True])
async def test_cover_respects_long_retry_after(
    tmp_path: Path, status: int, date_header: bool
) -> None:
    store = store_at(tmp_path)
    header = formatdate(time.time() + 3600, usegmt=True) if date_header else "3600"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, headers={"Retry-After": header})
        )
    ) as client:
        resolver = ArtworkResolver(store, MusicBrainzService(store, api_settings(tmp_path), client))
        with pytest.raises(httpx.HTTPStatusError):
            await resolver.cover(GROUP)
        assert resolver.next_cover - time.monotonic() > 3598


def test_additive_migration_and_queue_survive_restart(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    queue = ArtworkStore(store)
    assert queue.request("c" * 64) == {"status": "missing", "url": None}
    assert queue.request(REFERENCE) == {"status": "pending", "url": None}
    store.initialize()
    queue.initialize()
    assert queue.next()["reference_key"] == REFERENCE
    queue.finish(REFERENCE, IMAGE)
    queue.initialize()
    assert queue.request(REFERENCE) == {"status": "ready", "url": IMAGE}
    with store.connect() as db:
        db.execute("UPDATE artwork_requests SET expires_at=0")
    assert queue.request(REFERENCE)["status"] == "pending"
    queue.invalidate(REFERENCE)
    assert queue.next() is None


@pytest.mark.parametrize("state", ["unexpected", "", "~0"])
def test_legacy_artwork_codes_preserve_unknown_values(tmp_path: Path, state: str) -> None:
    store = store_at(tmp_path)
    with store.connect() as db:
        db.execute("DROP TABLE artwork_requests")
        db.execute(
            "CREATE TABLE artwork_requests(reference_key text PRIMARY KEY,state text NOT NULL,"
            "url text,due_at double precision NOT NULL,expires_at double precision NOT NULL,"
            "reason text)"
        )
        db.execute(
            "INSERT INTO artwork_requests VALUES(?,?,NULL,0,9999999999,'~0')",
            (REFERENCE, state),
        )
        db.execute("UPDATE service_state SET value='3' WHERE key='artwork_schema'")
    queue = ArtworkStore(store)
    queue.initialize()
    queue.initialize()
    assert queue.request(REFERENCE) == {"status": state, "url": None}
    with store.connect() as db:
        row = db.execute(
            "SELECT state,state_raw,reason,typeof(state) AS storage FROM artwork_requests"
        ).fetchone()
    assert row["state"] == ArtworkState.UNKNOWN
    assert row["state_raw"] == state
    assert ARTWORK_REASONS.decode(row["reason"]) == "~0"
    assert row["storage"] == "integer"


@pytest.mark.parametrize("version", [None, "1"])
@pytest.mark.parametrize("has_reason", [False, True])
def test_v1_migration_preserves_covers_and_rechecks_only_old_failures(
    tmp_path: Path, version: str | None, has_reason: bool
) -> None:
    store = store_at(tmp_path)
    with store.connect() as db:
        db.execute("DROP TABLE artwork_requests")
        db.execute(
            "CREATE TABLE artwork_requests(reference_key text PRIMARY KEY,state text NOT NULL,"
            "url text,due_at double precision NOT NULL,expires_at double precision NOT NULL)"
        )
        db.executemany(
            "INSERT INTO artwork_requests VALUES(?,?,?,?,?)",
            [
                (REFERENCE, "ready", IMAGE, 12, 9999999999),
                ("c" * 64, "missing", None, 13, 9999999999),
                ("d" * 64, "unavailable", None, 14, 9999999999),
                ("e" * 64, "pending", None, 15, 9999999999),
            ],
        )
        if has_reason:
            db.execute("ALTER TABLE artwork_requests ADD COLUMN reason text")
        if version is None:
            db.execute("DELETE FROM service_state WHERE key='artwork_schema'")
        else:
            db.execute("UPDATE service_state SET value=? WHERE key='artwork_schema'", (version,))
    queue = ArtworkStore(store)
    queue.initialize()
    queue.initialize()
    with store.connect() as db:
        rows = db.execute("SELECT * FROM artwork_requests ORDER BY reference_key").fetchall()
        assert rows[0]["url"] == IMAGE
        assert rows[0]["expires_at"] == 9999999999
        assert [ARTWORK_REASONS.decode(item["reason"]) for item in rows] == [
            None,
            "legacy_retry",
            "legacy_retry",
            None,
        ]
        assert [item["expires_at"] for item in rows[1:]] == [0, 0, 9999999999]
        assert (
            db.execute("SELECT value FROM service_state WHERE key='artwork_schema'").fetchone()[
                "value"
            ]
            == "4"
        )
    queue.finish("c" * 64, None, reason="no_cover")
    queue.initialize()
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT expires_at FROM artwork_requests WHERE reference_key=?", ("c" * 64,)
            ).fetchone()["expires_at"]
            > time.time()
        )


@pytest.mark.parametrize("outcome", ["release", "group", "conflict", "unknown", "ambiguous"])
async def test_verified_release_hints_select_exact_edition(tmp_path: Path, outcome: str) -> None:
    store = store_at(tmp_path)
    other_group = str(uuid4())
    metadata = {
        "recording_mbid": RECORDING,
        "album": "Different spelling",
        "release_group_mbid": GROUP if outcome != "conflict" else other_group,
    }
    if outcome != "group":
        metadata["release_mbid"] = str(uuid4()) if outcome == "unknown" else RELEASE
    if outcome == "ambiguous":
        metadata.pop("release_group_mbid")
        metadata.pop("release_mbid")
        metadata["album"] = "Example album"
    with store.connect() as db:
        db.execute(
            "UPDATE reference_tracks SET metadata=? WHERE reference_key=?",
            (json.dumps(metadata), REFERENCE),
        )
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "musicbrainz.org":
            assert request.url.path == "/ws/2/release"
            assert request.url.params["recording"] == RECORDING
            return httpx.Response(
                200,
                json={
                    "release-count": 2,
                    "release-offset": 0,
                    "releases": [
                        {"id": RELEASE, "title": "Example album", "release-group": {"id": GROUP}},
                        {
                            "id": str(uuid4()),
                            "title": "Example album",
                            "release-group": {"id": other_group},
                        },
                    ],
                },
            )
        assert request.url.path == (
            f"/release/{RELEASE}/front-500"
            if outcome == "release"
            else f"/release-group/{GROUP}/front-500"
        )
        return httpx.Response(307, headers={"Location": IMAGE})

    queue = ArtworkStore(store)
    queue.request(REFERENCE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        resolver = ArtworkResolver(store, MusicBrainzService(store, api_settings(tmp_path), client))
        await resolver.process_one()
    expected = {
        "release": "ready_release",
        "group": "ready_group",
        "conflict": "conflicting_release_hint",
        "unknown": "release_hint_not_found",
        "ambiguous": "ambiguous_album",
    }[outcome]
    with store.connect() as db:
        row = db.execute("SELECT reason,url FROM artwork_requests").fetchone()
        assert ARTWORK_REASONS.decode(row["reason"]) == expected
        assert row["url"] == (IMAGE if outcome in ("release", "group") else None)
    assert len(calls) == (2 if outcome in ("release", "group") else 1)


async def test_release_fallback_ranks_cover_evidence_and_respects_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path)
    editions = [
        {"id": str(uuid4()), "group": GROUP, "title": "Example album", "status": "Official"},
        {
            "id": str(uuid4()),
            "group": GROUP,
            "title": "Different edition",
            "status": "Official",
            "front": True,
        },
        {"id": str(uuid4()), "group": GROUP, "title": "Example album", "status": "Bootleg"},
        {"id": str(uuid4()), "group": GROUP, "title": "Example album", "status": "Bootleg"},
    ]
    calls = []

    async def cover(identifier: str, *, release: bool = False) -> str | None:
        calls.append((identifier, release))
        return None

    async with httpx.AsyncClient() as client:
        resolver = ArtworkResolver(store, MusicBrainzService(store, api_settings(tmp_path), client))
        monkeypatch.setattr(resolver, "cover", cover)
        result = await resolver.album_cover(normalized("Example album"), editions)
        assert result.reason == "search_limit"
        assert calls[0] == (GROUP, False)
        assert calls[1] == (editions[1]["id"], True)
        assert calls[2] == (editions[0]["id"], True)
        assert len(calls) == 1 + artwork.RELEASE_FALLBACK_LIMIT


@pytest.mark.parametrize("total", [101, 301])
async def test_artwork_release_pagination_is_bounded_and_does_not_guess(
    tmp_path: Path, total: int
) -> None:
    store = store_at(tmp_path)
    offsets = []

    def upstream(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(
            200,
            json={
                "release-count": total,
                "release-offset": offset,
                "releases": [
                    {"id": str(uuid4()), "title": "Example album", "release-group": {"id": GROUP}}
                    for _ in range(min(100, total - offset))
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = MusicBrainzService(store, api_settings(tmp_path), client)
        result = await service.lookup("artwork", RECORDING)
        assert result["complete"] is (total == 101)
        assert len(result["releases"]) == min(300, total)
        assert offsets == ([0, 100] if total == 101 else [0, 100, 200])
        await service.lookup("artwork", RECORDING)
        assert len(offsets) == (2 if total == 101 else 3)
        if total == 301:
            resolver = ArtworkResolver(store, service)
            decision = await resolver.album_cover(
                normalized("Example album"), result["releases"], complete=False
            )
            assert decision.reason == "incomplete_releases"


async def test_artwork_pagination_advances_by_returned_release_count(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    offsets = []

    def upstream(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(
            200,
            json={
                "release-count": 5,
                "release-offset": offset,
                "releases": [
                    {"id": str(uuid4()), "title": "Example album", "release-group": {"id": GROUP}}
                    for _ in range(min(2, 5 - offset))
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = MusicBrainzService(store, api_settings(tmp_path), client)
        result = await service.lookup("artwork", RECORDING)
        assert result["complete"] is True
        assert len(result["releases"]) == 5
        assert offsets == [0, 2, 4]


def test_album_edition_qualifier_does_not_prevent_representative_cover() -> None:
    assert normalized("Scream Aim Fire (Deluxe Edition)") == normalized("Scream Aim Fire")
    assert normalized("Example (Special Edition)") == normalized("Example")
    assert normalized("Life Starts Now (Deluxe Version)") == normalized("Life Starts Now")
    assert normalized("Example (Live)") != normalized("Example")
    assert normalized("Example (Acoustic Version)") != normalized("Example")


@pytest.mark.parametrize(
    ("album", "base"),
    [
        ("Dirt (Remastered)", "Dirt"),
        ("Orthodox (Bonus Track Version)", "Orthodox"),
        ("American Idiot (20th\u00a0Anniversary Deluxe Edition)", "American Idiot"),
        ("Royalty - EP", "Royalty"),
        ("Royalty (EP)", "Royalty"),
        ("30 - Single", "30"),
        ("30 (Single)", "30"),
    ],
)
def test_common_edition_qualifiers_match(album: str, base: str) -> None:
    assert normalized(album) == normalized(base)


@pytest.mark.parametrize(("label", "group_type"), [("Single", "Single"), ("EP", "EP")])
async def test_explicit_single_or_ep_chooses_earliest_official_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, group_type: str
) -> None:
    store = store_at(tmp_path)
    newer = str(uuid4())
    releases = [
        {
            "title": "Song",
            "group": GROUP,
            "group_type": group_type,
            "status": "Official",
            "date": "2018-03-03",
        },
        {
            "title": "Song",
            "group": newer,
            "group_type": group_type,
            "status": "Official",
            "date": "2021-01-01",
        },
        {
            "title": "Song",
            "group": str(uuid4()),
            "group_type": "Album",
            "status": "Official",
            "date": "2010-01-01",
        },
    ]
    calls = []

    async def cover(identifier: str, *, release: bool = False) -> str | None:
        calls.append((identifier, release))
        return IMAGE

    async with httpx.AsyncClient() as client:
        resolver = ArtworkResolver(store, MusicBrainzService(store, api_settings(tmp_path), client))
        monkeypatch.setattr(resolver, "cover", cover)
        assert (await resolver.album_cover(f"Song - {label}", releases)).url == IMAGE
        assert calls == [(GROUP, False)]
        calls.clear()
        releases[1]["date"] = None
        assert (await resolver.album_cover(f"Song - {label}", releases)).reason == "ambiguous_album"
        assert not calls
        releases[1]["date"] = "2018-03-03"
        assert (await resolver.album_cover(f"Song - {label}", releases)).reason == "ambiguous_album"
        releases[1]["date"] = "2021-01-01"
        releases[2].pop("group_type")
        assert (await resolver.album_cover(f"Song - {label}", releases)).reason == "ambiguous_album"
        assert (
            await resolver.album_cover(f"Song - {label}", [releases[2]])
        ).reason == "ambiguous_album"
        releases[2]["group_type"] = "Album"
        assert (
            await resolver.album_cover(f"Song - {label}", [releases[2]])
        ).reason == "album_not_found"
        assert (
            await resolver.album_cover(f"Song - {label}", releases, complete=False)
        ).reason == "incomplete_releases"


@pytest.mark.parametrize("edition", ["Deluxe Version", "Expanded Edition"])
async def test_named_edition_uses_only_exact_release_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edition: str
) -> None:
    store = store_at(tmp_path)
    title = f"Record ({edition})"
    releases = [
        {"id": RELEASE, "group": GROUP, "title": title},
        {"id": str(uuid4()), "group": GROUP, "title": "Record"},
    ]
    calls = []

    async def cover(identifier: str, *, release: bool = False) -> str | None:
        calls.append((identifier, release))
        return None

    async with httpx.AsyncClient() as client:
        resolver = ArtworkResolver(store, MusicBrainzService(store, api_settings(tmp_path), client))
        monkeypatch.setattr(resolver, "cover", cover)
        assert (await resolver.album_cover(title, releases)).reason == "no_cover"
        assert calls == [(RELEASE, True)]
        calls.clear()
        assert (await resolver.album_cover(title, releases[1:])).reason == "album_not_found"
        assert not calls


@pytest.mark.parametrize("edition", ["Deluxe Version", "Expanded Edition"])
async def test_multiple_exact_edition_releases_use_unique_earliest_official_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edition: str
) -> None:
    store = store_at(tmp_path)
    newer = str(uuid4())
    title = f"Record ({edition})"
    releases = [
        {
            "id": RELEASE,
            "group": GROUP,
            "title": title,
            "status": "Official",
            "date": "2018-01-01",
        },
        {
            "id": newer,
            "group": GROUP,
            "title": title,
            "status": "Official",
            "date": "2021-01-01",
        },
    ]
    calls = []

    async def cover(identifier: str, *, release: bool = False) -> str | None:
        calls.append((identifier, release))
        return IMAGE

    async with httpx.AsyncClient() as client:
        resolver = ArtworkResolver(store, MusicBrainzService(store, api_settings(tmp_path), client))
        monkeypatch.setattr(resolver, "cover", cover)
        assert (await resolver.album_cover(title, releases)).url == IMAGE
        assert calls == [(RELEASE, True)]
        calls.clear()
        releases[1]["date"] = None
        assert (await resolver.album_cover(title, releases)).reason == "ambiguous_album"
        assert not calls


def test_artwork_parser_keeps_release_group_type() -> None:
    result = MusicBrainzService.parse_artwork(
        {
            "release-count": 1,
            "release-offset": 0,
            "releases": [
                {"title": "Song", "release-group": {"id": GROUP, "primary-type": "Single"}}
            ],
        }
    )
    assert result["releases"][0]["group_type"] == "Single"


@pytest.mark.parametrize("edition", ["Deluxe Version", "Expanded Edition"])
async def test_worker_preserves_edition_name_for_exact_artwork(
    tmp_path: Path, edition: str
) -> None:
    store = store_at(tmp_path)
    title = f"Record ({edition})"
    with store.connect() as db:
        db.execute(
            "UPDATE reference_tracks SET metadata=? WHERE reference_key=?",
            (
                json.dumps({"recording_mbid": RECORDING, "album": title}),
                REFERENCE,
            ),
        )
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "musicbrainz.org":
            return httpx.Response(
                200,
                json={
                    "release-count": 1,
                    "release-offset": 0,
                    "releases": [
                        {
                            "id": RELEASE,
                            "title": title,
                            "release-group": {"id": GROUP, "primary-type": "Album"},
                        }
                    ],
                },
            )
        assert request.url.path == f"/release/{RELEASE}/front-500"
        return httpx.Response(307, headers={"Location": IMAGE})

    queue = ArtworkStore(store)
    queue.request(REFERENCE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        resolver = ArtworkResolver(store, MusicBrainzService(store, api_settings(tmp_path), client))
        await resolver.process_one()
    assert queue.request(REFERENCE) == {"status": "ready", "url": IMAGE}
    assert len(calls) == 2


async def test_remastered_album_selects_only_unambiguous_recording_release(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "musicbrainz.org":
            return httpx.Response(
                200,
                json={
                    "release-count": 1,
                    "release-offset": 0,
                    "releases": [{"id": RELEASE, "title": "Dirt", "release-group": {"id": GROUP}}],
                },
            )
        assert request.url.path == f"/release-group/{GROUP}/front-500"
        return httpx.Response(307, headers={"Location": IMAGE})

    with store.connect() as db:
        db.execute(
            "UPDATE reference_tracks SET metadata=? WHERE reference_key=?",
            (json.dumps({"recording_mbid": RECORDING, "album": "Dirt (Remastered)"}), REFERENCE),
        )
    queue = ArtworkStore(store)
    queue.request(REFERENCE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        resolver = ArtworkResolver(store, MusicBrainzService(store, api_settings(tmp_path), client))
        await resolver.process_one()
    assert queue.request(REFERENCE) == {"status": "ready", "url": IMAGE}
    assert len(calls) == 2


@pytest.mark.parametrize("version", ["1", "2"])
def test_edition_migration_retries_only_album_misses_once(tmp_path: Path, version: str) -> None:
    store = store_at(tmp_path)
    queue = ArtworkStore(store)
    rows = [
        (REFERENCE, "missing", None, 1, 9999999999, "album_not_found"),
        ("c" * 64, "ready", IMAGE, 1, 9999999999, "ready_group"),
        ("d" * 64, "missing", None, 1, 9999999999, "no_cover"),
        ("e" * 64, "missing", None, 1, 9999999999, "ambiguous_album"),
        ("f" * 64, "pending", None, 1, 9999999999, None),
        ("a" * 64, "unavailable", None, 1, 9999999999, "upstream_error"),
    ]
    with store.connect() as db:
        db.execute("DROP TABLE artwork_requests")
        db.execute(
            "CREATE TABLE artwork_requests(reference_key text PRIMARY KEY,state text NOT NULL,"
            "url text,due_at double precision NOT NULL,expires_at double precision NOT NULL,"
            "reason text)"
        )
        db.executemany("INSERT INTO artwork_requests VALUES(?,?,?,?,?,?)", rows)
        db.execute("UPDATE service_state SET value=? WHERE key='artwork_schema'", (version,))
    queue.initialize()
    queue.initialize()
    with store.connect() as db:
        result = db.execute(
            "SELECT state,state_raw,url,expires_at,reason FROM artwork_requests "
            "WHERE reference_key=?",
            (REFERENCE,),
        ).fetchone()
        assert decode_artwork_state(result["state"], result["state_raw"]) == "missing"
        assert (result["url"], result["expires_at"], ARTWORK_REASONS.decode(result["reason"])) == (
            None,
            0,
            "edition_retry",
        )
        for reference, state, url, _, expires, reason in rows[1:]:
            result = db.execute(
                "SELECT state,state_raw,url,expires_at,reason FROM artwork_requests "
                "WHERE reference_key=?",
                (reference,),
            ).fetchone()
            assert (
                decode_artwork_state(result["state"], result["state_raw"]),
                result["url"],
                result["expires_at"],
                ARTWORK_REASONS.decode(result["reason"]),
            ) == (state, url, expires, reason)
    queue.finish(REFERENCE, None, reason="album_not_found")
    queue.initialize()
    with store.connect() as db:
        result = db.execute(
            "SELECT expires_at,reason FROM artwork_requests WHERE reference_key=?", (REFERENCE,)
        ).fetchone()
        assert result["expires_at"] > time.time()
        assert ARTWORK_REASONS.decode(result["reason"]) == "album_not_found"


def test_prewarm_prioritizes_and_preserves_edition_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path)
    queue = ArtworkStore(store)
    other, retry = "c" * 64, "d" * 64
    with store.connect() as db:
        db.executemany(
            "INSERT INTO reference_tracks(reference_key,metadata,created_at,publication_ready) "
            "VALUES(?,?,0,1)",
            [
                (other, json.dumps({"recording_mbid": RECORDING, "album": "Another album"})),
                (retry, json.dumps({"recording_mbid": RECORDING, "album": "Retry album"})),
            ],
        )
        db.executemany(
            "INSERT INTO artwork_requests(reference_key,state,url,due_at,expires_at,reason) "
            "VALUES(?,?,NULL,0,0,?)",
            [
                (REFERENCE, ArtworkState.MISSING, ARTWORK_REASONS.encode("edition_retry")),
                (retry, ArtworkState.MISSING, ARTWORK_REASONS.encode("edition_retry")),
            ],
        )
    monkeypatch.setattr(artwork_store, "PREWARM_LIMIT", 1)
    first_job = queue.next(prewarm=True)
    assert first_job is not None
    first = first_job["reference_key"]
    assert first in {REFERENCE, retry}
    queue.finish(first, IMAGE)
    second_job = queue.next(prewarm=True)
    assert second_job is not None
    assert second_job["reference_key"] == ({REFERENCE, retry} - {first}).pop()


def test_prewarm_prefers_demand_and_skips_unpublished_or_incomplete_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path)
    queue = ArtworkStore(store)
    other, hidden, incomplete = "c" * 64, "d" * 64, "e" * 64
    metadata = json.dumps({"recording_mbid": RECORDING, "album": "Example album"})
    with store.connect() as db:
        db.executemany(
            "INSERT INTO reference_tracks(reference_key,metadata,created_at,publication_ready) "
            "VALUES(?,?,0,?)",
            [(other, metadata, 1), (hidden, metadata, 0), (incomplete, "{}", 1)],
        )
    assert queue.next() is None
    queue.request(REFERENCE)
    assert queue.next(prewarm=True)["reference_key"] == REFERENCE
    queue.finish(REFERENCE, IMAGE)
    with monkeypatch.context() as cap:
        cap.setattr(artwork_store, "PREWARM_LIMIT", 1)
        assert queue.next(prewarm=True) is None
    assert queue.next(prewarm=True)["reference_key"] == other
    assert queue.request(other)["status"] == "pending"
    queue.finish(other, None, failed=True)
    with store.connect() as db:
        db.execute(
            "UPDATE artwork_requests SET expires_at=? WHERE reference_key=?",
            (time.time() - 1, other),
        )
    assert queue.next(prewarm=True) is None
    assert queue.request(other)["status"] == "pending"
    assert queue.next()["reference_key"] == other


async def test_idle_worker_prepares_artwork_without_a_request(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    contacted = asyncio.Event()

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "musicbrainz.org":
            return httpx.Response(
                200,
                json={
                    "release-count": 1,
                    "release-offset": 0,
                    "releases": [{"title": "Example album", "release-group": {"id": GROUP}}],
                },
            )
        assert request.url.path == f"/release-group/{GROUP}/front-500"
        contacted.set()
        return httpx.Response(307, headers={"Location": IMAGE})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        config = api_settings(tmp_path).model_copy(update={"musicbrainz_enabled": True})
        worker = Worker(store, config, client)
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(contacted.wait(), timeout=3)
            async with asyncio.timeout(3):
                while True:
                    with store.connect() as db:
                        row = db.execute(
                            "SELECT state,url FROM artwork_requests WHERE reference_key=?",
                            (REFERENCE,),
                        ).fetchone()
                    if row is not None and row["state"] == ArtworkState.READY:
                        assert row["url"] == IMAGE
                        break
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.parametrize(
    "outcome",
    ["cover", "group-title", "missing", "fallback", "outage", "ambiguous", "untrusted"],
)
async def test_worker_resolves_album_and_distinguishes_absence_from_failure(
    tmp_path: Path, outcome: str
) -> None:
    store = store_at(tmp_path)
    queue = ArtworkStore(store)
    queue.request(REFERENCE)
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "musicbrainz.org":
            releases = [
                {
                    "id": RELEASE,
                    "title": (
                        "A differently titled edition"
                        if outcome == "group-title"
                        else "Example album (Deluxe Version)"
                    ),
                    "release-group": {"id": GROUP, "title": "Example album"},
                }
            ]
            if outcome == "ambiguous":
                releases.append(
                    {
                        "title": "Another edition",
                        "release-group": {"id": str(uuid4()), "title": "Example album"},
                    }
                )
            return httpx.Response(
                200,
                json={"releases": releases, "release-count": len(releases), "release-offset": 0},
            )
        assert request.method == "HEAD"
        if request.url.path == f"/release/{RELEASE}/front-500":
            assert outcome in ("fallback", "missing")
            return (
                httpx.Response(307, headers={"Location": IMAGE})
                if outcome == "fallback"
                else httpx.Response(404)
            )
        assert request.url.path == f"/release-group/{GROUP}/front-500"
        if outcome in ("fallback", "missing"):
            return httpx.Response(404)
        if outcome == "outage":
            return httpx.Response(503)
        return httpx.Response(
            307,
            headers={
                "Location": "https://untrusted.example/cover" if outcome == "untrusted" else IMAGE
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        resolver = ArtworkResolver(store, MusicBrainzService(store, api_settings(tmp_path), client))
        await resolver.process_one()
        ready = outcome in ("cover", "group-title", "fallback")
        expected = (
            "ready" if ready else "unavailable" if outcome in ("outage", "untrusted") else "missing"
        )
        assert queue.request(REFERENCE) == {
            "status": expected,
            "url": IMAGE if ready else None,
        }
        assert any("/release/" in request.url.path for request in calls) == (
            outcome in ("missing", "fallback")
        )
        if outcome == "ambiguous":
            assert len(calls) == 1
        count = len(calls)
        await resolver.process_one()
        assert len(calls) == count
        if outcome in ("cover", "missing"):
            assert await resolver.cover(GROUP) == (IMAGE if outcome == "cover" else None)
            assert len(calls) == count
        if outcome == "outage":
            assert resolver.next_cover > time.monotonic() + 50


def test_facade_uses_expiring_opaque_cover_token_and_rejects_untrusted_url() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if "/artwork/" in request.url.path:
            assert request.url.path.endswith(REFERENCE)
            return httpx.Response(200, json={"status": "ready", "url": IMAGE})
        result = matched()
        result["match"]["reference_id"] = REFERENCE
        return httpx.Response(200, json=result)

    with TestClient(create_app(settings(), transport=httpx.MockTransport(upstream))) as client:
        response = client.post("/api/recognize?sample_rate=8000", content=PCM, headers=HEADERS)
        path = response.json()["match"]["artwork"]
        assert REFERENCE not in response.text
        assert client.get(path).json() == {"status": "ready", "url": IMAGE}
        assert client.get("/api/artwork/unknown").status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "http://archive.org/a",
        "https://archive.org.evil.example/a",
        "https://user:password@archive.org/a",  # pragma: allowlist secret
        "https://127.0.0.1/a",
    ],
)
def test_artwork_image_sources_are_restricted(url: str) -> None:
    assert not trusted_image(url)
