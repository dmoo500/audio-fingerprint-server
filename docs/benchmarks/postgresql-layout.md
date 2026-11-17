# PostgreSQL storage and query layouts

[Home](../../README.md) · [Documentation](../README.md) · [Benchmark overview](../benchmarks.md)

## Experimental PostgreSQL comparison

The server now has an optional PostgreSQL backend. The original
`scripts/postgres_benchmark.py` remains an isolated storage/query experiment,
not the runtime or a production migration.
It copies a schema 7 or 8 SQLite backup, adds the schema 8 landmark index to the
copy if needed, imports its tables and comparable indexes into a temporary local
PostgreSQL 17 container, and checks row counts and exact recognition-candidate
sets for a small sample. It reports storage and warm candidate-query timings.
The benchmark removes the temporary container and copy when it exits normally.
It requires Docker, local disk space for both databases and temporary indexes,
and an existing, consistent offline backup; never point it at a live WAL database.

For the newer complete-runtime results, see the
[full-collection rehearsal](postgresql-runtime.md#full-collection-runtime-and-restore-rehearsal).

On 2026-11-17, one local run used the same schema 7 backup for both databases,
adding the schema 8 index to the SQLite copy. It contained 836 references and
56,816,420 landmarks. PostgreSQL 17.12 ran in Docker Desktop; SQLite 3.53.4 ran
natively on the ARM64 macOS host. The five approximately six-second samples came directly
from published submissions; they were not microphone captures.

| Measurement | SQLite | PostgreSQL |
| --- | ---: | ---: |
| Whole database: occupied SQLite pages / PostgreSQL database size | 3.186 GiB | 7.075 GiB |
| Landmark table and its indexes | 2.333 GiB | 6.182 GiB |
| Median warm candidate query, 15 runs | 72.21 ms | 263.26 ms |

All imported table row counts and the candidate multisets for all five samples
matched. The PostgreSQL import and index construction took 104 seconds, excluding
creation of the SQLite copy and its index. PostgreSQL's database size includes
system catalogs; SQLite's occupied pages exclude reusable pages. Neither figure
includes the backup, WAL or temporary build space.

```sh
uv run --no-sync --with 'psycopg[binary]>=3.2,<4' \
  python scripts/postgres_benchmark.py /path/to/offline-backup.db
```

Candidate-query timings do not include the full recognition pipeline, concurrent
load, cache-cold performance or a hosted database's network latency. Exact match
results are checked only for the chosen samples. Row counts do not prove that
every imported value is identical, and the script does not migrate or validate a
production installation.

## PostgreSQL layout and migration investigation

`scripts/postgres_layout_experiment.py` separately imports the backup into the
baseline PostgreSQL layout and two experimental alternatives: one represents each
80-bit hash as two reversible signed integers; the other retains the hash and
uses 32-bit internal reference IDs. All retain the unique landmark key, the
reference lookup index and the foreign key. It reports table/index sizes and
analyzed plans before and after `VACUUM`, then compares sampled candidate
multisets and warm timings. The 32-bit alternative rejects IDs outside its range;
it would impose a hard long-term limit. This is an experiment, not a change to
the stored fingerprint format or a production-ready migration.

On 2026-11-17 the same 56,816,420-landmark backup was tested with five samples
and three timed repetitions per sample, locally on PostgreSQL 17.12. All three
layouts returned the same candidate multisets for these samples:

| Landmark layout, including both indexes | PostgreSQL size | Median warm query |
| --- | ---: | ---: |
| 10-byte `bytea` hash, 64-bit reference ID | 6.183 GiB | 69.55 ms |
| Two integer hash columns, 64-bit reference ID | 6.182 GiB | 2,246.84 ms |
| 10-byte `bytea` hash, 32-bit reference ID | 5.275 GiB | 53.38 ms |

These are table-and-index sizes, not whole databases; the 32-bit layout saved
about 14.7% for landmarks in this run. The signed 32-bit range is a permanent
capacity limit unless a future migration expands it. All timings include local
query result transfer and Python decoding, use a fixed query order and warm
caches, and are not a concurrent-load benchmark.

```mermaid
xychart-beta
    title "56.8 million landmarks: PostgreSQL table plus indexes"
    x-axis ["bytea + int64 ID", "split hash + int64 ID", "bytea + int32 ID"]
    y-axis "GiB" 0 --> 7
    bar [6.183, 6.182, 5.275]
```

The smaller int32 layout was not selected: its size benefit introduces an
additional permanent capacity limit, and this one experiment is insufficient
to select a production representation.

The first baseline plan used an index-only scan but still performed 15,618 heap
fetches. After `VACUUM (ANALYZE)`, the sampled plan had zero heap fetches and a
hash join for published references. The individual plan execution times are not
a controlled vacuum speed comparison because the first run also read cold pages.
For the two-integer hash, the sampled planner estimated one index row per probe,
while observing approximately 31; it chose a reference-index nested loop with
16,967 lookups. That poor estimate and plan coincided with the much slower
measurement; other statistics and query shapes were not exhaustively tested.
The 32-bit reference layout retained an index-only scan and hash join for the
sample. Do not select any production layout from this single experiment alone.

```sh
uv run --no-sync --with 'psycopg[binary]>=3.2,<4' \
  python scripts/postgres_layout_experiment.py /path/to/offline-backup.db
```
