# Documentation

[Home](../README.md)

All shell commands assume the repository root, not this directory. Links are relative Markdown links and work when browsing the repository on GitHub. Mermaid diagrams remain alongside the relevant explanations.

## Install and operate

- [Local setup](operations/deployment.md#local-setup) and [Docker](operations/deployment.md#docker)
- [Logging and gateway admission protection](operations/deployment.md#gateway-admission-protection)
- [Database versions, backup and recovery](operations/deployment.md#database-upgrades-and-version-policy)
- [Moving between SQLite and PostgreSQL, including rollback](operations/database-migration.md)
- [Two API replicas, APISIX balancing and shared limits](operations/replicas.md)
- [APISIX setup and personal credentials](operations/gateway.md#optional-apisix-mode-application-token-and-personal-token)
- [Credential storage, rotation and rollback](operations/gateway.md#storage-rotation-and-rollback)
- [Administrator website: tables, unresolved ISRCs and personal API keys](operations/admin.md)
- [Public microphone recognition app](operations/recognition.md)
- [MusicBrainz configuration and cooldowns](metadata.md#optional-metadata-enrichment)
- [Pending housekeeping, queue window and manual checks](metadata.md#pending-metadata-housekeeping)

## Understand and integrate

- [Contribution, publication and recognition flow](architecture.md#how-it-works)
- [Database tables](architecture.md#database-tables)
- [HTTP API and request limits](api.md#api)
- [Compatibility with sampled references](api.md#upgrade-from-sampled-references)

## Evaluate and develop

- [Benchmark overview](benchmarks.md) and [historical SQLite growth](benchmarks/sqlite-storage.md)
- [PostgreSQL storage and query layouts](benchmarks/postgresql-layout.md)
- [Full-collection migration, runtime and restore checks](benchmarks/postgresql-runtime.md)
- [Extraction, codec and short-capture recognition results](benchmarks/audio.md)
- [Validation commands and known limitations](development.md#validation-and-limitations)
