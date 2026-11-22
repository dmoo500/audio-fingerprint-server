"""Public recognition facade boundaries, privacy and admission tests."""

import asyncio
from collections.abc import AsyncIterator
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiohttp_client import AsyncClient, TestClient
from pydantic import SecretStr, ValidationError

from fingerprint_server.aiohttp_support import HTTPError, trusted_proxy
from fingerprint_server.recognition.app import Admission, RecognitionSettings, create_app

ORIGIN = "https://listen.example"
HEADERS = {
    "Origin": ORIGIN,
    "Content-Type": "application/octet-stream",
    "X-Recognition-Request": "microphone",
}
PCM = b"\0" * (8000 * 4 * 3)


@pytest.mark.parametrize(
    ("upstream_status", "body", "expected"),
    [(200, {"status": "ok"}, True), (503, {}, False), (200, {"status": "wrong"}, False)],
)
def test_availability_reports_and_caches_upstream_health(
    upstream_status: int, body: dict[str, str], expected: bool
) -> None:
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/health"
        return httpx.Response(upstream_status, json=body)

    app = create_app(settings(), transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/availability").json() == {"available": expected}
        assert client.get("/api/availability").json() == {"available": expected}
    assert len(calls) == 1


def settings(**overrides: object) -> RecognitionSettings:
    """Create isolated configuration with non-production test identities."""
    return RecognitionSettings.model_validate(
        {
            "public_url": ORIGIN,
            "api_url": "http://fingerprints.internal",
            "read_key": SecretStr("r" * 32),
            "application_key": SecretStr("a" * 32),
            **overrides,
        }
    )


def test_search_crawlers_are_disallowed() -> None:
    with TestClient(create_app(settings()), base_url=ORIGIN) as client:
        response = client.get("/robots.txt")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text == "User-agent: *\nDisallow: /\n"
        assert response.headers["x-robots-tag"] == "noindex, nofollow"
        assert client.portal.call(client.client.head, "/robots.txt").status_code == 200
        assert client.get("/").headers["x-robots-tag"] == "noindex, nofollow"


def test_missing_frontend_build_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make an incomplete source or package installation actionable."""
    module = import_module("fingerprint_server.recognition.app")
    monkeypatch.setattr(module, "FRONTEND", tmp_path)
    with pytest.raises(RuntimeError, match="frontend assets are missing"):
        with TestClient(create_app(settings())):
            pass


def matched() -> dict[str, object]:
    """Include internal fields that must never appear in the public response."""
    return {
        "status": "matched",
        "match": {
            "reference_id": "private-reference",
            "matched_landmarks": 100,
            "metadata": {
                "title": "Example song",
                "artists": ["Example artist"],
                "album": "Example album",
                "private": "retained metadata",
            },
        },
    }


def test_facade_keeps_credentials_and_internal_metadata_on_server() -> None:
    """Forward only configured read credentials and expose an explicit public projection."""
    requests = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-API-Key"] == "r" * 32
        assert request.headers["X-Application-Key"] == "a" * 32
        assert "Cookie" not in request.headers
        if request.url.path.endswith("statistics"):
            return httpx.Response(
                200,
                json={
                    "recordings": {"total": 42, "excluded": 9},
                    "submissions": {"failed": 7},
                    "worker_available": True,
                    "references": {"total": 100},
                },
            )
        assert dict(request.url.params) == {"sample_rate": "8000", "channels": "1"}
        assert request.content == PCM
        return httpx.Response(200, json=matched())

    app = create_app(settings(), transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post(
            "/api/recognize?sample_rate=8000",
            content=PCM,
            headers={
                **HEADERS,
                "X-API-Key": "caller-cannot-replace-server-identity",
                "Cookie": "x=y",
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "matched",
            "match": {
                "title": "Example song",
                "artists": ["Example artist"],
                "album": "Example album",
            },
        }
        assert client.get("/api/statistics").json() == {"recordings": 42}
        assert client.get("/api/statistics").json() == {"recordings": 42}
        assert len(requests) == 2
        for path in (
            "/",
            "/assets/app.js",
            "/assets/index.css",
            "/assets/recording.js",
            "/assets/capture.js",
        ):
            page = client.get(path)
            assert page.status_code == 200
            assert "r" * 32 not in page.text
            assert "a" * 32 not in page.text
            assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["permissions-policy"].startswith("microphone=(self)")
        for path in ("/admin", "/api/v1/dejavu/submissions", "/assets/../app.py", "/openapi.json"):
            assert client.get(path).status_code == 404


@pytest.mark.parametrize("address", ["http://localhost:8100", "http://127.0.0.1:8100"])
def test_page_redirects_to_configured_origin_before_microphone_capture(address: str) -> None:
    """Alternative hostnames must not serve a page whose recognition POST will be rejected."""
    app = create_app(settings(public_url="http://127.0.0.1:8100"))
    with TestClient(app, base_url=address, follow_redirects=False) as client:
        response = client.get("/")
        if address == "http://localhost:8100":
            assert response.status_code == 307
            assert response.headers["location"] == "http://127.0.0.1:8100/"
        else:
            assert response.status_code == 200
            assert '<div id="app"></div>' in response.text
            assert 'src="/assets/app.js"' in response.text


@pytest.mark.parametrize(("proxy", "status"), [("127.0.0.1", 200), ("192.0.2.1", 307)])
async def test_public_origin_behind_tls_proxy(proxy: str, status: int) -> None:
    """Serve the HTTPS origin over HTTP only with scheme information from the chosen peer."""
    app = create_app(settings())
    app.middlewares.append(trusted_proxy(proxy))
    async with TestServer(app) as listener, httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(
            str(listener.make_url("/")),
            headers={
                "Host": "listen.example",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "192.0.2.10",
            },
        )
    assert response.status_code == status


@pytest.mark.parametrize("outcome", ["no_match", "ambiguous"])
def test_unmatched_outcomes_remain_distinct(outcome: str) -> None:
    """Do not invent a successful match for ambiguous or absent evidence."""
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"status": outcome, "match": None})
    )
    with TestClient(create_app(settings(), transport=transport)) as client:
        response = client.post("/api/recognize?sample_rate=8000", headers=HEADERS, content=PCM)
        assert response.status_code == 200
        assert response.json() == {"status": outcome, "match": None}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {**HEADERS, "Origin": "https://stranger.example"},
        {key: value for key, value in HEADERS.items() if key != "X-Recognition-Request"},
    ],
)
def test_other_origins_cannot_spend_recognition_credentials(headers: dict[str, str]) -> None:
    """Reject browser cross-origin and missing-marker requests before forwarding."""
    with TestClient(create_app(settings())) as client:
        response = client.post("/api/recognize?sample_rate=8000", headers=headers, content=PCM)
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/recognize", 422),
        ("/api/recognize?sample_rate=invalid", 422),
        ("/api/recognize?sample_rate=7999", 422),
        ("/api/recognize?sample_rate=8000", 403),
        ("/unknown", 404),
    ],
)
def test_security_headers_apply_to_errors(path: str, status: int) -> None:
    """Keep browser protections on validation, authorization, and routing errors."""
    with TestClient(create_app(settings())) as client:
        response = client.post(path)
        assert response.status_code == status
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["permissions-policy"].startswith("microphone=(self)")
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("pcm", "rate", "content_type", "expected"),
    [
        (b"", 8000, "application/octet-stream", 422),
        (PCM + b"x", 8000, "application/octet-stream", 422),
        (b"\0" * (15 * 8000 * 4 + 4), 8000, "application/octet-stream", 413),
        (PCM, 7999, "application/octet-stream", 422),
        (PCM, 192001, "application/octet-stream", 422),
        (PCM, 8000, "text/plain", 415),
    ],
)
def test_invalid_audio_is_rejected_before_upstream(
    pcm: bytes, rate: int, content_type: str, expected: int
) -> None:
    """Check exact duration, alignment, byte and sample-rate boundaries."""

    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid audio reached the fingerprint API")

    with TestClient(create_app(settings(), transport=httpx.MockTransport(unexpected))) as client:
        response = client.post(
            f"/api/recognize?sample_rate={rate}",
            content=pcm,
            headers={**HEADERS, "Content-Type": content_type},
        )
        assert response.status_code == expected


async def test_streamed_audio_is_bounded_without_content_length() -> None:
    """Reject oversized chunked recordings before they reach the upstream service."""

    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("Oversized audio reached the fingerprint API")

    async def chunks() -> AsyncIterator[bytes]:
        yield b"\0" * (15 * 8000 * 4)
        yield b"\0" * 4

    app = create_app(settings(), transport=httpx.MockTransport(unexpected))
    async with AsyncClient(app, base_url=ORIGIN) as client:
        response = await client.post(
            "/api/recognize?sample_rate=8000", content=chunks(), headers=HEADERS
        )
        assert response.status_code == 413
        assert response.json() == {"detail": "Request exceeds the size limit"}
        assert response.headers["cache-control"] == "no-store"


def test_per_address_limit_ignores_spoofed_forwarded_header() -> None:
    """A caller cannot rotate its identity using an untrusted forwarding header."""
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"status": "no_match", "match": None})
    )
    with TestClient(create_app(settings(requests_per_minute=1), transport=transport)) as client:
        assert (
            client.post("/api/recognize?sample_rate=8000", headers=HEADERS, content=PCM).status_code
            == 200
        )
        limited = client.post(
            "/api/recognize?sample_rate=8000",
            headers={**HEADERS, "X-Forwarded-For": "1.2.3.4"},
            content=PCM,
        )
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"


@pytest.mark.parametrize(
    ("code", "expected"), [(401, 503), (403, 503), (429, 429), (500, 503), (422, 422)]
)
def test_upstream_errors_are_explicit_and_never_leak_details(code: int, expected: int) -> None:
    """Keep outages distinct from no-match without forwarding backend messages."""
    transport = httpx.MockTransport(lambda _: httpx.Response(code, text="internal secret details"))
    with TestClient(create_app(settings(), transport=transport)) as client:
        response = client.post("/api/recognize?sample_rate=8000", headers=HEADERS, content=PCM)
        assert response.status_code == expected
        assert "internal secret details" not in response.text


@pytest.mark.parametrize(
    "data",
    [
        b"not-json",
        b'{"status":"matched","match":null}',
        b'{"status":"unknown","match":null}',
        b'{"status":"no_match","match":{"metadata":{}}}',
        b"x" * 64_001,
    ],
)
def test_invalid_upstream_results_are_not_success_shaped(data: bytes) -> None:
    """Reject malformed or oversized responses instead of showing an empty song."""
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=data))
    with TestClient(create_app(settings(), transport=transport)) as client:
        assert (
            client.post("/api/recognize?sample_rate=8000", headers=HEADERS, content=PCM).status_code
            == 502
        )


def test_count_failure_is_explicit_and_backed_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed or expired counters are never shown as current, and retries are bounded."""
    module = import_module("fingerprint_server.recognition.api")
    clock = SimpleNamespace(monotonic=lambda: 0.0)
    monkeypatch.setattr(module, "time", clock)
    calls = []

    def upstream(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"recordings": {"total": -1}})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(upstream))) as client:
        assert client.get("/api/statistics").status_code == 502
        assert client.get("/api/statistics").status_code == 503
        assert len(calls) == 1
        clock.monotonic = lambda: 6.0
        assert client.get("/api/statistics").status_code == 502
        assert len(calls) == 2


