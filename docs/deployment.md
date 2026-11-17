# Setup and deployment

[Home](../README.md) · [Documentation](README.md)

## Local setup

After upgrading a gateway deployment, apply its normal gateway configuration to add the upload-protected availability route without replacing existing personal credentials. Older clients may ignore the additive capability; older servers remain usable by clients that treat absent availability support as optional.

Clone using the technical directory name expected by the Docker build:

```sh
git clone https://github.com/dmoo500/audio-fingerprint-server.git music-assistant-fingerprint-server
cd music-assistant-fingerprint-server
uv venv --python 3.14
uv pip install -e '.[dev]'
export FP_UPLOAD_KEY="$(uv run --no-sync python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export FP_READ_KEY="$(uv run --no-sync python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run --no-sync uvicorn fingerprint_server.app:app --host 127.0.0.1 --port 8097 --workers 1
```

Keep these keys privately for your session. Set the MA provider's URL to `http://localhost:8097` and its API key to `FP_UPLOAD_KEY` when MA runs on the same machine. MA in another container needs a reachable HTTPS destination; localhost there means that container, not this host. Do not relax the MA provider's HTTP guard.

Open <http://localhost:8097>, enter the **read** key and allow microphone access. The page records exactly ten seconds after you press the button, stops microphone access and sends normalized mono float32 PCM to this server. It does not keep the key in browser storage. The upload key is never distributed with the website.

Enable sharing in MA **before** analyzing reference tracks. Old private analyses are not backfilled. The reference database must contain the song and the captured passage must overlap indexed audio. New full-track references have no sampling gaps; legacy references remain only partially covered.

## Docker

### Gateway admission protection

The Docker backend uses the packaged `fingerprint_server/logging.json` configuration for consistent console output: Unix epoch seconds with millisecond precision, severity, logger name and message. For example: `1794764000.125 INFO: [fingerprint_server.worker] ...`. Native development runs can opt in with `uvicorn fingerprint_server.app:app --log-config fingerprint_server/logging.json`. Gateway access logs are JSON with numeric Unix `timestamp`, `level`, method, path and status; Nginx/APISIX native diagnostic logs retain their own format.

The optional APISIX deployment applies an IP-based global rule before route authentication: five requests per second with burst capacity ten, and at most eight concurrent in-flight requests per direct peer IP, with excess requests rejected using HTTP 429. Existing per-consumer route quotas still apply. The shared IP budget also covers public routes and invalid credentials; clients behind the same NAT share it. These controls are local to the gateway instance, not a distributed quota.

APISIX authentication can reject a request during rewrite, before ordinary access-phase rate limiters execute. The global rule therefore invokes OpenResty's leaky-bucket limiter through the supported serverless rewrite hook, using the enabled limit-req shared dictionary; the concurrency limiter remains in the access phase. A local Docker check returned 11 authentication rejections and 21 rate-limit rejections for a 32-request invalid-credential burst, including forged forwarded-IP headers. A separate ten-request overlapping probe admitted eight requests and rejected two; the temporary probe route was removed. These are bounded functional checks, not a load-capacity benchmark.

Gateway header inactivity is limited to ten seconds, body inactivity to thirty seconds, idle keep-alive to thirty seconds, and request bodies to 36 MiB. Body inactivity is not a total upload deadline or minimum transfer-rate guarantee; the backend also bounds body-reading time and recognition concurrency. The connection plugin limits in-flight HTTP requests, not every open TCP socket. Application admission controls do not prevent bandwidth-exhaustion or distributed DDoS attacks; public deployments still need upstream network/edge protection.

This configuration assumes direct access. Do not trust arbitrary forwarded-IP headers. Before placing a proxy or tunnel in front of APISIX, explicitly configure trusted proxy addresses and verify client-IP handling; otherwise every proxied client shares the proxy's budget. Re-run gateway `configure` after upgrading to install the owned `fingerprint-admission` global rule; unrelated global rules and personal consumers are not changed. Restart the gateway when changing its plugin list or Nginx timeouts.

