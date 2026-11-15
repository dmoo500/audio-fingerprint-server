"""Two-token backend authentication and gateway provisioning regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from test_server import READ, UPLOAD, audio, canonical, document, settings

from fingerprint_server.app import create_app
from fingerprint_server.async_utils import run_sync
from fingerprint_server.gateway import configure, provision
from fingerprint_server.schemas import Submission

APPLICATION = "test-fingerprint-application-key-with-24-characters"


@pytest.mark.parametrize(
    "options",
    [
        {"gateway_enabled": True},
        {"application_key": SecretStr(APPLICATION)},
        {"gateway_enabled": True, "application_key": SecretStr("short")},
        {"gateway_enabled": True, "application_key": SecretStr(READ)},
        {"gateway_enabled": True, "application_key": SecretStr(UPLOAD)},
    ],
)
def test_gateway_configuration_fails_closed(tmp_path: Path, options: dict[str, object]) -> None:
    """Invalid or partially enabled gateway settings cannot start the server."""
    with pytest.raises(ValidationError):
        settings(tmp_path, **options)


@pytest.mark.parametrize(
    ("method", "path", "internal"),
    [
        ("POST", "/api/v1/dejavu/submissions", UPLOAD),
        ("GET", "/api/v1/dejavu/submissions/unknown", UPLOAD),
        ("POST", "/api/v1/dejavu/recognize?sample_rate=44100", READ),
        ("GET", "/api/v1/dejavu/statistics", READ),
    ],
)
async def test_every_protected_route_requires_both_tokens(
    tmp_path: Path, method: str, path: str, internal: str
) -> None:
    """Neither credential alone nor forged consumer headers grants access."""
    application = create_app(
        settings(tmp_path, gateway_enabled=True, application_key=SecretStr(APPLICATION))
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            for headers in [
                {},
                {"X-API-Key": internal},
                {"X-Application-Key": APPLICATION},
                {"X-Application-Key": "wrong", "X-API-Key": internal},
                {"X-Application-Key": APPLICATION, "X-API-Key": "wrong"},
                {"X-Consumer-Username": "admin", "X-Credential-Identifier": "admin"},
            ]:
                response = await client.request(method, path, headers=headers, content=b"invalid")
                assert response.status_code == 401, response.text
            response = await client.request(
                method,
                path,
                headers={"X-Application-Key": APPLICATION, "X-API-Key": internal},
                content=b"invalid",
            )
            assert response.status_code in (200, 404, 415), response.text


async def test_two_token_upload_and_recognition(tmp_path: Path) -> None:
    """Gateway-mode headers preserve the existing fingerprint and PCM contracts."""
    application = create_app(
        settings(tmp_path, gateway_enabled=True, application_key=SecretStr(APPLICATION))
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            samples = audio()
            body, identifier = canonical(document(samples))
            response = await client.post(
                "/api/v1/dejavu/submissions",
                content=body,
                headers={
                    "X-Application-Key": APPLICATION,
                    "X-API-Key": UPLOAD,
                    "Content-Type": "application/json",
                    "Idempotency-Key": identifier,
                },
            )
            assert response.status_code == 202
            await run_sync(
                application.state.worker.publish,
                identifier,
                Submission.model_validate_json(body),
                document(samples)["metadata"],
            )
            response = await client.post(
                "/api/v1/dejavu/recognize?sample_rate=44100",
                content=samples[: 5 * 44100].tobytes(),
                headers={
                    "X-Application-Key": APPLICATION,
                    "X-API-Key": READ,
                    "Content-Type": "application/octet-stream",
                },
            )
            assert response.status_code == 200
            assert response.json()["status"] == "matched"
            page = await client.get("/")
            assert APPLICATION not in page.text
            assert 'id="application-key"' in page.text


def test_gateway_routes_enforce_personal_scope_and_preserve_application_header(
    tmp_path: Path,
) -> None:
    """Generated routes cannot forward personal credentials or bypass role restrictions."""
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with httpx.Client(
        transport=httpx.MockTransport(capture), base_url="http://localhost/apisix/admin/"
    ) as client:
        configure(
            client, settings(tmp_path, gateway_enabled=True, application_key=SecretStr(APPLICATION))
        )
    routes = {
        request.url.path.rsplit("/", 1)[1]: json.loads(request.content)
        for request in requests
        if "/routes/" in request.url.path
    }
    assert set(routes) == {"upload", "status", "recognize", "statistics", "public"}
    for name, scope, secret in [
        ("upload", "upload", UPLOAD),
        ("status", "upload", UPLOAD),
        ("recognize", "read", READ),
        ("statistics", "read", READ),
    ]:
        plugins = routes[name]["plugins"]
        assert plugins["key-auth"] == {"header": "X-API-Key", "query": "", "hide_credentials": True}
        assert plugins["consumer-restriction"]["whitelist"] == [scope, "both"]
        assert plugins["limit-count"]["key"] == "consumer_name"
        assert plugins["proxy-rewrite"]["headers"]["set"] == {"X-API-Key": secret}
        assert "X-Application-Key" not in plugins["proxy-rewrite"]["headers"]["remove"]
    assert not any("/consumers/" in request.url.path for request in requests)
    assert APPLICATION not in "".join(request.content.decode() for request in requests)


def test_provisioning_and_errors_are_explicit() -> None:
    """Provisioning validates identifiers and does not conceal Admin API errors."""
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with httpx.Client(
        transport=httpx.MockTransport(capture), base_url="http://localhost/apisix/admin/"
    ) as client:
        provision(client, "installation-1", "both", READ)
        assert json.loads(requests[0].content) == {
            "username": "installation-1",
            "group_id": "both",
            "plugins": {"key-auth": {"key": READ}},
        }
        for username, role, key in [
            ("../bad", "read", READ),
            ("ok", "admin", READ),
            ("ok", "read", "short"),
        ]:
            with pytest.raises(ValueError):
                provision(client, username, role, key)
        assert len(requests) == 1
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        base_url="http://localhost/apisix/admin/",
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            provision(client, "installation-1", "read", READ)
