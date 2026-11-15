# Validation and limitations

[Home](../README.md) · [Documentation](README.md)

## Validation and limitations

```sh
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy fingerprint_server
node --test tests/test_microphone.js
# Optional: real gateway test with isolated containers, fresh volumes and local ports.
FP_RUN_GATEWAY_TESTS=1 uv run --no-sync pytest -q tests/test_gateway_integration.py
```

The offset-voting matcher reports distinct aligned landmarks, their fraction and matched time span, **not a calibrated probability**. Thresholds are initial conservative defaults, not measured real-microphone accuracy. Test room noise, resampling, capture phase, different masterings and ambiguous references before relying on it. Compatibility with other Dejavu versions/databases is not established.

The local browser worklet/HTTP path has been tested with a synthetic reference and a simulated microphone stream, including automatic track shutdown. This does not validate real acoustic recording quality. The opt-in Docker test builds the service and exercises the real APISIX gateway with isolated volumes: both credentials, role restrictions, PCM recognition, revocation, per-consumer limits and an unpublished backend port.

A later Sendspin/AUX integration should submit bounded incoming PCM to this same recognition endpoint and consume returned metadata. A safe generic live-PCM tap in MA still needs design; no Sendspin or player/queue internals are modified by this project.
