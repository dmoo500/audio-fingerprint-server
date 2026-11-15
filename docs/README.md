# Documentation

[Home](../README.md)

All shell commands assume the repository root, not this directory. Links are relative Markdown links and work when browsing the repository on GitHub. Mermaid diagrams remain alongside the relevant explanations.

## Install and operate

- [Local setup](deployment.md#local-setup) and [Docker](deployment.md#docker)
- [Logging and gateway admission protection](deployment.md#gateway-admission-protection)
- [Database versions, backup and recovery](deployment.md#database-upgrades-and-version-policy)
- [APISIX setup and personal credentials](gateway.md#optional-apisix-mode-application-token-and-personal-token)
- [Credential storage, rotation and rollback](gateway.md#storage-rotation-and-rollback)
- [MusicBrainz configuration and cooldowns](metadata.md#optional-metadata-enrichment)
- [Pending housekeeping, queue window and manual checks](metadata.md#pending-metadata-housekeeping)

## Understand and integrate

- [Contribution, publication and recognition flow](architecture.md#how-it-works)
- [Database tables](architecture.md#database-tables)
- [HTTP API and request limits](api.md#api)
- [Compatibility with sampled references](api.md#upgrade-from-sampled-references)

## Evaluate and develop

- [Storage benchmarks](benchmarks.md#database-storage)
- [Extraction and codec benchmarks](benchmarks.md#library-extraction-and-document-encoding)
- [Short-capture recognition results](benchmarks.md#short-capture-recognition)
- [Validation commands and known limitations](development.md#validation-and-limitations)
