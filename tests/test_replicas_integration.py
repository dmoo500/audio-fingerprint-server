"""Real APISIX balancing, failover and shared budgets with two isolated APIs."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from audio_fingerprint import FORMAT_ID, SAMPLE_RATE, fingerprint
from test_gateway_integration import free_port
from test_server import audio, canonical, document

from fingerprint_server.gateway import provision

pytestmark = pytest.mark.skipif(
    os.environ.get("FP_RUN_REPLICA_TESTS") != "1",
    reason="Set FP_RUN_REPLICA_TESTS=1 for the isolated two-API APISIX deployment",
)
ROOT = Path(__file__).resolve().parents[1]


class Deployment:
    """Operate only the uniquely named Compose project created by this fixture."""

    def __init__(self) -> None:
        self.project = f"fp-replicas-test-{uuid4().hex[:12]}"
        self.env = {
            key: value for key, value in os.environ.items() if not key.startswith(("FP_", "PG"))
        }
        self.env.update(
            {
                key: secrets.token_urlsafe(32)
                for key in (
                    "FP_UPLOAD_KEY",
                    "FP_READ_KEY",
                    "FP_APPLICATION_KEY",
                    "FP_GATEWAY_ADMIN_KEY",
                    "FP_POSTGRES_PASSWORD",
                )
            }
        )
        self.env.update(
            FP_REPLICA_PORT=str(free_port()),
            FP_REPLICA_ADMIN_PORT=str(free_port()),
            FP_REPLICA_IMAGE=f"{self.project}:local",
        )
        self.command = [
            "docker",
            "compose",
            "-p",
            self.project,
            "-f",
            str(ROOT / "compose.replicas.yaml"),
        ]

    def compose(self, *arguments: str, timeout: int = 120) -> str:
        result = subprocess.run(
            [*self.command, *arguments],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()

    def probe(self, client: httpx.Client, count: int = 12) -> None:
        """Stay below the unchanged pre-auth IP budget while exercising round-robin."""
        for _ in range(count):
            time.sleep(0.25)
            response = client.get("/health")
            assert response.status_code == 200, response.text

    def addresses(self) -> dict[str, int]:
        """Count upstream addresses from actual APISIX access logs."""
        time.sleep(3.2)  # APISIX's generated Nginx access log flush interval is three seconds.
        counts: Counter[str] = Counter()
        for line in self.compose("logs", "--no-log-prefix", "gateway").splitlines():
            if not line.startswith("{"):
                continue
            entry = json.loads(line)
            if entry.get("path") == "/health" and entry.get("status") == 200:
                counts[entry["upstream"]] += 1
        return dict(counts)


@pytest.fixture
def deployment() -> Iterator[tuple[Deployment, httpx.Client, httpx.Client]]:
    target = Deployment()
    try:
        target.compose("up", "--build", "-d", timeout=300)
        with (
            httpx.Client(
                base_url=f"http://127.0.0.1:{target.env['FP_REPLICA_ADMIN_PORT']}/apisix/admin/",
                headers={"X-API-KEY": target.env["FP_GATEWAY_ADMIN_KEY"]},
                timeout=10,
                trust_env=False,
            ) as admin,
            httpx.Client(
                base_url=f"http://127.0.0.1:{target.env['FP_REPLICA_PORT']}",
                timeout=30,
                trust_env=False,
            ) as public,
        ):
            deadline = time.monotonic() + 90
            while True:
                try:
                    if admin.get("routes").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "Isolated APISIX Admin API unavailable"
                time.sleep(0.5)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "fingerprint_server.gateway",
                    "--admin-port",
                    target.env["FP_REPLICA_ADMIN_PORT"],
                    "configure",
                ],
                env=target.env
                | {
                    "FP_GATEWAY_ENABLED": "true",
                    "FP_GATEWAY_BACKENDS": '["api-1:8097","api-2:8097"]',
                },
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            while public.get("/health").status_code != 200:
                assert time.monotonic() < deadline, "Isolated API/worker failed health check"
                time.sleep(0.5)
            for service in ("api-1", "api-2", "worker", "postgres"):
                container = target.compose("ps", "-q", service)
                bindings = subprocess.run(
                    [
                        "docker",
                        "inspect",
                        container,
                        "--format",
                        "{{json .HostConfig.PortBindings}}",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                assert not json.loads(bindings), f"{service} must not publish host ports"
            yield target, public, admin
    finally:
        try:
            target.compose("down", "--volumes", "--remove-orphans", "--rmi", "local")
        except subprocess.CalledProcessError as err:
            pytest.fail(f"Replica cleanup failed: {err.stderr}")


def test_balancing_limits_ingestion_failover_and_rejoin(deployment, record_property) -> None:
    target, public, admin = deployment
    personal = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    for index, key in enumerate(personal):
        provision(admin, f"trial-{index}", "both", key)
    headers = {"X-API-Key": personal[0], "X-Application-Key": target.env["FP_APPLICATION_KEY"]}
    time.sleep(2)
    target.probe(public)
    before = target.addresses()
    assert len(before) == 2, before
    assert min(before.values()) >= 4, before
    record_property("initial_upstream_requests", before)

    # Two distinct consumers do not hit their personal gateway budgets: this is
    # specifically the backend-wide shared 30/minute statistics budget.
    for index in range(31):
        time.sleep(0.25)
        response = public.get(
            "/api/v1/dejavu/statistics",
            headers={**headers, "X-API-Key": personal[index % 2]},
        )
        assert response.status_code == (200 if index < 30 else 429), (index, response.text)
    assert "Request rate limit reached" in response.text

    samples = audio()
    body, identifier = canonical(document(samples))
    time.sleep(0.25)
    response = public.post(
        "/api/v1/dejavu/submissions",
        content=body,
        headers={**headers, "Content-Type": "application/json", "Idempotency-Key": identifier},
    )
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 15
    while True:
        time.sleep(0.3)
        response = public.get(f"/api/v1/dejavu/submissions/{identifier}", headers=headers)
        assert response.status_code == 200, response.text
        if response.json()["state"] == "processed":
            break
        assert time.monotonic() < deadline, response.text

    # Hold both database-wide slots in an isolated child, then send a request
    # through the real gateway. No production-only testing endpoint is needed.
    holder_code = """
