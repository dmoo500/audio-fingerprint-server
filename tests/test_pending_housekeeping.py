"""Bounded pending retries, migration grace and logged submission-only deletion."""

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_metadata_gate import document
from test_musicbrainz_validation import (
    ISRC,
    MBID,
    OTHER,
    api,
    online_settings,
    recording,
)

import fingerprint_server.database.migrations as migrations
import fingerprint_server.database.sqlite as storage
from fingerprint_server.database.sqlite import Store
from fingerprint_server.musicbrainz import MusicBrainzService, VerificationPending
from fingerprint_server.schemas import acoustic_reference_id, decode_submission
from fingerprint_server.worker import Worker

WEEK = 7 * 86400


def legacy_without_identifiers(store: Store) -> str:
    payload = document()
    reference_key = acoustic_reference_id(decode_submission(json.dumps(payload)))
    store.submit("legacy", json.dumps(payload), 100)
    store.finish("legacy", reference_key, payload["metadata"], [("0" * 20, 0)], verified=True)
    payload["metadata"] = {"duration": 12.0}
    with store.connect() as db:
        db.execute(
            "UPDATE submissions SET document=?,state='pending_metadata',pending_due_at=0 "
            "WHERE id='legacy'",
            (json.dumps(payload),),
        )
        db.execute(
            "UPDATE reference_tracks SET metadata=?,publication_ready=0 WHERE reference_key=?",
            (json.dumps(payload["metadata"]), reference_key),
        )
    return reference_key


async def test_due_missing_identifier_deletes_unused_reference_and_can_rebuild(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    config = online_settings(tmp_path)
    store = Store(config.database, True)
    store.initialize()
    reference_key = legacy_without_identifiers(store)
    with store.connect() as db:
        db.execute(
            "UPDATE submissions SET pending_due_at=? WHERE id='legacy'", (time.time() + WEEK,)
        )
    assert store.claim() is None
    with store.connect() as db:
        db.execute("UPDATE submissions SET pending_due_at=0 WHERE id='legacy'")
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        task = asyncio.create_task(Worker(store, config, client).run())
        try:
            async with asyncio.timeout(5):
                while "reference deleted with landmarks; landmarks_deleted=1" not in caplog.text:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reference_tracks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM landmarks").fetchone()[0] == 0
    assert "Housekeeping checking submission legacy" in caplog.text
    assert "reference deleted with landmarks; landmarks_deleted=1" in caplog.text
    payload = document()
    store.submit("corrected", json.dumps(payload), 100)
    store.finish("corrected", reference_key, payload["metadata"], [("0" * 20, 0)], verified=True)
    assert store.match([("0" * 20, 0)])


@pytest.mark.parametrize("dependency", ["linked", "unlinked", "published", "invalid"])
def test_missing_identifier_preserves_references_with_dependencies(
    tmp_path: Path, dependency: str
) -> None:
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    reference_key = legacy_without_identifiers(store)
    if dependency == "published":
        with store.connect() as db:
            db.execute("UPDATE reference_tracks SET publication_ready=1")
    else:
        payload = document()
        store.submit("other", json.dumps(payload), 100)
        if dependency == "linked":
            with store.connect() as db:
                db.execute(
                    "UPDATE submissions SET reference_id=? WHERE id='other'", (reference_key,)
                )
        elif dependency == "invalid":
            with store.connect() as db:
                db.execute("UPDATE submissions SET document='not-json' WHERE id='other'")
    assert store.delete_missing_identifier_submission("legacy") == (True, False, 0)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reference_tracks").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM landmarks").fetchone()[0] == 1


def test_missing_identifier_cleanup_rolls_back_on_error(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    legacy_without_identifiers(store)
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_reference_deletion BEFORE DELETE ON reference_tracks "
            "BEGIN SELECT RAISE(ABORT,'simulated failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
        store.delete_missing_identifier_submission("legacy")
    assert store.get_submission("legacy") is not None
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reference_tracks").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM landmarks").fetchone()[0] == 1


