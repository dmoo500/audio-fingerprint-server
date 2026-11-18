# Operations

[Documentation](../README.md)

- [Install and deploy](deployment.md): local setup, Docker, logging and upgrades.
- [Administrator website](admin.md): browse metadata, review unresolved ISRCs,
  retry/delete submissions and manage personal API keys.
- [Gateway and credentials](gateway.md): APISIX configuration, scopes and key rotation.
- [Database migration and rollback](database-migration.md): verified offline
  SQLite/PostgreSQL transfers and experimental storage-layout conversions.
- [API replicas](replicas.md): two API services and one ingestion worker.

Run commands from the repository root. Store deployment credentials and machine-specific
Compose overrides outside the repository. No operational procedure should require
committing keys, database files or catalogue backups.