import sys
from contextlib import ExitStack
from fingerprint_server.settings import Settings
from fingerprint_server.postgres_store import PostgresStore
store = PostgresStore(Settings())
with ExitStack() as stack:
    assert stack.enter_context(store.recognition_slot())
    assert stack.enter_context(store.recognition_slot())
    print("HELD", flush=True)
    sys.stdin.readline()
"""
    holder = subprocess.Popen(
        [*target.command, "exec", "-T", "worker", "python", "-c", holder_code],
        env=target.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "HELD"
        for _ in range(2):
            time.sleep(0.25)
            response = public.post(
                "/api/v1/dejavu/recognize?sample_rate=44100",
                content=b"",
                headers={**headers, "Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 429
            assert "Recognition busy" in response.text
        time.sleep(0.25)
        response = public.post(
            "/api/v1/dejavu/recognize/landmarks",
            json={
                "version": 1,
                "format_id": FORMAT_ID,
                "sample_rate": SAMPLE_RATE,
                "offset_unit": "fft_hops",
                "sample_count": 5 * SAMPLE_RATE,
                "fingerprints": [],
            },
            headers=headers,
        )
        assert response.status_code == 429 and "Recognition busy" in response.text
    finally:
        holder.communicate("\n", timeout=15)
        assert holder.returncode == 0

    time.sleep(0.25)
    response = public.post(
        "/api/v1/dejavu/recognize?sample_rate=44100",
        content=samples[: 5 * 44100].tobytes(),
        headers={**headers, "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "matched", response.text
    assert response.json()["match"]["metadata"]["title"] == "Synthetic reference"
    pcm_result = response.json()
    time.sleep(0.25)
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

    target.compose("stop", "api-1")
    time.sleep(8)  # Active health checks converge; in-flight requests may fail before this.
    baseline = target.addresses()
    target.probe(public, 10)
    stopped = target.addresses()
    increments = {address: stopped[address] - baseline.get(address, 0) for address in stopped}
    assert sorted(increments.values()) == [0, 10], increments
    record_property("stopped_api_request_increments", increments)

    target.compose("start", "api-1")
    time.sleep(8)
    baseline = target.addresses()
    target.probe(public, 12)
    rejoined = target.addresses()
    increments = {address: rejoined[address] - baseline.get(address, 0) for address in rejoined}
    assert len(increments) == 2 and min(increments.values()) >= 4, increments
    record_property("rejoined_api_request_increments", increments)
    for service in ("api-1", "api-2"):
        assert "fingerprint-ingestion" not in target.compose("logs", service)
    target.compose("stop", "worker")
    time.sleep(8)
    assert public.get("/health").status_code != 200
    target.compose("start", "worker")
    deadline = time.monotonic() + 30
    while public.get("/health").status_code != 200:
        assert time.monotonic() < deadline, "Worker restart did not restore gateway readiness"
        time.sleep(0.5)
    time.sleep(0.25)
    response = public.get(f"/api/v1/dejavu/submissions/{identifier}", headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "processed"