def test_unrelated_unlinked_submission_does_not_prevent_cleanup(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    legacy_without_identifiers(store)
    other = document()
    other["fingerprints"] = [["1" * 20, 0]]
    store.submit("other", json.dumps(other), 100)
    assert store.delete_missing_identifier_submission("legacy") == (True, True, 1)
    assert store.get_submission("other") is not None


async def test_manual_check_retains_linked_unresolvable_reference(tmp_path: Path) -> None:
    config = online_settings(tmp_path)
    store = Store(config.database, True)
    store.initialize()
    legacy_without_identifiers(store)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        task = asyncio.create_task(Worker(store, config, client).run(["legacy"]))
        try:
            async with asyncio.timeout(5):
                while lifecycle_row(store, "legacy")["error"] is None:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert lifecycle_row(store, "legacy")["state"] == "pending_metadata"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM landmarks").fetchone()[0] == 1


def test_concurrent_database_change_defers_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    legacy_without_identifiers(store)
    other = document()
    other["fingerprints"] = [["1" * 20, 0]]
    store.submit("other", json.dumps(other), 100)
    original_decode = storage.decode_submission

    def decode_with_concurrent_write(raw: str) -> object:
        with store.connect() as db:
            db.execute("UPDATE submissions SET attempts=attempts+1 WHERE id='other'")
        return original_decode(raw)

    monkeypatch.setattr(storage, "decode_submission", decode_with_concurrent_write)
    assert store.delete_missing_identifier_submission("legacy") == (False, False, 0)
    assert lifecycle_row(store, "legacy")["state"] == "pending_metadata"
    assert lifecycle_row(store, "legacy")["pending_due_at"] > time.time()
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM landmarks").fetchone()[0] == 1


def pending(store: Store, identifier: str = "pending") -> None:
    store.submit(identifier, json.dumps(document()), 100)
    store.pending_metadata(identifier, "MusicBrainz ISRC ambiguous")


def lifecycle_row(store: Store, identifier: str = "pending") -> dict:
    with store.connect() as db:
        row = db.execute("SELECT * FROM submissions WHERE id=?", (identifier,)).fetchone()
    assert row is not None
    return dict(row)


def test_three_checks_at_deadlines_and_reference_survival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1_800_000_000.0]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: now[0]))
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    pending(store)
    store.finish("pending", "retained", document()["metadata"], [("0" * 20, 0)], verified=True)
    store.pending_metadata("pending")
    for check in range(1, 4):
        now[0] += WEEK - 1
        assert store.claim() is None
        now[0] += 1
        row = store.claim()
        assert row and row["pending_recheck"]
        assert row["pending_rechecks"] == check - 1
        deleted = store.complete_pending_check("pending", "still ambiguous", True)
        assert deleted == (check == 3)
        if not deleted:
            assert lifecycle_row(store)["pending_due_at"] == now[0] + WEEK
            store.initialize()
    assert store.get_submission("pending") is None
    assert store.match([("0" * 20, 0)])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM landmarks").fetchone()[0] == 1


@pytest.mark.parametrize("online,enabled", [(True, False), (False, True)])
def test_disabled_housekeeping_does_not_claim_or_delete(
    tmp_path: Path, online: bool, enabled: bool
) -> None:
    store = Store(tmp_path / "test.db", online, enabled)
    store.initialize()
    pending(store)
    with store.connect() as db:
        db.execute("UPDATE submissions SET pending_due_at=0")
    assert store.claim() is None
    assert not store.complete_pending_check("pending", "unresolved", True)
    assert lifecycle_row(store)["pending_rechecks"] == 0


