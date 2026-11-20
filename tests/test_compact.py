"""Compact uploads and stored migrations preserve identity, coverage and recognition."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest
import test_server
from audio_fingerprint.codec import ENCODING_ID, decode_document, encode_document
from test_metadata_gate import document
from test_server import UPLOAD, canonical

import fingerprint_server.api as api_module
from fingerprint_server.aiohttp_support import HTTPError
from fingerprint_server.api import parse_submission
from fingerprint_server.database.sqlite import Store
from fingerprint_server.schemas import decode_submission

client = test_server.client


def test_compact_and_legacy_upload_share_identity() -> None:
    payload = document()
    body, identifier = canonical(payload)
    compact = json.dumps(encode_document(payload), sort_keys=True, separators=(",", ":")).encode()
    assert parse_submission(body, identifier)[0] == identifier
    assert parse_submission(compact, identifier)[0] == identifier
    assert decode_submission(compact) == decode_submission(body)
    with pytest.raises(HTTPError) as error:
        parse_submission(compact, hashlib.sha256(compact).hexdigest())
    assert error.value.status_code == 422


async def test_capabilities_are_upload_authenticated(client: httpx.AsyncClient) -> None:
    url = "/api/v1/dejavu/capabilities"
    assert (await client.get(url)).status_code == 401
    response = await client.get(url, headers={"X-API-Key": UPLOAD})
    assert response.status_code == 200
    assert response.json()["fingerprint_encodings"] == ["json", ENCODING_ID]


def test_existing_documents_are_compacted_without_changing_ids(tmp_path: Path) -> None:
    store = Store(tmp_path / "reference.db")
    store.initialize()
    payload = document()
    body, identifier = canonical(payload)
    store.submit(identifier, body.decode(), 10)
    store.finish(identifier, "reference", payload["metadata"], [("0" * 20, 0)])
    with store.connect() as db:
        db.execute("DROP INDEX references_recording")
        db.execute("ALTER TABLE reference_tracks DROP COLUMN recording_key")
        db.execute("ALTER TABLE reference_tracks DROP COLUMN verification_version")
        db.execute("DROP TABLE service_state")
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
        db.execute("PRAGMA user_version=2")
    store.initialize()
    with store.connect() as db:
        row = db.execute("SELECT id,document,reference_id,state FROM submissions").fetchone()
        assert row["id"] == identifier
        assert row["reference_id"] == "reference"
        assert row["state"] == "processed"
        assert json.loads(row["document"])["fingerprints_encoding"] == ENCODING_ID
        assert decode_document(json.loads(row["document"])) == payload
    assert store.statistics()["references"]["full"] == 1
    assert store.match([("0" * 20, 0)])[0]["reference_id"] == "reference"
    assert store.submit(identifier, body.decode(), 10)["state"] == "processed"
    store.initialize()


@pytest.mark.parametrize("mutation", ["encoding", "payload", "extra", "coverage"])
def test_invalid_compact_document_rejected(mutation: str) -> None:
    payload = encode_document(document())
    if mutation == "encoding":
        payload["fingerprints_encoding"] = "unknown"
    elif mutation == "payload":
        payload["fingerprints"] = "!"
    elif mutation == "extra":
        payload["audio_url"] = "https://invalid.example"
    else:
        payload["windows"][0]["start_sample"] = 4096
    with pytest.raises(HTTPError) as error:
        parse_submission(json.dumps(payload).encode(), "bad")
    assert error.value.status_code == 422


def test_decoded_document_size_boundary_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = document()
    payload["fingerprints"] = [[f"{number:020x}", 0] for number in range(100)]
    body, identifier = canonical(payload)
    compact = json.dumps(encode_document(payload), sort_keys=True, separators=(",", ":")).encode()
    assert len(compact) < len(body)
    monkeypatch.setattr(api_module, "MAX_DOCUMENT_BYTES", len(body))
    assert parse_submission(compact, identifier)[0] == identifier
    monkeypatch.setattr(api_module, "MAX_DOCUMENT_BYTES", len(body) - 1)
    with pytest.raises(HTTPError) as error:
        parse_submission(compact, identifier)
    assert error.value.status_code == 413


def test_compact_legacy_cannot_bypass_original_byte_limit() -> None:
    payload = document()
    payload["format_id"] = "ma-dejavu-landmarks-v1"
    payload["fingerprints"] = [[f"{number:020x}", 0] for number in range(80000)]
    body, identifier = canonical(payload)
    compact = json.dumps(encode_document(payload), sort_keys=True, separators=(",", ":")).encode()
    assert len(compact) < 2000000 < len(body)
    with pytest.raises(HTTPError) as error:
        parse_submission(compact, identifier)
    assert error.value.status_code == 413