async def test_global_concurrency_is_bounded_and_released() -> None:
    """Reject a third in-flight recording even when its client address differs."""
    started = asyncio.Queue[None]()
    finish = asyncio.Event()

    async def upstream(_: httpx.Request) -> httpx.Response:
        await started.put(None)
        await finish.wait()
        return httpx.Response(200, json={"status": "no_match", "match": None})

    app = create_app(settings(), transport=httpx.MockTransport(upstream))
    async with AsyncClient(app, base_url=ORIGIN) as client:
        tasks = [
            asyncio.create_task(
                client.post("/api/recognize?sample_rate=8000", headers=HEADERS, content=PCM)
            )
            for _ in range(2)
        ]
        await asyncio.wait_for(started.get(), 2)
        await asyncio.wait_for(started.get(), 2)
        try:
            busy = await client.post(
                "/api/recognize?sample_rate=8000", headers=HEADERS, content=PCM
            )
            assert busy.status_code == 503
        finally:
            finish.set()
            await asyncio.gather(*tasks)
        assert (
            await client.post("/api/recognize?sample_rate=8000", headers=HEADERS, content=PCM)
        ).status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/with-path",
        "https://user:password@example.test",  # pragma: allowlist secret
        "https://example.test/?x=y",
        "https://example.test/#fragment",
    ],
)
def test_configuration_rejects_non_origin_urls(url: str) -> None:
    """Reject ambiguous routing and embedded credentials in configuration."""
    with pytest.raises(ValidationError):
        settings(public_url=url)


