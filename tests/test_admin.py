"""Verify framework authentication, metadata projections and guarded admin operations."""

import importlib
import json
import secrets
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_postgres_migration import postgres as postgres

pytest.importorskip("starlette_admin")
pytest.importorskip("psycopg")
admin = importlib.import_module("fingerprint_server.admin")
AdminSettings = admin.AdminSettings
create_admin = admin.create_admin
AdminData = importlib.import_module("fingerprint_server.admin.data").AdminData
ApplicationKeys = importlib.import_module(
    "fingerprint_server.database.application_keys"
).ApplicationKeys

ADMIN_KEY = "admin-only-" + "a" * 32
GATEWAY_KEY = "gateway-only-" + "b" * 32
ORIGIN = {"Origin": "http://testserver"}


def configuration(**kwargs) -> AdminSettings:
    return AdminSettings(
        upload_key="upload-" + "u" * 32,
        read_key="read-" + "r" * 32,
        admin_key=ADMIN_KEY,
        gateway_admin_key=GATEWAY_KEY,
        database_backend="postgres",
        gateway_enabled=True,
        application_key="legacy-app-" + "c" * 32,
        **kwargs,
    )


def login(client: TestClient) -> httpx.Response:
    return client.post(
        "/admin/login",
        data={"username": "admin", "password": ADMIN_KEY},
        headers=ORIGIN,
        follow_redirects=False,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        importlib.import_module("fingerprint_server.admin.app"),
        "validate_application_keys",
        lambda db: None,
    )
    from contextlib import contextmanager

    @contextmanager
    def connect(self, **kwargs):
        yield None

    monkeypatch.setattr("fingerprint_server.database.postgres.PostgresStore.connect", connect)
    overview = {
        "submissions": {"queued": 3, "processing": 2, "pending_metadata": 1},
        "manual_review": 1,
        "references": {"published": 1, "stored": 2},
        "database_bytes": 2048,
        "format_version": 1,
        "worker_available": True,
    }
    monkeypatch.setattr(AdminData, "overview", lambda self: overview)
    monkeypatch.setattr(AdminData, "hourly_history", lambda self, hour: [])
    monkeypatch.setattr(
        AdminData,
        "revisions",
        lambda self: {"submissions": [1, 0, 0, 0], "references": [2, 0, 0, 0]},
    )
    with TestClient(create_admin(configuration())) as client:
        yield client


def test_overview_shows_live_queue_and_persisted_history(client, monkeypatch):
    assert client.get("/admin/", follow_redirects=False).status_code == 303
    assert client.get("/admin/?view=history", follow_redirects=False).status_code == 303
    assert login(client).status_code == 303
    empty = client.get("/admin/")
    assert 'data-live-value="queue-queued">3</div>' in empty.text
    assert 'data-live-value="manual_review">1</div>' in empty.text
    assert "No hourly measurements yet" in empty.text
    assert 'id="history-reload"' in empty.text
    monkeypatch.setattr(
        AdminData,
        "hourly_history",
        lambda self, hour: [
            dict(
                hour=hour - 1,
                recordings=17,
                queued=4,
                processing=2,
                pending_metadata=1,
                manual_review=1,
            ),
            dict(
                hour=hour,
                recordings=18,
                queued=3,
                processing=2,
                pending_metadata=1,
                manual_review=1,
            ),
        ],
    )
    page = client.get("/admin/")
    assert page.status_code == 200
    assert 'aria-label="Searchable recordings over time"' in page.text
    assert 'aria-label="Submission backlog over time"' in page.text
    assert "Searchable recordings: <strong>18</strong>" in page.text
    refreshed = client.get("/admin/?view=history")
    assert refreshed.status_code == 200
    assert refreshed.headers["Cache-Control"] == "no-store"
    assert "Searchable recordings: <strong>18</strong>" in refreshed.text
    assert 'aria-label="Submission backlog over time"' in refreshed.text
    assert 'id="admin-live"' not in refreshed.text