def test_outage_and_crash_do_not_consume_checks(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    pending(store)
    with store.connect() as db:
        db.execute("UPDATE submissions SET pending_due_at=0")
    assert store.claim()["pending_recheck"]
    store.defer_musicbrainz("pending", 60, True)
    row = lifecycle_row(store)
    assert row["pending_rechecks"] == 0 and row["attempts"] == 0
    assert row["pending_due_at"] >= time.time() + 55
    with store.connect() as db:
        db.execute("UPDATE submissions SET pending_due_at=0")
    assert store.claim()["pending_recheck"]
    store.initialize()
    assert lifecycle_row(store)["state"] == "queued"
    assert store.claim()["pending_rechecks"] == 0


def test_activation_defers_unverifiable_retained_reference(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    payload = document()
    store.submit("legacy", json.dumps(payload), 10)
    store.finish("legacy", "reference", payload["metadata"], [("0" * 20, 0)])
    payload["metadata"] = {"duration": 12.0}
    with store.connect() as db:
        db.execute("UPDATE submissions SET document=?", (json.dumps(payload),))
        db.execute("UPDATE reference_tracks SET metadata=?", (json.dumps(payload["metadata"]),))
    online = Store(store.path, True)
    online.initialize()
    assert online.get_submission("legacy")["state"] == "pending_metadata"
    assert lifecycle_row(online, "legacy")["pending_due_at"] >= time.time() + WEEK - 5
    assert online.claim() is None


def test_schema_four_migration_preserves_all_rows_and_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    pending(store)
    with store.connect() as db:
        db.execute("UPDATE submissions SET document='not-json',created_at=1")
        for trigger in (
            "submission_metadata_insert",
            "submission_metadata_update",
            "submission_metadata_fields",
            "submission_metadata_delete",
        ):
            db.execute(f"DROP TRIGGER {trigger}")
        db.execute("DROP TABLE submission_metadata")
        db.execute("DROP INDEX references_recording")
        db.execute("ALTER TABLE reference_tracks DROP COLUMN recording_key")
        db.execute("DROP INDEX submissions_fifo")
        db.execute("DROP INDEX submissions_pending_due")
        for column in (
            "pending_rechecks",
            "pending_due_at",
            "pending_check",
            "enqueued_at",
            "background_kind",
        ):
            db.execute(f"ALTER TABLE submissions DROP COLUMN {column}")
        db.execute("PRAGMA user_version=4")
    now = 1_800_000_000.0
    monkeypatch.setattr(migrations, "time", SimpleNamespace(time=lambda: now, time_ns=lambda: 1))
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: now))
    store.initialize()
    assert lifecycle_row(store)["document"] == "not-json"
    assert lifecycle_row(store)["pending_due_at"] == now + WEEK
    assert store.claim() is None
    store.initialize()
    assert lifecycle_row(store)["pending_due_at"] == now + WEEK
    backup = next(tmp_path.glob("*.pre-v4-*.bak"))
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.execute("SELECT document FROM submissions").fetchone()[0] == "not-json"


