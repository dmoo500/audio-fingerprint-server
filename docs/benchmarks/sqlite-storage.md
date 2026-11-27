# SQLite storage benchmarks

[Home](../../README.md) · [Documentation](../README.md) · [Benchmark overview](../benchmarks.md)

## Database storage

Schema 2 replaced hexadecimal TEXT hashes with ten-byte BLOBs, introduced internal integer reference IDs and used a `WITHOUT ROWID` landmark table. Public reference keys, acoustic hashes and offsets were preserved. The query-first indexed join avoided the full landmark scan seen in the initial compact-layout experiment.

Schema 11 stores each distinct ten-byte hash once and uses integer IDs in its
postings and reference index. Previously, the same hash was stored with each
landmark occurrence and repeated in the reference index. The shared dictionary
replaces these repeated hashes with smaller integer IDs; it adds its own table
and index, so the saving depends on how often hashes recur. All hashes, reference
links and offsets are retained exactly: this does not remove fingerprints or
merge recordings.

In the 2026-11-27 backup, 265,109,078 landmark occurrences share 17,659,127
distinct hashes, about 15 occurrences per hash on average. In a separate local
prototype using a snapshot with 236,051,356 landmarks and 16,885,292 distinct
hashes, the landmark tables and indexes occupied 10,960,461,824 bytes before and
8,044,195,840 bytes after (26.6% less). This is one collection, not a guaranteed
saving. The prototype was vacuumed before its physical file size was measured;
an automatic upgrade does not vacuum and therefore does not immediately shrink
the file.

```mermaid
xychart-beta
    title "Landmark tables and indexes: local schema 11 prototype"
    x-axis ["Before schema 11", "Schema 11"]
    y-axis "Occupied GiB" 0 --> 12
    bar [10.21, 7.49]
```

A separate 15-minute local query benchmark compared 48 real landmark windows
against both complete layouts. Median SQLite candidate retrieval was 448 ms
before and 424 ms after; all candidate rows matched. This measured retrieval
and reading, not the whole recognition request, and the shared machine and OS
cache affected timings. It is not a production latency or capacity guarantee.

| Measurement | Before | After | Interpretation |
| --- | ---: | ---: | --- |
| Generated v2 migration fixture, 71,954 landmarks: active SQLite pages | 16.23 MiB | 3.54 MiB | About 78% less active space for this fixture |
| Deployed database after schema 2 migration: physical file | Not a controlled comparison | 4,502.23 MiB | File still includes reusable pages; no `VACUUM` performed |
| Same deployed database: active / reusable space | Not a controlled comparison | 893.57 / 3,608.66 MiB | Freed pages can be reused without shrinking the file |

```mermaid
xychart-beta
    title "Migration fixture: active SQLite space"
    x-axis ["Before schema 2", "After schema 2"]
    y-axis "MiB" 0 --> 18
    bar [16.23, 3.54]
```

The deployed snapshot contained 274 references. It is a historical snapshot, not the current database size or a projection per song. An earlier upper-limit synthetic baseline with 36 MB JSON and one million landmarks occupied about 238.3 MiB; it was not a before/after measurement of the deployed collection. Schema 3 subsequently compacted stored submission documents; no isolated schema 3 space-saving percentage was measured. Transactional upgrades, pre-upgrade backups including WAL data, and rejection of unsupported layouts are shipped safeguards, not speed or size improvements. None of these upgrades resets the collection.

## Deployed SQLite storage snapshots

Snapshots 1–3 are read-only SQLite measurements using schema 7. They illustrate
how the database grew as references and landmarks were added. They are an example
of possible growth, not a forecast: future storage needs depend on the contributed
recordings. These measurements predate the schema 8 reference index and do not
represent PostgreSQL.

The chart uses a separate vertical scale for each measurement; line slopes across
different scales are not directly comparable.

![SQLite DB Growth: occupied space, stored references, and stored landmarks](../sqlite-db-growth.svg)

The chart now also includes the schema 8 pre-PostgreSQL backup (3,299.09 MiB,
845 stored references, 57,373,098 landmarks) and the verified SQLite return
export from the isolated compact-runtime rehearsal (3,336.75 MiB file size, 846 references,
57,377,988 landmarks). Snapshot 5 includes synthetic test changes and was
never the live database.
The return point is a newly created file whose occupied pages were not separately
measured; its size uses a hollow marker and dashed segment, not a claim that file
size equals occupied pages. The early schema 7 points predate a secondary index, so the change in size is
not solely collection growth. The chart uses **all stored references**, including
unpublished ones; the earlier version of this diagram displayed published counts.

Snapshot 6 measures the live SQLite database after the return from PostgreSQL:
**6,673.21 MiB occupied**, **1,714 stored references** and **116,999,128 landmarks**.
The main database measured 6,674.29 MiB including reusable pages, excluding WAL.

Snapshot 7 measures a consistent SQLite backup from 2026-11-27 after the schema 11
upgrade: **12,111.45 MiB occupied**, **3,852 stored references** and
**265,109,078 landmarks**, with 17,659,127 distinct hashes. The backup file was
22,250.43 MiB, including 10,138.97 MiB of reusable pages; its file size excludes
the live database's WAL. The backup also contained 3,889 submissions. The
collection changed between snapshots 6 and 7, so their sizes do not measure the
saving from the schema upgrade. The reusable pages are not lost data or extra
space returned to the operating system: SQLite keeps them in the file for future
writes. The migration does not run `VACUUM` to shrink that file.

Occupied space rose from 1,418.54 to 2,111.26 MiB; published references from 460 to 715;
and stored landmarks from 38,637,618 to 56,424,148. Stored acoustic references, including
unpublished ones, numbered 575, 670 and 830. Published references had 426, 521 and 680
distinct verified recording identities, respectively, and 32, 32 and 33 references without
a verified identity.

The occupied-page figure excludes reusable free pages, but includes all database tables and indexes, not just published fingerprints. It is measured as `(page_count - freelist_count) * page_size` using SQLite's read-only `PRAGMA` queries.

This is a real deployment example, not a per-song storage guarantee. Track duration, landmark density, multiple acoustic variants of the same recording, pending submissions and cached metadata all affect storage. Published references without verified identities cannot safely be deduplicated into logical recordings, so the reference count must not be interpreted as a count of unique songs.

Snapshot values and provenance for both database charts live in
[`database-growth.json`](../database-growth.json). Add measured points there and
run `python scripts/render_database_growth.py` to regenerate
`sqlite-db-growth.svg` and `psql-db-growth.svg`. Keep occupied SQLite pages
separate from physical file size; see the [PostgreSQL chart](postgresql-runtime.md#database-growth)
for its different size definition.
