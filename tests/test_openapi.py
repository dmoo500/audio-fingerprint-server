"""Published OpenAPI stays accurate without changing runtime responses."""

import json
from pathlib import Path

import httpx
from test_server import READ, UPLOAD, canonical, document, settings

from fingerprint_server.app import create_app
from fingerprint_server.openapi import (
    Capabilities,
    Error,
    Health,
    Recognition,
    Statistics,
    SubmissionReceipt,
    SubmissionStatus,
    build_schema,
)

ROOT = Path(__file__).resolve().parents[1]


def test_published_schema_matches_generated_contract() -> None:
    schema = build_schema(create_app())
    assert json.loads((ROOT / "openapi.json").read_text()) == schema
    assert len(schema["paths"]) == 7
    assert schema["paths"]["/health"]["get"]["security"] == []
    for path, methods in schema["paths"].items():
        if path != "/health":
            for operation in methods.values():
                assert operation["security"] == [{"PersonalKey": [], "ApplicationKey": []}]


def test_wire_body_and_response_variants() -> None:
    schema = build_schema(create_app())
    operations = schema["paths"]
    upload = operations["/api/v1/dejavu/submissions"]["post"]
    assert {"200", "202", "409", "422", "503"} <= upload["responses"].keys()
    assert len(upload["requestBody"]["content"]["application/json"]["schema"]["oneOf"]) == 2
    pcm = operations["/api/v1/dejavu/recognize"]["post"]
    assert pcm["requestBody"]["content"]["application/octet-stream"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
    assert len(pcm["responses"]["422"]["content"]["application/json"]["schema"]["oneOf"]) == 2
    assert "reason" not in schema["components"]["schemas"]["Recognition"]["required"]
    assert "match" in schema["components"]["schemas"]["Recognition"]["required"]


def test_all_component_references_resolve() -> None:
    schema = build_schema(create_app())

    def check(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$ref":
                    target = schema
                    for part in child.removeprefix("#/").split("/"):
                        target = target[part]
                else:
                    check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(schema)


async def test_runtime_openapi_legacy_needs_no_lifespan_or_credentials() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200
    operation = response.json()["paths"]["/api/v1/dejavu/statistics"]["get"]
    assert operation["security"] == [{"ScopeKey": []}]


async def test_declared_models_describe_actual_responses(tmp_path: Path) -> None:
    application = create_app(settings(tmp_path))
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.get("/health")
            Health.model_validate(response.json())
            response = await client.get("/api/v1/dejavu/statistics", headers={"X-API-Key": READ})
            Statistics.model_validate(response.json())
            response = await client.get(
                "/api/v1/dejavu/capabilities", headers={"X-API-Key": UPLOAD}
            )
            Capabilities.model_validate(response.json())
            body, identifier = canonical(document())
            response = await client.post(
                "/api/v1/dejavu/submissions",
                content=body,
                headers={
                    "X-API-Key": UPLOAD,
                    "Content-Type": "application/json",
                    "Idempotency-Key": identifier,
                },
            )
            SubmissionReceipt.model_validate(response.json())
            response = await client.get(
                f"/api/v1/dejavu/submissions/{identifier}", headers={"X-API-Key": UPLOAD}
            )
            SubmissionStatus.model_validate(response.json())
            response = await client.post(
                "/api/v1/dejavu/recognize?sample_rate=8000",
                content=b"\0" * (8000 * 3 * 4),
                headers={"X-API-Key": READ, "Content-Type": "application/octet-stream"},
            )
            Recognition.model_validate(response.json())
            assert response.json()["match"] is None
            response = await client.get("/api/v1/dejavu/statistics")
            assert response.status_code == 401
            Error.model_validate(response.json())


def test_runtime_gateway_requires_both_keys(tmp_path: Path) -> None:
    application = create_app(
        settings(
            tmp_path,
            gateway_enabled=True,
            application_key="test-application-key-at-least-24-characters",
        )
    )
    schema = application.openapi()
    assert schema["paths"]["/api/v1/dejavu/statistics"]["get"]["security"] == [
        {"PersonalKey": [], "ApplicationKey": []}
    ]
