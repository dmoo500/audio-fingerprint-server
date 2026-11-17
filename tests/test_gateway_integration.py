"""Opt-in Docker tests of the real APISIX authentication boundary."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from audio_fingerprint import FORMAT_ID, SAMPLE_RATE, fingerprint
from pydantic import SecretStr
from test_server import audio, canonical, document

from fingerprint_server.gateway import configure, provision
from fingerprint_server.settings import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("FP_RUN_GATEWAY_TESTS") != "1",
    reason="Set FP_RUN_GATEWAY_TESTS=1 to build and run isolated Docker containers",
)
ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    """Choose a local port for the isolated test deployment."""
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return int(connection.getsockname()[1])


@pytest.fixture
def deployment(tmp_path: Path) -> Iterator[tuple[httpx.Client, httpx.Client, str, str]]:
    """Start a dedicated Compose project and remove only its test containers and volumes."""
    public_port, admin_port = free_port(), free_port()
    override = tmp_path / "ports.yaml"
    override.write_text(
        "services:\n  gateway:\n    ports: !override\n"
        f'      - "127.0.0.1:{public_port}:9080"\n'
        f'      - "127.0.0.1:{admin_port}:9180"\n'
    )
    env = dict(os.environ)
    env.update(
        {
            name: secrets.token_urlsafe(32)
            for name in [
                "FP_UPLOAD_KEY",
                "FP_READ_KEY",
                "FP_APPLICATION_KEY",
                "FP_GATEWAY_ADMIN_KEY",
            ]
        }
    )
    command = [
        "docker",
        "compose",
        "-p",
        f"fp-test-{uuid4().hex[:12]}",
        "-f",
        str(ROOT / "compose.gateway.yaml"),
        "-f",
        str(override),
    ]
    subprocess.run(["docker", "info"], check=True, capture_output=True, text=True, timeout=15)
    try:
        try:
            subprocess.run(
                [*command, "up", "--build", "-d"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.CalledProcessError as err:
            pytest.fail(f"Gateway deployment failed:\n{err.stderr}")
        with (
            httpx.Client(
                base_url=f"http://127.0.0.1:{admin_port}/apisix/admin/",
                headers={"X-API-KEY": env["FP_GATEWAY_ADMIN_KEY"]},
                timeout=5,
                trust_env=False,
            ) as admin,
            httpx.Client(
                base_url=f"http://127.0.0.1:{public_port}",
                timeout=30,
                trust_env=False,
                # Exercise route/personal budgets without exhausting the 5/s pre-auth IP guard.
                event_hooks={"request": [lambda _: time.sleep(0.25)]},
            ) as public,
        ):
            deadline = time.monotonic() + 90
            while True:
                try:
                    response = admin.get("routes")
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() >= deadline:
                    pytest.fail("APISIX Admin API did not become ready")
                time.sleep(1)
            configure(
                admin,
                Settings(
                    database=tmp_path / "unused.db",
                    upload_key=SecretStr(env["FP_UPLOAD_KEY"]),
                    read_key=SecretStr(env["FP_READ_KEY"]),
                    gateway_enabled=True,
                    application_key=SecretStr(env["FP_APPLICATION_KEY"]),
                ),
            )
            while public.get("/health").status_code != 200:
                if time.monotonic() >= deadline:
                    pytest.fail("Gateway/backend health route did not become ready")
                time.sleep(1)
            container = subprocess.run(
                [*command, "ps", "-q", "fingerprints"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            bindings = subprocess.run(
                ["docker", "inspect", container, "--format", "{{json .HostConfig.PortBindings}}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            assert not json.loads(bindings), "Backend must not publish host ports"
            yield public, admin, env["FP_APPLICATION_KEY"], env["FP_READ_KEY"]
    finally:
        subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans", "--rmi", "local"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )


def wait_for_status(client: httpx.Client, path: str, headers: dict[str, str], status: int) -> None:
    """Wait for an etcd configuration update to reach the running gateway."""
    deadline = time.monotonic() + 15
    while True:
        response = client.get(path, headers=headers)
        if response.status_code == status:
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"Expected HTTP {status}, got {response.status_code}: {response.text}")
        time.sleep(0.2)


def test_real_gateway_tokens_permissions_revocation_and_pcm(
    deployment: tuple[httpx.Client, httpx.Client, str, str],
) -> None:
    """The real gateway enforces both credentials and preserves recognition end to end."""
    public, admin, application, internal_read = deployment
    reader, uploader, both = (secrets.token_urlsafe(32) for _ in range(3))
    provision(admin, "reader", "read", reader)
    provision(admin, "uploader", "upload", uploader)
    provision(admin, "both", "both", both)
    headers = {"X-API-Key": both, "X-Application-Key": application}
    stats = "/api/v1/dejavu/statistics"
    wait_for_status(public, stats, headers, 200)
    for invalid in [
        {},
        {"X-API-Key": both},
        {"X-Application-Key": application},
        {"X-API-Key": internal_read, "X-Application-Key": application},
        {"X-API-Key": both, "X-Application-Key": "wrong"},
        {"X-API-Key": "wrong", "X-Application-Key": application, "X-Consumer-Username": "both"},
    ]:
        assert public.get(stats, headers=invalid).status_code == 401
    assert (
        public.get(
            stats, headers={"X-API-Key": uploader, "X-Application-Key": application}
        ).status_code
        == 403
    )
    assert (
        public.post(
            "/api/v1/dejavu/submissions",
            headers={"X-API-Key": reader, "X-Application-Key": application},
            content=b"{}",
        ).status_code
        == 403
    )
    assert public.get("/docs", headers=headers).status_code == 404
    assert public.post("/api/v1/dejavu/submissions/", headers=headers).status_code == 404
    samples = audio()
    body, identifier = canonical(document(samples))
    response = public.post(
        "/api/v1/dejavu/submissions",
        content=body,
        headers={**headers, "Content-Type": "application/json", "Idempotency-Key": identifier},
    )
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 15
    while True:
        response = public.get(f"/api/v1/dejavu/submissions/{identifier}", headers=headers)
        assert response.status_code == 200, response.text
        if response.json()["state"] == "processed":
            break
        if time.monotonic() >= deadline:
            pytest.fail(f"Submission was not processed: {response.text}")
        time.sleep(0.2)
    response = public.post(
        "/api/v1/dejavu/recognize?sample_rate=44100",
        content=samples[: 5 * 44100].tobytes(),
        headers={**headers, "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "matched"
    pcm_result = response.json()
    response = public.post(
        "/api/v1/dejavu/recognize/landmarks",
        json={
            "version": 1,
            "format_id": FORMAT_ID,
            "sample_rate": SAMPLE_RATE,
            "offset_unit": "fft_hops",
            "sample_count": 5 * SAMPLE_RATE,
            "fingerprints": fingerprint(samples[: 5 * SAMPLE_RATE]),
        },
        headers=headers,
    )
    assert response.status_code == 200 and response.json() == pcm_result
    admin.delete("consumers/both").raise_for_status()
    wait_for_status(public, stats, headers, 401)
    reader_headers = {"X-API-Key": reader, "X-Application-Key": application}
    assert public.get(stats, headers=reader_headers).status_code == 200
    admin.patch(
        "routes/statistics", json={"plugins": {"limit-count": {"count": 2}}}
    ).raise_for_status()
    time.sleep(2)
    assert public.get(stats, headers=reader_headers).status_code == 200
    assert public.get(stats, headers=reader_headers).status_code == 200
    assert public.get(stats, headers=reader_headers).status_code == 429
    other = secrets.token_urlsafe(32)
    provision(admin, "other-reader", "read", other)
    wait_for_status(public, stats, {"X-API-Key": other, "X-Application-Key": application}, 200)
    with httpx.Client(base_url=public.base_url, timeout=30, trust_env=False) as burst:
        statuses = [burst.get("/health").status_code for _ in range(20)]
    assert 429 in statuses
    assert set(statuses) <= {200, 429}