@pytest.mark.parametrize("enabled", [True, False])
async def test_missing_identifier_deletion_logged_only_when_enabled(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, enabled: bool
) -> None:
    caplog.set_level(logging.INFO)
    config = online_settings(tmp_path).model_copy(update={"housekeeping_enabled": enabled})
    store = Store(config.database, True, enabled)
    store.initialize()
    payload = document()
    payload["metadata"] = {"duration": 12.0}
    store.submit("no-id", json.dumps(payload), 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        task = asyncio.create_task(Worker(store, config, client).run())
        try:
            async with asyncio.timeout(5):
                while enabled and store.get_submission("no-id") is not None:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert ("Submission no-id deleted as unresolvable" in caplog.text) == enabled
    if enabled:
        assert "MusicBrainz identifier unavailable" in caplog.text
    else:
        assert store.get_submission("no-id")["state"] == "pending_metadata"


@pytest.mark.parametrize("status", [200, 404, 503, "malformed", "incomplete"])
async def test_due_worker_success_exhaustion_or_outage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, status: int | str
) -> None:
    caplog.set_level(logging.INFO)
    config = online_settings(tmp_path)
    store = Store(config.database, True)
    store.initialize()
    pending(store)
    with store.connect() as db:
        db.execute("UPDATE submissions SET pending_due_at=0,pending_rechecks=2")

    def reply(request: httpx.Request) -> httpx.Response:
        if status == 200:
            return api(request)
        if status == "malformed":
            return httpx.Response(200, json={})
        if status == "incomplete":
            return httpx.Response(200, json={"isrc": ISRC, "recordings": [recording(albums=[])]})
        assert isinstance(status, int)
        return httpx.Response(status)

    if status == "incomplete":
        payload = document()
        payload["metadata"].pop("album")
        with store.connect() as db:
            db.execute("UPDATE submissions SET document=?", (json.dumps(payload),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        task = asyncio.create_task(Worker(store, config, client).run())
        try:
            async with asyncio.timeout(5):
                while True:
                    row = store.get_submission("pending")
                    if row is None:
                        # The database write can finish before the worker logs the deletion.
                        if "deleted after 3 unresolved" in caplog.text:
                            break
                        await asyncio.sleep(0.01)
                        continue
                    if row["state"] == "processed":
                        break
                    if (
                        row["state"] == "pending_metadata"
                        and lifecycle_row(store)["pending_due_at"] > time.time()
                    ):
                        break
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    if status == 200:
        assert store.get_submission("pending")["state"] == "processed"
    elif status in (404, "incomplete"):
        assert store.get_submission("pending") is None
        assert "deleted after 3 unresolved" in caplog.text
        assert (
            "reason=MusicBrainz ISRC not found" if status == 404 else "reason=Incomplete metadata"
        ) in caplog.text
    else:
        assert lifecycle_row(store)["pending_rechecks"] == 2
        assert lifecycle_row(store)["pending_due_at"] >= time.time() + 55
    assert (" deleted " in caplog.text) == (status in (404, "incomplete"))


@pytest.mark.parametrize("invalid", [{"pending_recheck_days": 6}, {"pending_max_rechecks": 0}])
def test_housekeeping_settings_validation(tmp_path: Path, invalid: dict) -> None:
    config = online_settings(tmp_path).model_dump()
    config.update(invalid)
    with pytest.raises(ValidationError):
        type(online_settings(tmp_path))(**config)


async def test_official_isrc_shape_and_mbid_disambiguation(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db", True)
    store.initialize()
    responses = [
        {"isrc": ISRC, "recordings": [recording()]},
        {"isrc": ISRC, "recordings": []},
        {"isrc": ISRC, "recordings": [recording(), recording(OTHER)]},
    ]
    for index, response in enumerate(responses):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request, response=response: httpx.Response(
                    200,
                    json=recording() if "/recording/" in request.url.path else response,
                )
            )
        ) as client:
            with store.connect() as db:
                db.execute("DELETE FROM enrichment_cache")
            service = MusicBrainzService(store, online_settings(tmp_path), client)
            if index == 0:
                assert (await service.verify({"isrc": ISRC}))["recording_mbid"] == MBID
            elif index == 1:
                with pytest.raises(VerificationPending, match="not found"):
                    await service.verify({"isrc": ISRC})
            else:
                assert await service.verify({"isrc": ISRC}) == {"isrc": ISRC}
                assert (await service.verify({"isrc": ISRC, "recording_mbid": MBID}))[
                    "recording_mbid"
                ] == MBID


async def test_old_incomplete_cache_is_not_reused(tmp_path: Path) -> None:
    config = online_settings(tmp_path)
    store = Store(config.database, True)
    store.initialize()
    endpoint = hashlib.sha256(str(config.musicbrainz_base_url).rstrip("/").encode()).hexdigest()
    store.cache_enrichment(
        f"musicbrainz:v1:{endpoint}:isrc:{ISRC}",
        {"status": "resolved", "recordings": [recording()], "complete": False},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        result = await MusicBrainzService(store, config, client).verify({"isrc": ISRC})
    assert result["recording_mbid"] == MBID


async def test_failed_delete_does_not_log_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    config = online_settings(tmp_path)
    store = Store(config.database, True)
    store.initialize()
    payload = document()
    payload["metadata"] = {"duration": 12.0}
    store.submit("no-id", json.dumps(payload), 10)

    def fail_delete(identifier: str) -> bool:
        with store.connect() as db:
            db.execute("DELETE FROM submissions WHERE id=?", (identifier,))
            raise sqlite3.OperationalError("simulated commit failure")

    def fail_missing_identifier_delete(identifier: str) -> tuple[bool, bool, int]:
        return fail_delete(identifier), False, 0

    monkeypatch.setattr(
        store, "delete_missing_identifier_submission", fail_missing_identifier_delete
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
            await asyncio.wait_for(Worker(store, config, client).run(), 5)
    assert store.get_submission("no-id") is not None
    assert " deleted " not in caplog.text


async def test_manual_pass_preserves_unresolvable_and_retry_budget(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    config = online_settings(tmp_path)
    store = Store(config.database, True)
    store.initialize()
    pending(store, "ambiguous")
    pending(store, "no-id")
    payload = document()
    payload["metadata"] = {"duration": 12.0}
    with store.connect() as db:
        db.execute("UPDATE submissions SET pending_rechecks=2")
        db.execute("UPDATE submissions SET document=? WHERE id='no-id'", (json.dumps(payload),))
    store.request_pending_recheck()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as client:
        task = asyncio.create_task(Worker(store, config, client).run())
        try:
            async with asyncio.timeout(5):
                while "Manual pending metadata pass completed" not in caplog.text:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert store.take_pending_recheck() is None
    assert lifecycle_row(store, "ambiguous")["pending_rechecks"] == 2
    assert lifecycle_row(store, "no-id")["pending_rechecks"] == 2
    assert " deleted " not in caplog.text
