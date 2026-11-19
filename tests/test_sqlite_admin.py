"""Exercise SQLite administration against a real shared database."""

import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_admin import ORIGIN, configuration, create_admin, login

from fingerprint_server.admin.app import AdminSettings
from fingerprint_server.admin.errors import ActionFailed
from fingerprint_server.admin.sqlite_data import SQLiteAdminData
from fingerprint_server.database.application_keys import ApplicationKeys, initialize
from fingerprint_server.database.metrics import capture_hour
from fingerprint_server.database.sqlite import Store


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = AdminSettings.model_validate(
        configuration().model_dump()
        | {"database_backend": "sqlite", "database": tmp_path / "fingerprints.db"}
    )
    store = Store(settings.database)
    store.initialize()
    keys = ApplicationKeys(store)
    with keys.connection(write=True) as db:
        initialize(db)
    monkeypatch.setattr(
        httpx, "get", lambda *args, **kwargs: httpx.Response(200, json={"status": "ok"})
    )
    admin = SQLiteAdminData(settings, "http://fingerprints:8097/health")
    yield admin
    admin.close()


def insert_examples(data: SQLiteAdminData) -> None:
    with data.store.connect() as db:
        for index, state in enumerate(
            ("pending_metadata", "failed", "processed", "processing", "queued", "pending_metadata")
        ):
            metadata = {"title": f"Title {index}", "artists": ["Artist"], "album": "Album"}
            if index != 5:
                metadata["isrc"] = "USABC1234567"
            db.execute(
                "INSERT INTO submissions(id,state,document,created_at) VALUES(?,?,?,?)",
                (
                    str(index),
                    state,
                    json.dumps({"metadata": metadata, "fingerprints": ["secret"]}),
                    index + 1,
                ),
            )
        db.execute(
            "INSERT INTO reference_tracks(reference_key,metadata,created_at) VALUES('ref',?,1)",
            (json.dumps({"title": "Reference", "isrc": "USABC1234567"}),),
        )


def test_queries_actions_and_revisions(data: SQLiteAdminData) -> None:
    insert_examples(data)
    assert {row["id"] for row in data.read("unresolved")} == {"0", "1", "3", "4"}
    assert data.read("submissions", "Title", count=True) == 6
    assert data.read("submissions", "title-0", count=True) == 1
    assert data.read("submissions", {"artists": {"eq": "Artist"}}, count=True) == 6
    assert data.read("submissions", {"title": {"endswith": "0"}}, count=True) == 1
    with data.store.connect() as db:
        db.execute(
            "UPDATE submissions SET document=? WHERE id='5'",
            (json.dumps({"metadata": {"title": "Beyoncé", "artists": ["Artist"]}}),),
        )
    assert data.read("submissions", "BEYONCÉ", count=True) == 1
    assert data.read("submissions", pks=[], count=True) == 0
    assert data.read("submissions", pks=["0"])[0]["metadata"]["title"] == "Title 0"
    assert all("document" not in row for row in data.read("submissions"))
    assert len(data.read("submissions", skip=1, limit=2)) == 2
    assert data.read("references", count=True) == 1
    assert data.overview()["worker_available"]
    before = data.revisions()
    with data.store.connect() as db:
        db.execute(
            "UPDATE reference_tracks SET metadata=? WHERE reference_key='ref'",
            (json.dumps({"title": "Changed"}),),
        )
    assert data.revisions()["references"] != before["references"]
    for ids, action in ((["0", "3"], "delete"), (["2"], "retry"), (["4"], "delete")):
        with pytest.raises(ActionFailed):
            data.mutate(ids, action)
    data.mutate(["0", "1"], "retry")
    assert data.store.get_submission("0")["state"] == "queued"
    data.mutate(["2"], "delete")
    assert data.read("references", count=True) == 1
    with data.store.connect() as db:
        db.execute("UPDATE submissions SET state='failed' WHERE id='5'")
    data.settings = data.settings.model_copy(update={"queue_limit": 1})
    with pytest.raises(ActionFailed, match="Queue capacity"):
        data.mutate(["5"], "retry")
    assert data.store.get_submission("5")["state"] == "failed"
    for where in ({"evil": {"eq": "x"}}, {"id": {"unsupported": "x"}}, "x" * 201):
        with pytest.raises(ActionFailed):
            data.read("submissions", where)
    with pytest.raises(ActionFailed):
        data.read("submissions", order_by=["id;drop table submissions desc"])


def test_review_selection_is_atomic_and_state_guarded(data: SQLiteAdminData) -> None:
    insert_examples(data)
    with data.store.connect() as db:
        db.execute(
            "INSERT INTO musicbrainz_reviews(submission_id,candidates) VALUES(?,?)",
            ("0", json.dumps([{"id": "candidate-a"}, {"id": "candidate-b"}])),
        )
    assert data.musicbrainz_reviews()[0]["title"] == "Title 0"
    assert data.musicbrainz_reviews()[0]["metadata"]["artists"] == ["Artist"]
    with pytest.raises(ActionFailed):
        data.select_musicbrainz_candidate("0", "other")
    data.select_musicbrainz_candidate("0", "candidate-b")
    assert data.store.get_submission("0")["state"] == "queued"
    assert not data.musicbrainz_reviews()
    with pytest.raises(ActionFailed):
        data.select_musicbrainz_candidate("0", "candidate-a")


def test_mutation_serializes_against_worker(data: SQLiteAdminData) -> None:
    insert_examples(data)
    acquired = threading.Event()
    release = threading.Event()

    def worker() -> None:
        with data.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE submissions SET state='processing' WHERE id='1'")
            acquired.set()
            assert release.wait(5)

    thread = threading.Thread(target=worker)
    thread.start()
    assert acquired.wait(5)
    timer = threading.Timer(0.1, release.set)
    timer.start()
    try:
        with pytest.raises(ActionFailed, match="Active or queued"):
            data.mutate(["1"], "delete")
    finally:
        release.set()
        thread.join(timeout=5)
        timer.join()
    assert data.store.get_submission("1")["state"] == "processing"


def test_worker_health_and_missing_database(data: SQLiteAdminData, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(httpx, "get", Mock(side_effect=httpx.ConnectError("unavailable")))
    assert data.overview()["worker_available"] is False
    missing = SQLiteAdminData(
        data.settings.model_copy(update={"database": tmp_path / "missing.db"}),
        "http://fingerprints:8097/health",
    )
    with pytest.raises(sqlite3.OperationalError):
        missing.overview()
    assert not (tmp_path / "missing.db").exists()


def test_hourly_history(data: SQLiteAdminData) -> None:
    insert_examples(data)
    capture_hour(data.store, 1000)
    history = data.hourly_history(1000)
    assert len(history) == 1
    assert history[0]["queued"] == 1
    assert history[0]["processing"] == 1
    assert history[0]["pending_metadata"] == 2
    assert data.hourly_history(1720) == []


def test_full_admin_starts_with_sqlite(data: SQLiteAdminData) -> None:
    with TestClient(create_admin(data.settings)) as client:
        assert login(client).status_code == 200
        response = client.get("/admin/api/overview")
        assert response.status_code == 200
        assert response.json()["overview"]["database_bytes"] > 0
        assert client.get("/admin/api/applications").status_code == 200
        response = client.post(
            "/admin/api/applications",
            headers=ORIGIN,
            json={"operation": "issue", "name": "sqlite-test"},
        )
        assert response.status_code == 200
        assert response.json()["new_key"].startswith("fpapp_")