def test_statistics_cache_expires_without_showing_stale_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A once-valid count cannot hide a later upstream outage."""
    module = import_module("fingerprint_server.recognition.api")
    clock = SimpleNamespace(monotonic=lambda: 0.0)
    monkeypatch.setattr(module, "time", clock)
    calls = []

    def upstream(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) > 1:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, json={"recordings": {"total": 42}})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(upstream))) as client:
        assert client.get("/api/statistics").json() == {"recordings": 42}
        clock.monotonic = lambda: 59.0
        assert client.get("/api/statistics").json() == {"recordings": 42}
        assert len(calls) == 1
        clock.monotonic = lambda: 61.0
        assert client.get("/api/statistics").status_code == 503


def test_admission_expires_and_rejects_new_addresses_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound accounting memory without evicting active rate-limit budgets."""
    module = import_module("fingerprint_server.recognition.api")
    clock = SimpleNamespace(monotonic=lambda: 0.0)
    monkeypatch.setattr(module, "time", clock)
    admission = Admission()
    request = cast(web.Request, SimpleNamespace(remote="one-client"))
    admission.check(request, "recognize", 1)
    with pytest.raises(HTTPError) as limited:
        admission.check(request, "recognize", 1)
    assert limited.value.status_code == 429
    clock.monotonic = lambda: 60.0
    admission.check(request, "recognize", 1)
    for index in range(4095):
        request = cast(web.Request, SimpleNamespace(remote=f"address-{index}"))
        admission.check(request, "recognize", 1)
    with pytest.raises(HTTPError) as full:
        request = cast(web.Request, SimpleNamespace(remote="new-address"))
        admission.check(request, "recognize", 1)
    assert full.value.status_code == 503
    assert len(admission.requests) == 4096