The build context is the parent directory; use the clone directory name shown above. Only this project's source is copied into the image; the shared library is installed from its pinned public Git revision. From this directory, set both private key environment variables and run `docker compose up --build`. The named volume holds the durable queue and references. The web port is bound only to loopback. Use an authenticated HTTPS deployment and review operational limits before exposing the service publicly. Microphone access on non-localhost sites requires HTTPS.

SQLite/WAL remains the default and requires **one application process**. PostgreSQL supports a combined process or separate API/worker roles, plus an [opt-in two-API deployment](replicas.md) behind APISIX; ingestion still has exactly one worker. Never delete the SQLite volume during upgrades; back up the database with SQLite's backup API (or with the service stopped), not by copying a live database without its WAL. Startup upgrades recognized older SQLite databases before starting the worker or serving requests; see the version and recovery policy below. Incomplete historical references remain excluded from recognition and their formerly processed uploads become `pending_metadata`. Malformed historical coverage is counted as partial for otherwise complete references.

Never run two receiver workers against the same database. Keep credentials, collection backups and machine-specific Compose overrides outside version control. If MA already binds port 8097, use a local override for the gateway's published port; do not publish the backend.

### MusicBrainz capacity planning before public operation

An optional, separately operated MusicBrainz mirror is a future capacity option,
not part of this deployment. The worker currently uses direct recording-ID and
ISRC lookups, so indexed full-text search is not required for its present calls.
Measure time spent on uncached lookups versus landmark publication before
provisioning a mirror; neither more APIs nor a mirror alone proves higher
ingestion throughput.

The client accepts `FP_MUSICBRAINZ_BASE_URL`, but still paces uncached requests
at 1.1-second intervals for every endpoint. A mirror therefore also needs a
tested, explicitly bounded private-endpoint request policy before we can use
its additional capacity. Keep caching and error cooldowns. Do not silently
fall back to the public API with a higher private-mirror request budget.

