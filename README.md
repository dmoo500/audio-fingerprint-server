# Audio Fingerprint Server

An independent experimental service compatible with Music Assistant fingerprint submissions. This is not an official Music Assistant or Open Home Foundation (OHF) project, and no endorsement or adoption is implied. Future integration would require agreement with the respective maintainers. Music Assistant names below describe compatibility, not ownership. The shared library uses the neutral distribution name `audio-fingerprint` and Python import `audio_fingerprint`; stored fingerprint format IDs and this server's deployment directory remain unchanged for compatibility.

Licensed under [MIT](LICENSE). The shared library is available at [dmoo500/audio-fingerprint](https://github.com/dmoo500/audio-fingerprint). Its exact Git revision is pinned because there is no PyPI release.

The old analysis server is not modified. It accepts the MA `dejavu_analysis` provider's full-track `ma-dejavu-landmarks-v2` fingerprints and legacy sampled `ma-dejavu-landmarks-v1` documents, durably queues them and publishes validated references atomically. A local microphone page tests recognition against those references. No original provider audio is requested or stored; microphone PCM is processed in memory and discarded.

## Getting started

1. Follow [local setup or Docker deployment](docs/deployment.md#local-setup). Run one backend process against each database.
2. Enable fingerprint sharing in the compatible MA analysis integration before analyzing tracks; old private analyses are not automatically uploaded.
3. Open the server's microphone page, enter the read key and capture a known indexed track. Microphone recognition does not add unknown songs to the collection.

This is an experimental service. Recognition quality depends on indexed coverage and capture conditions; it is not guaranteed. Before exposing it remotely, read the [gateway and credentials guide](docs/gateway.md) and [admission-protection limits](docs/deployment.md#gateway-admission-protection). Back up existing installations and read the [database upgrade policy](docs/deployment.md#database-upgrades-and-version-policy) before upgrading.

## Documentation

| Topic | Contents |
| --- | --- |
| [Architecture and data flow](docs/architecture.md) | Contribution, queue processing and microphone recognition diagrams; database tables |
| [Setup and deployment](docs/deployment.md) | Local installation, Docker, logging, DoS admission controls, backups and migrations |
| [HTTP API](docs/api.md) | Endpoints, credentials, limits and fingerprint-format compatibility |
| [Credentials and APISIX gateway](docs/gateway.md) | Personal keys, application tokens, gateway setup, rotation and rollback |
| [MusicBrainz and pending metadata](docs/metadata.md) | Verification, cooldowns, housekeeping, shared FIFO queue and manual rechecks |
| [Benchmarks](docs/benchmarks.md) | Storage and codec measurements, charts and short-capture experiments |
| [Validation and limitations](docs/development.md) | Test commands, integration checks and recognition limitations |

See the [documentation index](docs/README.md) for task-oriented links. Commands in the documentation run from the repository root unless stated otherwise.

## Benchmark results and shipped optimizations

Measurements and charts have moved to [Benchmarks](docs/benchmarks.md#benchmark-results-and-shipped-optimizations).

### Database storage

See [database storage measurements](docs/benchmarks.md#database-storage).

### Library extraction and document encoding

See [library extraction and document encoding](docs/benchmarks.md#library-extraction-and-document-encoding).

### Short-capture recognition

See [short-capture recognition results](docs/benchmarks.md#short-capture-recognition).