@pytest.mark.parametrize("sample_rate", [44100, 48000])
@pytest.mark.parametrize("gain", [1.0, 0.1, 0.02])
async def test_public_audio_path_extracts_landmarks_on_real_api(
    tmp_path: Path, sample_rate: int, gain: float
) -> None:
    """Direct and public paths must return the same decision for identical native-rate PCM."""
    import numpy as np
    import soxr
    from audio_fingerprint import HOP_SIZE, SAMPLE_RATE
    from test_server import READ, audio, document, submit
    from test_server import settings as api_settings

    from fingerprint_server.api_state import STORE
    from fingerprint_server.app import create_app as create_api
    from fingerprint_server.database.recognition_metrics import recognition_history

    api = create_api(api_settings(tmp_path))
    async with AsyncClient(api, base_url="http://index.test") as producer:

        async def forward(request: httpx.Request) -> httpx.Response:
            response = await producer.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.content,
            )
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        web = create_app(
            settings(read_key=SecretStr(READ), application_key=None),
            transport=httpx.MockTransport(forward),
        )
        async with AsyncClient(web, base_url=ORIGIN) as visitor:
            samples = audio()
            await submit(producer, document(samples))
            excerpt = samples[50 * HOP_SIZE : 50 * HOP_SIZE + 5 * SAMPLE_RATE]
            if sample_rate != SAMPLE_RATE:
                excerpt = soxr.resample(excerpt, SAMPLE_RATE, sample_rate, quality="HQ")
            pcm = (excerpt * gain).astype("<f4").tobytes()
            direct = await producer.post(
                f"/api/v1/dejavu/recognize?sample_rate={sample_rate}&channels=1",
                content=pcm,
                headers={"Content-Type": "application/octet-stream", "X-API-Key": READ},
            )
            assert direct.status_code == 200
            assert direct.json()["status"] == ("no_match" if gain == 0.02 else "matched")
            response = await visitor.post(
                f"/api/recognize?sample_rate={sample_rate}",
                content=pcm,
                headers=HEADERS,
            )
            assert response.status_code == 200
            assert response.json()["status"] == direct.json()["status"]
            buckets = recognition_history(api[STORE], "all")["buckets"]
            assert sum(bucket["attempts"] for bucket in buckets) == 2
            if direct.json()["status"] == "no_match":
                assert response.json() == {"status": "no_match", "match": None}
                return
            public = response.json()
            artwork_path = public["match"].pop("artwork")
            assert artwork_path.startswith("/api/artwork/")
            assert direct.json()["match"]["reference_id"] not in artwork_path
            assert (await visitor.get(artwork_path)).json() == {"status": "missing", "url": None}
            assert public["match"] == {
                key: direct.json()["match"]["metadata"][key]
                for key in ("title", "artists", "album")
            }
            assert public == {
                "status": "matched",
                "match": {
                    "title": "Synthetic reference",
                    "artists": ["Test signal"],
                    "album": "Synthetic album",
                },
            }
            for unknown in (
                np.zeros(5 * sample_rate, dtype="<f4"),
                np.full(5 * sample_rate, 0.0001, dtype="<f4"),
                np.random.default_rng(99).normal(0, 0.002, 5 * sample_rate).astype("<f4"),
            ):
                negative = await visitor.post(
                    f"/api/recognize?sample_rate={sample_rate}",
                    content=unknown.tobytes(),
                    headers=HEADERS,
                )
                assert negative.status_code == 200
                assert negative.json() == {"status": "no_match", "match": None}
            assert (await visitor.get("/api/statistics")).json() == {"recordings": 1}
