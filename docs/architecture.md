# Architecture and data flow

[Home](../README.md) · [Documentation](README.md)

## How it works

There are two separate paths: contributors build the reference collection by submitting fingerprints; listeners query that collection with a short microphone capture. Recognition does **not** add the microphone recording to the collection or learn an unknown song. The server does not crawl a music catalogue or fetch reference audio itself.

### 1. Read reference audio and submit fingerprints

Reference extraction happens on the producer side, not in this server. The compatible MA analysis integration must be installed and sharing enabled; this project does not install that integration into MA.

```mermaid
flowchart TD
    A["Compatible producer: local reference analysis in MA"] --> B["Shared fingerprint library: decode PCM and resample to 44.1 kHz mono"]
    B --> C["Process the whole track in overlapping hop-aligned windows of at most 30 seconds"]
    C --> D["Spectrogram peaks: hash frequency pairs and keep absolute FFT-hop offsets"]
    D --> E["JSON: format, windows, fingerprints and allowlisted metadata"]
    E --> F["POST submissions: upload key and canonical document SHA-256"]
    F --> G["Server: authenticate, enforce limits and validate document and coverage"]
    G --> H["Commit submission: queue eligible work or retain pending_metadata"]
    H --> I["HTTP 202: accepted, not yet searchable"]
```

The shared `audio-fingerprint` library implements window sampling and landmark extraction. A landmark is a frequency-pair hash plus a time offset, not an audio file or a title-based identifier. New v2 windows cover the entire decoded track; their approximately ten-second overlap protects context at section boundaries. Old v1 references still have unsampled gaps. Existing private analyses are not automatically uploaded. Repeated identical documents reuse their submission ID; already processed ones return HTTP 200 instead of adding queue work.

### 2. Process the queue and publish a reference

```mermaid
flowchart TD
    A["Durable queue: queued submission eligible for processing"] --> B["Worker claims entry: processing, attempts incremented"]
    B --> C["Validate stored document again"]
    C --> D{"MusicBrainz enabled?"}
    D -->|Yes| E["Verify recording MBID and/or ISRC with paced cached lookups"]
    E -->|Unresolved| M["Durable pending_metadata: not published; scheduled bounded rechecks"]
    E -->|Missing identifiers| P["Housekeeping enabled: delete submission and unused unpublished reference"]
    E -->|Conflicting identifiers| O["Housekeeping enabled: delete submission only and log reason"]
    M -->|Housekeeping enabled and deadline reached| A
    M -->|Three unresolved rechecks by default| O
    E -->|Verified| F["Fill only missing unambiguous metadata"]
    D -->|No| L{"Metadata complete?"}
    F --> L
    L -->|No| M
    L -->|Yes| G["Compute acoustic reference ID from format, windows and sorted fingerprints"]
    G --> H["One transaction: insert reference and landmarks if new; mark submission processed"]
    H --> I["Reference is searchable; exact acoustic duplicates reuse existing reference"]
    C -->|Invalid document| J["Terminal failed state with explicit error"]
    E -->|Malformed response| M
    E -->|HTTP 429, 502 or 503| N["Service-wide cooldown; defer without consuming an attempt"]
    N -->|Retry due| A
    E -->|Network failure or timeout| K["Delayed retry; failed after three attempts"]
    K -->|Retry due| A
```

The submission ID covers the **whole document**, including metadata. The reference ID covers the **acoustic content and coverage**, excluding metadata. Multiple submissions can therefore point to one reference, and a later duplicate does not overwrite the first published complete metadata. An exact acoustic duplicate with complete metadata can make an excluded legacy reference searchable, without deleting its landmarks or original submission. Compact and legacy encodings of the same canonical document share an ID. Different acoustic formats or sampled passages are not automatically merged into one song.