The [official mirror deployment](https://github.com/metabrainz/musicbrainz-docker)
lists approximately two CPU threads, 4 GB RAM and 100 GB disk without indexed
search; allow additional space for imports, backups and upgrades. Confirm the
chosen release's architecture support and actual collection size on the target
host. Keep its database, credentials, volumes and release lifecycle separate
from the fingerprint database.

Before enabling it for public operation, plan:

- Initial MusicBrainz dump import and hourly
  [Live Data Feed](https://musicbrainz.org/doc/Live_Data_Feed) replication, with
  a private MetaBrainz token and the appropriate personal/noncommercial or
  commercial usage arrangement.
- Monitoring of replication lag, lookup errors, disk space and backup recovery.
- Pinned mirror releases and the upstream
  [release-specific upgrade procedure](https://github.com/metabrainz/musicbrainz-docker/releases):
  data replication, application upgrades, schema migrations and PostgreSQL
  major upgrades are separate operations, not automatic image replacements.
- A maintenance plan that retains queued submissions while verification is
  unavailable; recognition against already published references does not
  require a live MusicBrainz request.

### Optional PostgreSQL backend

This is an experimental alternative, not an automatic conversion or an instruction to switch an existing installation. SQLite files and settings remain unchanged. Back up PostgreSQL separately with PostgreSQL-aware tooling or your managed database's backup service; SQLite's automatic pre-upgrade backup does not protect it.

Install `uv pip install -e '.[postgres]'` for native runs. The Docker image includes this extra. Provision PostgreSQL 17 (the tested version) with a dedicated database and role allowed to create and own the target schema. Use a direct connection or a **session-mode** pooler: transaction-mode pooling is incompatible with the process ownership locks. Configure standard libpq variables such as `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` and `PGPASSFILE`. Protect the password file and use `PGSSLMODE=verify-full` plus the appropriate trusted certificate for remote databases. Alternatively, inject `FP_POSTGRES_DSN` as a secret; do not put credentials in command-line arguments or version control.

| Setting | Default | Meaning |
| --- | --- | --- |
| `FP_DATABASE_BACKEND` | `sqlite` | Explicitly choose `sqlite` or `postgres`; errors never fall back to another database. |
| `FP_DATABASE` | `data/fingerprints.db` | SQLite path; ignored by PostgreSQL. |
| `FP_POSTGRES_SCHEMA` | `fingerprints` | Dedicated schema; 1–63 lowercase ASCII letters, digits or underscores, starting with a letter. A missing schema is created; an unrecognized existing schema or unsupported version stops startup. |
| `FP_POSTGRES_DSN` | unset | Optional secret libpq connection string; otherwise use standard libpq environment variables. |
| `FP_SERVICE_ROLE` | `all` | `all` runs API and worker together; PostgreSQL also permits `api` or `worker` separately. SQLite rejects split roles. |
| `FP_API_REPLICAS_ENABLED` | `false` | Allow shared API membership only with PostgreSQL and `FP_SERVICE_ROLE=api`; keep the worker separate. See [replica setup and limits](replicas.md). |

With the same private credentials, database connection and verification/housekeeping settings supplied to both processes:

```sh
export FP_DATABASE_BACKEND=postgres
export FP_POSTGRES_SCHEMA=fingerprints

# Terminal / process 1: HTTP API only
FP_SERVICE_ROLE=api uv run --no-sync uvicorn fingerprint_server.app:app \
  --host 127.0.0.1 --port 8097 --workers 1

# Terminal / process 2: one ingestion worker, no HTTP listener
FP_SERVICE_ROLE=worker uv run --no-sync python -m fingerprint_server.worker_runtime
```

For a combined process, omit `FP_SERVICE_ROLE` and start Uvicorn as above. In Compose, add a private override supplying the libpq variables or an `env_file` to `fingerprints`; the base files forward the backend and schema selection, not database passwords. Splitting roles additionally requires a separate worker service with the same environment/network and command `["python", "-m", "fingerprint_server.worker_runtime"]`, while setting `FP_SERVICE_ROLE=api` on the HTTP service. Do not publish a worker port.

By default PostgreSQL session locks reject a second API process, and they always reject a second worker for the same database/schema. Opt-in API replicas use shared API membership; they cannot overlap an exclusive combined/default API process. Use stop-then-start / `Recreate` when changing modes or replacing the worker; rolling schema upgrades are not supported. Ownership connections are checked every five seconds; loss stops the local worker and makes the API reject requests. A worker token fences stale mutations after another worker takes ownership. `/health` and upload admission require a running local worker, or an external worker with both a held lock and a heartbeat younger than twenty seconds. Database failures surface as errors, not successful empty matches. The standalone worker polls for new work every second; the combined process retains immediate local wakeups.

Only the ingestion owner recovers interrupted jobs and applies verification-mode transitions; API startup never resets active claims. Queue admission and other mutations serialize through a transaction lock, preserving shared count/UTF-8 byte budgets and atomic publication/cleanup. Reads use short-lived connections and consistent snapshots. Lookup statements have a 60-second timeout and lock waits a 15-second timeout; long imports use the separate offline tool. PostgreSQL request-rate budgets and the two recognition slots are database-wide; adding API replicas does not multiply those limits. This stage is not a high-availability deployment or a parallel-load capacity claim.

Import SQLite with the [offline migration tool](benchmarks/postgresql-runtime.md#offline-migration), not by merely changing `FP_DATABASE_BACKEND`. Its PostgreSQL format 1 is the runtime's format 1; select its destination schema explicitly. After import, run PostgreSQL `VACUUM (ANALYZE)` on the target tables using the target schema's search path before measuring recognition performance. A production switch still needs a separately approved final snapshot, recognition comparison and rollback/reconciliation plan. Do not resume writes on both copies.

The [database migration guide](database-migration.md) covers both directions.
The reverse exporter produces a new, checked SQLite schema 8 file containing
the current PostgreSQL collection, including later changes, while all
PostgreSQL API/worker roles are stopped. It never merges or overwrites databases.

An isolated [full-collection runtime and restore rehearsal](benchmarks.md#full-collection-runtime-and-restore-rehearsal) has passed on the local setup. It validated import, sampled recognition parity, real API/worker subprocesses, worker recovery, synthetic housekeeping and complete dump/restore content checks without switching the live installation. PostgreSQL still used about 2.22 times the occupied SQLite space, before WAL and backups. Repeat the rehearsal on the intended destination before treating those local timings as deployment expectations.

### Database upgrades and version policy

Independent versions describe different contracts:

| Version | Current value | When it changes |
| --- | --- | --- |
| SQLite schema (`PRAGMA user_version`, `DB_SCHEMA_VERSION`) | 8 | SQLite layout or document encoding needs a migration |
| PostgreSQL schema (`migration_info.format_version`) | 1 | PostgreSQL layout needs a migration; independent of SQLite's version |
| HTTP API major version | `/api/v1` | An incompatible request/response contract requires a new API major version |
| Fingerprint format (`format_id`) | v1 and v2 supported | Acoustic parameters or coverage semantics change |
| Landmark encoding (`fingerprints_encoding`) | `sha1-80-offset-delta-v1` or legacy JSON | Lossless landmark representation changes |

DB version 0 denotes the original unversioned databases, with or without the publication-readiness column. Migration 1 completes the publication gate. Migration 2 converts hashes to binary storage and assigns internal integer reference IDs. Public reference keys, submission IDs, decoded documents, metadata, offsets, cache entries and publication decisions are retained. This is a storage change, not a fingerprint algorithm or HTTP API change; producers need no update or reanalysis. Migration 3 compacts existing valid canonical submission documents using the shared codec. Invalid or noncanonical records are logged and retained unchanged; migration never invents replacement IDs. Migration 4 records verification versions and configuration transitions. Migration 5 adds pending-housekeeping counters and deadlines with a seven-day grace period and no migration-time deletion. Migration 6 adds FIFO admission timestamps and durable background-work classification, retaining the existing queue order and pending deadlines/counters without deleting anything. Migration 7 adds a logical-recording key and associates only references whose recording ID was verified by the current MusicBrainz verification version. Invalid or missing metadata is logged and left ungrouped; acoustic references, landmarks and submission IDs are not merged or deleted. Migration 8 adds an index for finding landmarks by reference; it does not delete data. New installations create schema 8 directly.

Before upgrading an existing database, startup creates a consistent SQLite backup beside it named `<database>.pre-v<old-version>-<timestamp>.bak` and logs its path. The backup includes committed WAL data, is private to the file owner and is not automatically deleted. Allow space for this full backup, the replacement tables and the migration WAL. The schema 8 landmark index also requires substantial additional disk space, including temporary space during construction; its size depends on the collection. Large collections can take time to upgrade; do not start a second server process. All migration steps and version changes commit together. A failed migration rolls back and stops startup with an error instead of resetting the collection. Invalid hashes, orphaned landmarks, unknown layouts and newer unsupported schema versions are refused rather than discarded or guessed.

The compact tables use fewer active pages, but SQLite reuses freed pages rather than automatically shrinking the file. Optional file compaction with SQLite `VACUUM` should be scheduled separately while the service is stopped, after a backup and with enough temporary disk space. Startup does not run `VACUUM`.

For a downgrade, stop the service and restore the pre-upgrade backup using SQLite's backup tooling before starting the older executable. Do not run old code against the compact schema or copy over a database while its WAL/SHM files are still in use. A restore loses uploads accepted after that backup; retain the upgraded database separately if those later uploads need recovery.

Future SQLite changes add ordered migrations and increase `DB_SCHEMA_VERSION`; unsupported newer versions must continue to stop startup. PostgreSQL currently creates or validates format 1 under the shared startup lock; it does not guess an upgrade path or recreate an incompatible schema. A future PostgreSQL format change needs its own ordered migration and backup/recovery procedure. `migration_info.sqlite_schema_version` records the source layout supported by the importer, not the runtime's schema version. A storage-only change does not bump the API. An incompatible API change needs a new route version and coordinated producer support. A new acoustic format needs an explicit `format_id`, receiver support before producers enable it, and a compatibility decision; a DB migration never relabels old fingerprints.