def test_login_is_separate_and_sessions_are_revoked(client):
    assert client.get("/admin/events", follow_redirects=False).status_code == 303
    assert client.get("/admin/api/submissions", follow_redirects=False).status_code == 303
    response = client.post(
        "/admin/login", data={"username": "admin", "password": "r" * 32}, headers=ORIGIN
    )
    assert response.status_code == 400
    assert "Invalid administrator credentials" in response.text
    response = login(client)
    assert response.status_code == 303
    cookie = client.cookies.get("fp_admin")
    assert cookie and ADMIN_KEY not in cookie
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    overview = client.get("/admin/")
    assert overview.status_code == 200
    assert "Unresolved ISRC submissions" in overview.text
    assert 'data-live-value="database"' in overview.text
    assert "new EventSource(" in overview.text
    assert "no-store" in overview.headers["cache-control"]
    assert overview.headers["referrer-policy"] == "same-origin"
    client.get("/admin/logout")
    client.cookies.set("fp_admin", cookie)
    assert client.get("/admin/", follow_redirects=False).status_code == 303


def test_login_limits_csrf_and_action_methods(client):
    assert client.post("/admin/login", data={}).status_code == 403
    assert client.post("/admin/login", headers=ORIGIN, data={}).status_code == 400
    login(client)
    assert client.post("/admin/", headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert client.get("/admin/api/submissions/row-action?name=remove&pk=test").status_code == 405
    client.get("/admin/logout")
    for _ in range(10):
        response = client.post(
            "/admin/login", headers=ORIGIN, data={"username": "admin", "password": "wrong"}
        )
    assert "Too many login attempts" in response.text


def test_framework_tables_and_bounded_requests(client, monkeypatch):
    login(client)
    for identity in ("submissions", "unresolved", "references"):
        response = client.get(f"/admin/{identity}/list")
        assert response.status_code == 200
        assert "scrollY" in response.text
        assert 'id="live-reload"' in response.text
        assert 'data-dataset="' + identity + '"' in response.text
        assert client.get(f"/admin/{identity}/create").status_code == 403
    for query in ("limit=-1", "limit=101", "skip=-1", "skip=nope", "where=123"):
        response = client.get("/admin/api/submissions?" + query)
        assert response.status_code == 422
    rows = [
        {
            "id": "test",
            "state": "pending_metadata",
            "error": "<script>bad</script>",
            "title": "Title",
            "artists": '["Artist"]',
            "album": "Album",
            "isrc": "ABC",
            "recording_mbid": None,
            "created_at": 1,
            "attempts": 2,
            "pending_rechecks": 1,
            "reference_id": None,
            "metadata": {"title": "Title"},
        }
    ]
    monkeypatch.setattr(
        AdminData, "read", lambda self, *args, **kwargs: 1 if kwargs.get("count") else rows
    )
    response = client.get("/admin/api/submissions").json()
    assert response["total"] == 1
    assert response["items"][0]["title"] == "Title"
    assert "document" not in response["items"][0]
    assert "fingerprints" not in json.dumps(response)
    assert client.get("/admin/submissions/detail/test").status_code == 200


def test_pending_and_row_actions_use_existing_service_logic(client, monkeypatch):
    login(client)
    mutation = Mock()
    monkeypatch.setattr(AdminData, "mutate", mutation)
    pending = Mock()
    monkeypatch.setattr(client.app.state.data.store, "request_pending_recheck", pending)
    assert client.post("/admin/", headers=ORIGIN, follow_redirects=False).status_code == 303
    pending.assert_called_once()
    response = client.post("/admin/api/submissions/row-action?name=retry&pk=test", headers=ORIGIN)
    assert response.status_code == 200
    mutation.assert_called_once_with(["test"], "retry")
    response = client.post("/admin/api/references/row-action?name=retry&pk=test", headers=ORIGIN)
    assert response.status_code == 400
    assert mutation.call_count == 1


def test_musicbrainz_reviews_are_authenticated_and_select_through_admin_data(client, monkeypatch):
    from starlette_admin.exceptions import ActionFailed

    assert client.get("/admin/musicbrainz-reviews", follow_redirects=False).status_code == 303
    login(client)
    candidates = [
        {
            "id": "recording-a",
            "title": "Song",
            "artists": ["Artist"],
            "length": 226339,
            "disambiguation": "",
            "albums": ["Album", "Reissue"],
            "earliest_album": {"title": "Album", "date": "1985-06"},
        },
        {"id": "recording-b", "length": 3600000},
        {"id": "recording-c", "length": 59000},
        {"id": "recording-d", "length": None},
    ]
    monkeypatch.setattr(
        AdminData,
        "musicbrainz_reviews",
        lambda self, skip=0: [
            {
                "submission_id": "submission-a",
                "title": 'Song "Original"',
                "metadata": {"artists": ["Artist & Co", "Guest"]},
                "candidates": candidates,
            }
        ],
    )
    select = Mock()
    monkeypatch.setattr(AdminData, "select_musicbrainz_candidate", select)
    response = client.get("/admin/musicbrainz-reviews")
    assert response.status_code == 200
    assert "Song" in response.text and "recording-a" in response.text
    assert 'value="Song &#34;Original&#34; - Artist &amp; Co, Guest"' in response.text
    assert 'value="Song - Artist"' not in response.text
    assert response.text.count('aria-label="Copy title and artist"') == 1
    assert "navigator.clipboard.writeText(input.value)" in response.text
    assert "Recording MBID: <code>recording-a</code>" in response.text
    assert "Length: 03:46</span>" in response.text
    assert "Earliest known official album:" in response.text
    assert "(1985-06)" in response.text
    assert "<summary>Show album credits</summary>" in response.text
    assert "Album, Reissue" in response.text
    assert response.text.count("Earliest known official album:") == 1
    assert " checked" not in response.text
    assert "Length: 01:00:00</span>" in response.text
    assert "Length: 00:59</span>" in response.text
    assert response.text.count("Length:") == 3
    assert 'name="selected_mbid"' in response.text
    assert client.post("/admin/musicbrainz-reviews", data={}).status_code == 403
    response = client.post(
        "/admin/musicbrainz-reviews",
        headers=ORIGIN,
        data={"submission_id": "submission-a", "selected_mbid": "recording-a"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/musicbrainz-reviews?selected=1")
    select.assert_called_once_with("submission-a", "recording-a")
    select.side_effect = ActionFailed("Select one of the listed MusicBrainz candidates")
    response = client.post(
        "/admin/musicbrainz-reviews",
        headers=ORIGIN,
        data={"submission_id": "submission-a", "selected_mbid": "not-listed"},
    )
    assert response.status_code == 400
    assert "Select one of the listed MusicBrainz candidates" in response.text


def test_musicbrainz_selection_locks_and_validates_before_updates(monkeypatch):
    from contextlib import contextmanager

    from starlette_admin.exceptions import ActionFailed

    data = AdminData(configuration())
    db = Mock()
    submission_result = Mock()
    submission_result.fetchone.return_value = {"id": "submission-a", "state": "pending_metadata"}
    review_result = Mock()
    review_result.fetchone.return_value = {
        "candidates": json.dumps([{"id": "recording-a"}]),
        "selected_mbid": None,
    }

    @contextmanager
    def connect(*, write=False):
        assert write
        yield db

    monkeypatch.setattr(data.store, "connect", connect)
    db.execute.side_effect = [submission_result, review_result, Mock(), Mock()]
    data.select_musicbrainz_candidate("submission-a", "recording-a")
    calls = db.execute.call_args_list
    assert len(calls) == 4
    assert "FROM submissions WHERE id=%s FOR UPDATE" in calls[0].args[0]
    assert "FROM musicbrainz_reviews" in calls[1].args[0]
    assert calls[2].args[1] == ("recording-a", "submission-a")
    assert calls[3].args[1][1] == "submission-a"

    db.execute.reset_mock()
    db.execute.side_effect = [submission_result, review_result]
    with pytest.raises(ActionFailed, match="listed"):
        data.select_musicbrainz_candidate("submission-a", "not-listed")
    assert db.execute.call_count == 2

    db.execute.reset_mock()
    submission_result.fetchone.return_value = {"id": "submission-a", "state": "failed"}
    db.execute.side_effect = [submission_result]
    with pytest.raises(ActionFailed, match="no longer awaiting"):
        data.select_musicbrainz_candidate("submission-a", "recording-a")
    assert db.execute.call_count == 1


def test_application_key_ui_requires_confirmation_and_never_redisplays_secrets(client, monkeypatch):
    login(client)
    entries = []
    secret = secrets.token_urlsafe(32)
    identifier = "a" * 32
    monkeypatch.setattr(ApplicationKeys, "list", lambda self: entries)

    def issue(self, name):
        entries.append({"id": identifier, "name": name, "created_at": 1, "revoked_at": None})
        return identifier, secret

    monkeypatch.setattr(ApplicationKeys, "issue", issue)
    revoke = Mock()
    monkeypatch.setattr(ApplicationKeys, "revoke", revoke)
    response = client.get("/admin/applications")
    assert response.status_code == 200
    assert "Legacy environment application key: configured" in response.text
    assert client.app.state.data.settings.application_key.get_secret_value() not in response.text
    response = client.post(
        "/admin/applications", headers=ORIGIN, data={"operation": "issue", "name": "my-client"}
    )
    assert response.status_code == 200
    assert secret in response.text
    assert "no-store" in response.headers["cache-control"]
    assert secret not in client.get("/admin/applications").text
    response = client.post(
        "/admin/applications", headers=ORIGIN, data={"operation": "revoke", "id": identifier}
    )
    assert response.status_code == 400
    revoke.assert_not_called()
    response = client.post(
        "/admin/applications",
        headers=ORIGIN,
        data={"operation": "revoke", "id": identifier, "confirmed": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    revoke.assert_called_once_with(identifier)
    assert client.post("/admin/applications", data={}).status_code == 403
    client.get("/admin/logout")
    assert client.get("/admin/applications", follow_redirects=False).status_code == 303


def test_key_values_redacted_and_creation_does_not_replace_named_consumer(client):
    login(client)
    view = next(view for view in client.app.state.admin._views if view.name == "keys")
    calls = []
    consumers = {
        "existing": {
            "username": "existing",
            "group_id": "read",
            "plugins": {"key-auth": {"key": "hidden-original-secret"}},
        }
    }

    def handle(request):
        calls.append(request)
        username = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and username == "consumers":
            return httpx.Response(200, json={"list": [{"value": v} for v in consumers.values()]})
        if request.method == "PUT":
            consumers[username] = json.loads(request.content)
            return httpx.Response(201, json={"value": consumers[username]})
        if request.method == "GET":
            return httpx.Response(200, json={"value": consumers[username]})
        del consumers[username]
        return httpx.Response(200, json={"deleted": 1})

    original = view.client
    view.client = httpx.AsyncClient(
        base_url="http://gateway/apisix/admin/", transport=httpx.MockTransport(handle)
    )
    try:
        response = client.get("/admin/keys")
        assert response.status_code == 200
        assert "hidden-original-secret" not in response.text
        response = client.post(
            "/admin/keys",
            headers=ORIGIN,
            data={"operation": "issue", "username": "existing", "role": "upload"},
        )
        assert response.status_code == 200
        issued = next(name for name in consumers if name != "existing")
        assert issued.startswith("existing-")
        key = consumers[issued]["plugins"]["key-auth"]["key"]
        assert key in response.text
        assert key not in client.get("/admin/keys").text
        assert consumers["existing"]["plugins"]["key-auth"]["key"] == "hidden-original-secret"
        response = client.post(
            "/admin/keys", headers=ORIGIN, data={"operation": "revoke", "username": issued}
        )
        assert response.status_code == 400
        response = client.post(
            "/admin/keys",
            headers=ORIGIN,
            data={"operation": "revoke", "username": issued, "confirmed": "yes"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert issued not in consumers
    finally:
        client.portal.call(view.client.aclose)
        view.client = original


def test_admin_settings_reject_shared_secret():
    with pytest.raises(ValidationError, match="separate admin"):
        configuration().model_validate(configuration().model_dump() | {"admin_key": GATEWAY_KEY})
    values = configuration().model_dump() | {
        "application_key": "application-" + "c" * 32,
        "gateway_enabled": True,
    }
    assert AdminSettings.model_validate(values).gateway_enabled


def test_actual_postgres_admin_queries_and_atomic_actions(postgres, tmp_path: Path):
    from uuid import uuid4

    from starlette_admin.exceptions import ActionFailed

    config = configuration(postgres_schema="admin_" + uuid4().hex)
    data = AdminData(config)
    data.store.initialize()
    with data.store.connect(write=True) as db:
        for index, state in enumerate(
            ("pending_metadata", "failed", "processed", "processing", "queued", "pending_metadata")
        ):
            metadata = {"title": f"Title {index}", "artists": ["Artist"], "album": "Album"}
            if index != 5:
                metadata["isrc"] = "USABC1234567"
            db.execute(
                "INSERT INTO submissions(id,state,document,created_at) VALUES(%s,%s,%s,%s)",
                (
                    str(index),
                    state,
                    json.dumps({"metadata": metadata, "fingerprints": ["secret"]}),
                    index + 1,
                ),
            )
        db.execute(
            "INSERT INTO reference_tracks(reference_key,metadata,created_at) VALUES('ref',%s,1)",
            (json.dumps({"title": "Reference", "isrc": "USABC1234567"}),),
        )
    rows = data.read("unresolved")
    assert isinstance(rows, list)
    assert {row["id"] for row in rows} == {"0", "1", "3", "4"}
    assert all("document" not in row for row in rows)
    assert data.read("submissions", "Title", count=True) == 6
    assert data.read("submissions", {"state": {"eq": "failed"}}, count=True) == 1
    assert data.read("submissions", {"artists": {"eq": "Artist"}}, count=True) == 6
    assert (
        data.read(
            "submissions",
            {"and": [{"state": {"neq": "processed"}}, {"isrc": {"contains": "USABC"}}]},
            count=True,
        )
        == 4
    )
    assert len(data.read("submissions", skip=1, limit=2)) == 2
    assert data.read("submissions", pks=["0"])[0]["metadata"]["title"] == "Title 0"
    assert data.read("references", count=True) == 1
    assert data.overview()["format_version"] == 1
    for ids, operation in ((["0", "3"], "delete"), (["2"], "retry"), (["4"], "delete")):
        with pytest.raises(ActionFailed):
            data.mutate(ids, operation)
    assert data.read("submissions", pks=["0"], count=True) == 1
    with data.store.connect(write=True) as db:
        db.execute("UPDATE submissions SET pending_rechecks=2,attempts=8 WHERE id='0'")
    data.mutate(["0", "1"], "retry")
    row = data.store.get_submission("0")
    assert row["state"] == "queued" and row["attempts"] == 0
    assert data.read("submissions", pks=["0"])[0]["pending_rechecks"] == 2
    data.mutate(["2"], "delete")
    assert data.read("references", count=True) == 1
    for where in ({"evil": {"eq": "x"}}, {"id": {"unsupported": "x"}}, "x" * 201):
        with pytest.raises(ActionFailed):
            data.read("submissions", where)
    with pytest.raises(ActionFailed):
        data.read("submissions", order_by=["id;drop table submissions desc"])
    with data.store.connect(write=True) as db:
        db.execute("UPDATE submissions SET state='failed' WHERE id='5'")
    data.settings = config.model_copy(update={"queue_limit": 1})
    with pytest.raises(ActionFailed, match="Queue capacity"):
        data.mutate(["5"], "retry")
    assert data.store.get_submission("5")["state"] == "failed"
    before = data.revisions()
    with data.store.connect(write=True) as db:
        db.execute(
            "UPDATE reference_tracks SET metadata=%s WHERE reference_key='ref'",
            (json.dumps({"title": "Changed without changing row count"}),),
        )
        db.execute("SELECT pg_stat_force_next_flush()")
    deadline = time.monotonic() + 5
    while (after := data.revisions())["references"] == before["references"]:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert after["submissions"] == before["submissions"]


def test_postgres_musicbrainz_review_selection_is_membership_and_state_guarded(postgres):
    from uuid import uuid4

    from starlette_admin.exceptions import ActionFailed

    data = AdminData(configuration(postgres_schema="mb_review_" + uuid4().hex))
    data.store.initialize()
    candidates = [
        {
            "id": "recording-a",
            "title": "Song",
            "artists": ["Artist"],
            "length": 180000,
            "disambiguation": "",
            "albums": ["Album"],
        },
        {
            "id": "recording-b",
            "title": "Song (Live)",
            "artists": ["Artist"],
            "length": 190000,
            "disambiguation": "live",
            "albums": ["Live Album"],
        },
    ]
    with data.store.connect(write=True) as db:
        for submission_id, state in (("pending", "pending_metadata"), ("failed", "failed")):
            db.execute(
                "INSERT INTO submissions(id,state,document,created_at,attempts,error) "
                "VALUES(%s,%s,%s,1,7,'Waiting for review')",
                (submission_id, state, json.dumps({"metadata": {"title": "Song"}})),
            )
            db.execute(
                "INSERT INTO musicbrainz_reviews(submission_id,candidates) VALUES(%s,%s)",
                (submission_id, json.dumps(candidates)),
            )
    reviews = data.musicbrainz_reviews()
    assert [row["submission_id"] for row in reviews] == ["pending"]
    assert reviews[0]["title"] == "Song"
    assert reviews[0]["candidates"] == candidates
    with pytest.raises(ActionFailed, match="listed"):
        data.select_musicbrainz_candidate("pending", "not-listed")
    with pytest.raises(ActionFailed, match="no longer awaiting"):
        data.select_musicbrainz_candidate("failed", "recording-a")
    assert data.store.get_submission("pending")["state"] == "pending_metadata"
    data.select_musicbrainz_candidate("pending", "recording-b")
    submission = data.store.get_submission("pending")
    assert submission["state"] == "queued"
    assert submission["attempts"] == 0
    assert submission["error"] is None
    with data.store.connect() as db:
        internal = db.execute(
            "SELECT retry_at,pending_check,background_kind FROM submissions WHERE id='pending'"
        ).fetchone()
        assert internal == {"retry_at": 0, "pending_check": 0, "background_kind": 0}
        review = db.execute(
            "SELECT selected_mbid FROM musicbrainz_reviews WHERE submission_id='pending'"
        ).fetchone()
    assert review["selected_mbid"] == "recording-b"
    assert data.musicbrainz_reviews() == []


def test_postgres_search_normalization_and_numeric_column_sort(postgres):
    from uuid import uuid4

    data = AdminData(configuration(postgres_schema="search_" + uuid4().hex))
    data.store.initialize()
    with data.store.connect(write=True) as db:
        for index, (artist, attempts) in enumerate((("AC/DC", 10), ("Other", 2), ("AC DC", 1))):
            db.execute(
                "INSERT INTO submissions(id,state,document,created_at,attempts,pending_rechecks) "
                "VALUES(%s,'pending_metadata',%s,%s,%s,%s)",
                (
                    str(index),
                    json.dumps(
                        {"metadata": {"artists": [artist], "title": "Song", "isrc": "USABC1234567"}}
                    ),
                    index,
                    attempts,
                    attempts,
                ),
            )
            db.execute(
                "INSERT INTO reference_tracks(reference_key,metadata,created_at,"
                "verification_version,publication_ready) VALUES(%s,%s,%s,%s,%s)",
                (str(index), json.dumps({"artists": [artist]}), index, attempts, index % 2),
            )
    for dataset in ("submissions", "unresolved", "references"):
        assert data.read(dataset, "aCdC", count=True) == 2
        assert len(data.read(dataset, "ACDC", limit=1)) == 1
        assert data.read(dataset, "/", count=True) == 1
        assert data.read(dataset, "does-not-exist", count=True) == 0
        field = "verification_version" if dataset == "references" else "attempts"
        assert [row["id"] for row in data.read(dataset, order_by=[field + " asc"])] == [
            "2",
            "1",
            "0",
        ]
        assert [row["id"] for row in data.read(dataset, order_by=[field + " desc"])] == [
            "0",
            "1",
            "2",
        ]
    assert data.read("submissions", {"artists": {"contains": "ACDC"}}, count=True) == 0
    assert data.read("submissions", "'; DROP TABLE submissions;--", count=True) == 0