Publication requires a nonblank title, at least one nonblank artist, a nonblank album, a positive finite duration, and either a valid ISRC or a recording UUID. Whitespace is checked but supplied metadata is not trimmed or rewritten. With MusicBrainz disabled, incomplete documents remain `pending_metadata`; with it enabled, documents with an identifier are checked before completeness and missing fields can be filled from an unambiguous result. Unresolved identifiers and missing required metadata remain pending with a reason and are not published; enabled housekeeping retries or removes them according to the [housekeeping policy](metadata.md#pending-metadata-housekeeping), and removes definitively conflicting identifiers. A missing duration defers the exact track-end coverage check, but window, offset, duration and upload limits still apply; duration is not inferred from MusicBrainz. To correct metadata, submit a corrected document; its whole-document submission ID is different and the original document follows the housekeeping policy independently.

Publishing is atomic: recognition cannot see a half-inserted reference. On worker startup, interrupted queued work retains its FIFO position and background classification without spending a completed recheck. An interrupted manual pending check returns to `pending_metadata`. An unexpected worker crash is reported and makes the health check fail rather than pretending ingestion still works. SQLite runs API and worker together. PostgreSQL supports separate roles and [opt-in API replicas](operations/replicas.md) with shared request/concurrency budgets; ingestion ownership remains exclusive. PostgreSQL API startup never recovers another process's jobs. Multiple workers are always rejected.

### 3. Recognize a microphone capture

```mermaid
flowchart TD
    A["Browser: read key and microphone permission"] --> B["Capture ten seconds of mono PCM in memory"]
    B --> C["Stop microphone tracks; encode little-endian float32 PCM"]
    C --> D["POST recognize: read key, sample rate and channel count"]
    D --> E["Server: authenticate, enforce limits, decode PCM and resample"]
    E --> F["Shared library: extract query landmarks in memory"]
    F --> G["SQLite or PostgreSQL: join query hashes to indexed reference landmarks"]
    G --> H["Group votes by reference and consistent time-offset difference"]
    H --> I["Rank candidates; check match count, fraction, time span and separation"]
    I --> J["matched with reference metadata, no_match or ambiguous"]
    J --> K["Browser displays result; no capture or query is persisted"]
```

For each matching hash, the matcher subtracts the query offset from the reference offset. Many distinct landmarks agreeing on that difference support the same alignment within a track. The server performs this database lookup and ranking; the shared library only supplies the acoustic extraction. Weak or insufficiently separated candidates do not produce a match. Invalid input, overload and excessive candidate counts return explicit HTTP errors, not `no_match`.

Only indexed passages can contribute matching landmarks. MusicBrainz enrichment adds metadata, not fingerprints, and cannot make an unindexed song recognizable.

## Database tables

This is a recognition index with a durable ingestion queue, not a full music-library database. It does not maintain separate artist, album, user, audio-file or recognition history tables. Much of the structured input is retained as JSON:

| Persistent table | Contents | Purpose |
| --- | --- | --- |
| `submissions` | Legacy or compact JSON, queue state, attempts, retry time, error, published reference ID, FIFO admission time and background-check lifecycle | Durable processing, duplicate upload handling, bounded background admission and status; JSON includes analysis windows and supplied metadata |
| `reference_tracks` | Internal integer ID, public acoustic reference key, metadata JSON, creation time, publication readiness, verification version and optional verified recording key | Retained acoustic variants; verified recording keys group logical recordings without merging their fingerprints |
| `landmarks` | Ten-byte binary hash, internal reference ID and absolute offset in FFT hops | Compact searchable acoustic index; potentially many rows per reference |
| `enrichment_cache` | Versioned endpoint and identifier key, lookup result JSON and expiry | Cache successful, empty and ambiguous MusicBrainz results; normally empty when enrichment is disabled |
| `service_state` | Configuration transition markers | Keep enable and disable transitions durable and idempotent |

```mermaid
erDiagram
    reference_tracks ||--o{ landmarks : contains
    reference_tracks |o--o{ submissions : "logically linked after processing"
    submissions {
        TEXT id PK
        TEXT document
        TEXT state
        TEXT reference_id
        REAL enqueued_at
        INTEGER background_kind
        INTEGER pending_rechecks
        REAL pending_due_at
        INTEGER pending_check
    }
    reference_tracks {
        INTEGER id PK
        TEXT reference_key UK
        TEXT metadata
        TEXT recording_key
    }
    landmarks {
        BLOB hash PK
        INTEGER reference_id PK, FK
        INTEGER offset PK
    }
    enrichment_cache {
        TEXT isrc PK
        TEXT document
        REAL expires_at
    }
    service_state {
        TEXT key PK
        TEXT value
    }
```

Only `landmarks.reference_id` has a database-enforced foreign key; the submission link is set by the worker and retains the public acoustic key, not the internal integer ID. The landmark primary key is the combination of hash, reference and offset. In SQLite, the `WITHOUT ROWID` table stores that key once, and the query explicitly keeps the small query table outermost. PostgreSQL uses a heap plus a hash-first B-tree primary key and its planner selects the join strategy. Both backends index landmarks by reference for deletion and store hashes losslessly as ten bytes rather than twenty hexadecimal characters; upload documents and API reference IDs remain unchanged. Queue selection has indexes for state and due work. PostgreSQL uses bigint internal reference IDs and an explicit `queue_order` identity in place of SQLite's rowid FIFO tie-breaker. Its additional `migration_info` table records PostgreSQL format 1 and importer source-version provenance; transient process tokens and heartbeats use `service_state`.

Recognition creates a connection-local temporary `query` table for the hash join; it is not persistent and disappears when the connection closes. Both backends use the same Python ranking, coverage and identifier-resolution decisions. Coverage statistics are derived from submitted windows and reference duration rather than stored in another table. SQLite WAL/SHM files are database support files, not tables. Table count therefore says little about collection size: count references and landmark rows separately. Processed submissions and published references are not automatically pruned; unused unpublished references can be removed only by the documented missing-identifier housekeeping.
