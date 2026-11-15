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

This first implementation uses SQLite/WAL and **one application process**. Do not scale it to multiple workers/replicas: queue recovery, request throttling and the ingestion worker are single-process. PostgreSQL and distributed workers are not included. Never delete the volume during upgrades; back up the SQLite database with SQLite's backup API (or with the service stopped), not by copying a live database without its WAL. Startup upgrades recognized older databases before starting the worker or serving requests; see the version and recovery policy below. Incomplete historical references remain excluded from recognition and their formerly processed uploads become `pending_metadata`. Malformed historical coverage is counted as partial for otherwise complete references.

Never run two receiver workers against the same database. Keep credentials, collection backups and machine-specific Compose overrides outside version control. If MA already binds port 8097, use a local override for the gateway's published port; do not publish the backend.

### Database upgrades and version policy

Four independent versions describe different contracts:

| Version | Current value | When it changes |
| --- | --- | --- |
| Database schema (`PRAGMA user_version`, `DB_SCHEMA_VERSION`) | 7 | Stored layout or document encoding needs a migration |
| HTTP API major version | `/api/v1` | An incompatible request/response contract requires a new API major version |
| Fingerprint format (`format_id`) | v1 and v2 supported | Acoustic parameters or coverage semantics change |
| Landmark encoding (`fingerprints_encoding`) | `sha1-80-offset-delta-v1` or legacy JSON | Lossless landmark representation changes |

DB version 0 denotes the original unversioned databases, with or without the publication-readiness column. Migration 1 completes the publication gate. Migration 2 converts hashes to binary storage and assigns internal integer reference IDs. Public reference keys, submission IDs, decoded documents, metadata, offsets, cache entries and publication decisions are retained. This is a storage change, not a fingerprint algorithm or HTTP API change; producers need no update or reanalysis. Migration 3 compacts existing valid canonical submission documents using the shared codec. Invalid or noncanonical records are logged and retained unchanged; migration never invents replacement IDs. Migration 4 records verification versions and configuration transitions. Migration 5 adds pending-housekeeping counters and deadlines with a seven-day grace period and no migration-time deletion. Migration 6 adds FIFO admission timestamps and durable background-work classification, retaining the existing queue order and pending deadlines/counters without deleting anything. Migration 7 adds a logical-recording key and associates only references whose recording ID was verified by the current MusicBrainz verification version. Invalid or missing metadata is logged and left ungrouped; acoustic references, landmarks and submission IDs are not merged or deleted. New installations create schema 7 directly.

Before upgrading an existing database, startup creates a consistent SQLite backup beside it named `<database>.pre-v<old-version>-<timestamp>.bak` and logs its path. The backup includes committed WAL data, is private to the file owner and is not automatically deleted. Allow space for this full backup, the replacement tables and the migration WAL. Large collections can take time to upgrade; do not start a second server process. All migration steps and version changes commit together. A failed migration rolls back and stops startup with an error instead of resetting the collection. Invalid hashes, orphaned landmarks, unknown layouts and newer unsupported schema versions are refused rather than discarded or guessed.

The compact tables use fewer active pages, but SQLite reuses freed pages rather than automatically shrinking the file. Optional file compaction with SQLite `VACUUM` should be scheduled separately while the service is stopped, after a backup and with enough temporary disk space. Startup does not run `VACUUM`.

For a downgrade, stop the service and restore the pre-upgrade backup using SQLite's backup tooling before starting the older executable. Do not run old code against the compact schema or copy over a database while its WAL/SHM files are still in use. A restore loses uploads accepted after that backup; retain the upgraded database separately if those later uploads need recovery.

Future DB changes add ordered migrations and increase `DB_SCHEMA_VERSION`; unsupported newer versions must continue to stop startup. A storage-only change does not bump the API. An incompatible API change needs a new route version and coordinated producer support. A new acoustic format needs an explicit `format_id`, receiver support before producers enable it, and a compatibility decision; a DB migration never relabels old fingerprints.
