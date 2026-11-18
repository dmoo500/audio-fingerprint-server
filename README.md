# Audio Fingerprint Server

An independent experimental service compatible with Music Assistant fingerprint submissions. This is not an official Music Assistant or Open Home Foundation (OHF) project, and no endorsement or adoption is implied. Future integration would require agreement with the respective maintainers. Music Assistant names below describe compatibility, not ownership. The shared library uses the neutral distribution name `audio-fingerprint` and Python import `audio_fingerprint`; stored fingerprint format IDs and this server's deployment directory remain unchanged for compatibility.

Licensed under [MIT](LICENSE). The shared library is available at [dmoo500/audio-fingerprint](https://github.com/dmoo500/audio-fingerprint). Its exact Git revision is pinned because there is no PyPI release.

The old analysis server is not modified. This server accepts the MA `dejavu_analysis` provider's full-track `ma-dejavu-landmarks-v2` fingerprints and legacy sampled `ma-dejavu-landmarks-v1` documents, durably queues them and publishes validated references atomically. No original provider audio is requested or stored; microphone PCM is processed in memory and discarded.

## Getting started

1. Follow [local setup or Docker deployment](docs/operations/deployment.md#local-setup). SQLite uses one combined process; optional PostgreSQL permits separate API/worker roles and [opt-in API replicas behind APISIX](docs/operations/replicas.md), always with one ingestion worker.
2. Enable fingerprint sharing in the compatible MA analysis integration before analyzing tracks; old private analyses are not automatically uploaded.
3. To try recognition in a browser, start the optional [public recognition app](docs/operations/recognition.md) and capture a known indexed track. Microphone recognition does not add unknown songs to the collection.

For visitors, the optional [public recognition app](docs/operations/recognition.md)
provides a separate microphone-only experience without exposing API keys in the
browser. It includes a searchable-recording counter and keeps technical and admin
data private. It is a separately configured service, not enabled by default.

This is an experimental service. Recognition quality depends on indexed coverage and capture conditions; it is not guaranteed. Before exposing it remotely, read the [gateway and credentials guide](docs/operations/gateway.md) and [admission-protection limits](docs/operations/deployment.md#gateway-admission-protection). Back up existing installations and read the [database upgrade policy](docs/operations/deployment.md#database-upgrades-and-version-policy) before upgrading.

## Documentation

| Topic | Contents |
| --- | --- |
| [Architecture and data flow](docs/architecture.md) | Contribution, queue processing and microphone recognition diagrams; database tables |
| [Setup and deployment](docs/operations/deployment.md) | Local installation, Docker, logging, DoS admission controls, backups and migrations |
| [Administrator website](docs/operations/admin.md) | Browse submissions and references, unresolved ISRCs, retry/delete actions and personal keys |
| [HTTP API](docs/api.md) | Endpoints, [OpenAPI specification](openapi.json), generated clients, credentials and limits |
| [Credentials and APISIX gateway](docs/operations/gateway.md) | Personal keys, application tokens, gateway setup, rotation and rollback |
| [MusicBrainz and pending metadata](docs/metadata.md) | Verification, cooldowns, housekeeping, shared FIFO queue and manual rechecks |
| [Benchmarks](docs/benchmarks.md) | Storage and codec measurements, charts and short-capture experiments |
| [Development and validation](docs/development.md) | Source layout, Pre-Commit/secret safeguards, tests and recognition limitations |

See the [documentation index](docs/README.md) for task-oriented links. Commands in the documentation run from the repository root unless stated otherwise.

## Benchmark results and shipped optimizations

Measurements and charts have moved to [Benchmarks](docs/benchmarks.md#benchmark-results-and-shipped-optimizations).

### Database storage

See [database storage measurements](docs/benchmarks.md#database-storage).

### Library extraction and document encoding

See [library extraction and document encoding](docs/benchmarks.md#library-extraction-and-document-encoding).

### Short-capture recognition

See [short-capture recognition results](docs/benchmarks.md#short-capture-recognition).
