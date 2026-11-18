# Validation and limitations

[Home](../README.md) · [Documentation](README.md)

## Source layout

```text
fingerprint_server/
  app.py                 Public API and microphone page
  admin/                 Optional administrator service
    app.py               Framework views, authentication and service entry point
    data.py              Bounded metadata queries and guarded actions
    templates/           Administrator templates only
  database/
    __init__.py          Backend selection
    sqlite.py            SQLite queue and acoustic reference store
    postgres.py          PostgreSQL store and shared runtime coordination
    migrations.py        SQLite schema upgrades and verification versions
    postgres_schema.py   PostgreSQL table definitions and format version
    postgres_postings.py Experimental compact layout (not deployed)
    logic.py             Backend-independent coverage and matching decisions
  static/                Microphone UI assets
  musicbrainz.py         Metadata verification, batching, cache and cooldown
  worker.py              Ingestion lifecycle
  runtime.py             Process ownership and shutdown
scripts/                 Offline migration, benchmark and maintenance tools
tests/                   Unit tests and opt-in isolated integration tests
docs/
  operations/            Deployment, admin, credentials, migrations and replicas
  benchmarks/            Measurements and experimental results
  reference/             Standalone generated API-reference website
```

Public start commands remain `fingerprint_server.app:app`,
`fingerprint_server.admin:app` and `python -m fingerprint_server.worker_runtime`.
Storage modules are internal; their imports now live under
`fingerprint_server.database`. No stored schema, API or fingerprint format changes
are involved in this reorganization.

## Pre-commit and credential safeguards

Install the development tools and optional admin dependencies, then install the
Git hook once per checkout:

```sh
uv pip install -e '.[dev,admin]'
uv run --no-sync pre-commit install
uv run --no-sync pre-commit run --all-files
```

Like the Music Assistant setup, hooks run tools from the development environment
through `scripts/run-in-env.sh`. They check private-key material, credential/token
patterns, Python syntax, Ruff lint/format, JSON/YAML/TOML, merge conflicts,
filename case, symlinks, newly added files over 1 MiB, whitespace and local
documentation link targets.
The YAML check is syntax-only to support Compose's custom merge tags.

Secret detection runs **offline** (`--no-verify`): it never sends candidate
credentials to external validation services. `.secrets.baseline` contains only
hashes of four reviewed non-secrets: two acoustic hash examples, an intentionally
wrong test password and a dummy credential URL used to test rejection. It does
not exempt whole test directories. Do not regenerate the baseline to silence a
new finding: inspect it, remove/rotate real credentials, and approve only
demonstrably synthetic fixtures. The separate private-key hook has no baseline.

The repository-checks GitHub workflow runs the same hooks, type checks and
hook/layout regression tests on pull requests and pushes to `main`. Local hooks
can be bypassed, so they are not the only check before publication.

Private environment files, common private-key containers and credential JSON
filenames are ignored as an additional guard, not a substitute for scanning.
Hooks inspect files included in a commit, not all historical commits, and cannot
guarantee that every secret format will be detected. Never commit production
credentials. If a secret was committed, rotate it even after deleting the file.

## Validation and limitations

```sh
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy fingerprint_server
node --test tests/test_microphone.js tests/test_admin_live.js
# Optional: real gateway test with isolated containers, fresh volumes and local ports.
FP_RUN_GATEWAY_TESTS=1 uv run --no-sync pytest -q tests/test_gateway_integration.py
# Optional PostgreSQL runtime and importer tests: disposable PostgreSQL 17 containers.
uv pip install -e '.[dev,postgres]'
FP_TEST_POSTGRES=1 uv run --no-sync pytest -q tests/test_postgres_store.py tests/test_postgres_migration.py
# Optional administrator authentication, tables, key management and guarded actions.
uv pip install -e '.[dev,admin]'
FP_TEST_POSTGRES=1 uv run --no-sync pytest -q tests/test_admin.py tests/test_admin_live.py tests/test_application_keys.py
# Shared budgets, plus real APISIX balancing and failover in a separate Compose project.
FP_TEST_POSTGRES=1 uv run --no-sync pytest -q tests/test_replicas.py
FP_RUN_REPLICA_TESTS=1 uv run --no-sync pytest -q tests/test_replicas_integration.py
```

The PostgreSQL tests use fresh, randomly named schemas and loopback-bound containers, never an operator's database. They check exact candidate-limit boundaries, concurrent admission/idempotence, FIFO, publication rollback, recording grouping, coverage/recognition parity, cleanup dependencies, imported queue recovery, separate roles and loss of ownership. Without `FP_TEST_POSTGRES=1`, integration cases are skipped. The ordinary suite still exercises SQLite.

The admin tests require the optional `admin` extra; otherwise that module is
skipped. They test separate login credentials, session revocation, same-origin
POST actions, metadata-only tables, bounded filters and key redaction using a
mock gateway. With `FP_TEST_POSTGRES=1`, they additionally exercise actual
PostgreSQL projections, admission budgets and atomic mutation guards. Use
`.[dev,admin]` when running type checks for the optional admin modules.

The [replica test](operations/replicas.md#tested-and-not-tested) builds the two-API image and checks actual APISIX upstream access logs, backend-wide limits, ingestion, PCM recognition, API exclusion/rejoin and worker restart. It leaves the live Compose project untouched. This is a functional test on synthetic data, not a full-collection concurrent-capacity benchmark.

For a full-collection rehearsal, use the separate tool below after installing the PostgreSQL extra. It opens the SQLite source read-only and creates a consistent temporary snapshot, then uses a disposable, loopback-bound PostgreSQL 17 container limited to two CPUs and 4 GiB RAM. Allow substantial free disk space for the snapshot, both PostgreSQL databases, indexes, WAL and dump, and expect sustained local disk/CPU activity. The source may be a live database because this tool uses SQLite's backup API; do not substitute a plain filesystem copy of a live WAL database.

```sh
uv run --no-sync python scripts/postgres_runtime_trial.py /path/to/fingerprints.db \
  --samples 50 --repetitions 3 --report /private/path/postgres-runtime-trial.json
```

The tool checks every imported field and landmark through the offline importer; compares sampled complete recognition results, statistics and availability; and starts the actual HTTP API and worker in separate local subprocesses. Imported open work is paused **only in PostgreSQL after the untouched-import comparison**. Synthetic fixtures then test HTTP ingestion, PCM recognition, interrupted-claim recovery, missing-identifier reference/landmark cleanup and two concurrent store lookups. A local MusicBrainz stub serves only the synthetic recording; any real-catalogue lookup fails the trial instead of contacting MusicBrainz.

Finally, the tool stops API/worker subprocesses and exports the updated PostgreSQL collection to a new SQLite file using the [reverse migration tool](operations/database-migration.md#postgresql-back-to-sqlite). It verifies all exported fields and landmarks, compares recognition/statistics/availability and confirms the PostgreSQL source is unchanged. It also creates a custom-format `pg_dump`, restores it into a second empty database, checks binary SHA-256 digests and row counts of every application table, and repeats sampled recognition against the restored backend. The report retains measurements and checksums, not audio or credentials. Temporary snapshots, databases, subprocesses, reverse export and dump are removed on exit. This is a bounded functional rehearsal, not a sustained-load or high-availability benchmark.

The offset-voting matcher reports distinct aligned landmarks, their fraction and matched time span, **not a calibrated probability**. Thresholds are initial conservative defaults, not measured real-microphone accuracy. Test room noise, resampling, capture phase, different masterings and ambiguous references before relying on it. Compatibility with other Dejavu versions/databases is not established.

The local browser worklet/HTTP path has been tested with a synthetic reference and a simulated microphone stream, including automatic track shutdown. This does not validate real acoustic recording quality. The opt-in Docker test builds the service and exercises the real APISIX gateway with isolated volumes: both credentials, role restrictions, PCM recognition, revocation, per-consumer limits and an unpublished backend port.

A later Sendspin/AUX integration should submit bounded incoming PCM to this same recognition endpoint and consume returned metadata. A safe generic live-PCM tap in MA still needs design; no Sendspin or player/queue internals are modified by this project.
