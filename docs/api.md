# HTTP API

[Home](../README.md) · [Documentation](README.md)

## API

### Availability

`POST /api/v1/dejavu/availability` requires upload credentials (and the application key in gateway mode). Send JSON `{"version":1,"items":[{"index":0,"format_id":"ma-dejavu-landmarks-v2","recording_mbid":"e9c2a2a3-4f4b-4b6a-9c31-3a8b2f3c7f10"}]}` with at most 100 items and 128,000 bytes. Items may also contain `isrc`, `title`, `artists`, `duration`, and `album`; an ISRC-only lookup needs title, artists and duration to select a matching stored recording.

The ordered response is `{"version":1,"items":[{"index":0,"status":"full","recording_mbid":"e9c2a2a3-4f4b-4b6a-9c31-3a8b2f3c7f10"}]}`. Status is `full`, `partial`, or `unknown`; unknown has a null recording MBID. Only published verified references with compatible landmark formats can be full. A supplied MBID and ISRC must both match the stored identity; a missing or ambiguous identity is unknown. This is a point-in-time hint, not an upload acknowledgement. The capabilities endpoint advertises support as `availability_version: 1`; absence means the feature is unavailable.

### OpenAPI and generated clients

The canonical [OpenAPI 3.1 specification](../openapi.json) describes the APISIX deployment, including concrete responses, both submission encodings, raw PCM bodies and the two required authentication headers. Use it with an OpenAPI 3.1-capable client generator (such as OpenAPI Generator); pin the specification to a release or commit when generating your client. Acoustic coverage and canonical hashing constraints still require client-side implementation; generated models alone cannot enforce the entire fingerprint contract. Statistics now add a compatible logical-recording count alongside the existing acoustic-reference count; the endpoint remains `/api/v1`.

Generate or refresh the gateway artifact from the repository root with `python -m fingerprint_server.openapi`. For a direct deployment, use `python -m fingerprint_server.openapi --legacy --output openapi-legacy.json` instead; legacy read/upload keys are not the gateway's personal keys. The exporter starts no workers, opens no database, contacts no services and requires no credentials. The committed artifact is checked against generation by `pytest -q tests/test_openapi.py` and the OpenAPI workflow. Regenerate it after API or schema-generation dependency updates.

FastAPI's backend `/openapi.json`, `/docs` and `/redoc` endpoints describe the running deployment's authentication. APISIX does not expose these endpoints, and this work does not change gateway routing or CORS. Gateway security is an AND requirement: `X-API-Key` and `X-Application-Key` belong to one security requirement object, not two alternative objects. Gateway errors may differ from backend error models; treat HTTP status and optional `Retry-After` independently of an error body's shape.

A self-contained, searchable API reference is prepared in [docs/reference](reference/). It uses only bundled HTML, CSS and JavaScript with no CDN, analytics or remote validator, and does not execute API requests or collect credentials. The [Pages workflow](../.github/workflows/api-docs.yaml) publishes only those assets and the specification. Enable GitHub Pages with **GitHub Actions** as the source in repository Settings → Pages before publishing the workflow; once deployed, the reference is available at `https://dmoo500.github.io/audio-fingerprint-server/`. GitHub's repository viewer shows JSON as source, not as an interactive OpenAPI page. To preview locally, copy the three reference assets and `openapi.json` into a temporary directory and serve it with `python -m http.server`; all documentation commands otherwise assume the repository root.

### Endpoints

All mutation/status calls require the upload key; recognition requires the read key in `X-API-Key` in legacy mode. In the optional gateway mode, every protected request instead carries a personal key in `X-API-Key` **and** the fingerprint application's token in `X-Application-Key`. Keys must differ and each contain at least 24 characters.

- `POST /api/v1/dejavu/submissions`: v2 JSON document, max 36,000,000 bytes and
  1,000,000 landmarks, up to 30 minutes of complete coverage. Legacy v1 documents
  retain their 2,000,000-byte / 100,000-landmark / three-window limits.
  Legacy pairs and compact `sha1-80-offset-delta-v1` documents are accepted.
  Size limits apply to the upload and decoded canonical document, so packing does not
  bypass limits. `Idempotency-Key` is the SHA-256 of **decoded canonical JSON**, without
  `fingerprints_encoding` (`sort_keys=True`, compact separators, Python's default ASCII
  escaping). HTTP 202 returns the durable state (`queued`,
  `processing` or `pending_metadata`), not a publication acknowledgement.
  HTTP 200 `processed` acknowledges an already published duplicate.
- `GET /api/v1/dejavu/submissions/{id}`: processing state, attempts, explicit failure
  and published reference ID. A terminally failed repeated submission returns HTTP 409.
- `GET /api/v1/dejavu/capabilities`: upload-authenticated supported API major,
  acoustic formats, landmark encodings and document-size limit. Deploy the updated receiver
  and reconfigure gateway routes before updating the producer to library 0.3.0.
  This compatible addition keeps `/api/v1`; an old receiver is explicitly rejected by
  the compact producer, not silently treated as supporting the codec.
- `POST /api/v1/dejavu/recognize?sample_rate=48000&channels=1`:
  `application/octet-stream`, little-endian float32 PCM, 3 to 15 seconds,
  8 to 192 kHz, one or two channels. No audio file/container decoding.
  Returns `matched`, `no_match` or `ambiguous`; `match` is null unless matched.
- `GET /health`: 503 if the ingestion worker has stopped.
- `GET /api/v1/dejavu/statistics`: read-key authenticated counts of logical recordings,
  acoustic references (including retained variants), uploads by queue state including
  `pending_metadata`, and worker status. References are grouped as one recording only when
  their recording MBID was verified by the current MusicBrainz verification version; uncertain
  or unverified references remain separate. A recording is full if any searchable variant is
  fully covered, partial if it has searchable variants but none is full, and excluded only if
  none of its variants is searchable. The microphone page refreshes counts every 15 seconds
  while visible after entering the read key. Failed requests clear the displayed counts instead
  of presenting stale data as current.

Coverage is computed from stored windows rather than a separate stored coverage flag. Full coverage means the union of windows starts at zero, has no gaps and reaches the exact track duration in samples; even small unsampled gaps count as partial. This describes sampled audio, not guaranteed recognition of every passage. New v2 references have full coverage; legacy references usually have partial coverage because only three windows were analyzed. Different acoustic references with the same verified recording MBID count as one logical recording but remain separate acoustic variants. References without a verified MBID count separately; matching titles, artists, durations or ISRCs alone never merge records. Duplicate uploads of an identical reference still count once. Queue counts describe submissions, not recordings or analyses still running inside MA before upload.

Reference hashes and offsets are validated against hop-aligned coverage. V2 requires complete coverage and permits overlap; legacy v1 windows must not overlap. Only the worker can publish them. Exact acoustic submissions deduplicate even when metadata differs; original supplied metadata is retained rather than overwritten by a later conflicting contribution. Acoustic deduplication here means identical landmarks/coverage, **not** merging all different encodings or partial recordings. An excluded reference can receive complete supplied metadata from an exact duplicate; already complete reference metadata is never overwritten. Logical recording grouping does not remove acoustic variants or reduce database storage automatically.

Submissions are limited to 60/minute; recognition and status reads use their respective credential scopes (30 read requests/minute). Two recognition computations can run at once. Statistics have a separate 30 requests/minute budget, so refreshes do not consume recognition requests. Repetitive queries with excessive candidate counts fail explicitly rather than returning a misleading match. The queue is bounded by submission count and by 256,000,000 bytes of queued, processing and pending-metadata stored JSON (`FP_QUEUE_BYTES_LIMIT`); full queues return 503 with Retry-After. This is a private pilot, not a hardened anonymous public contribution service.

### Upgrade from sampled references

Deploy this receiver before enabling the full-track MA provider or updating its local fingerprint library to 0.2.2. The provider increments its analysis version to 2, making old analyses eligible for replacement on the next normal full playback or an existing filesystem-only scan. It does not fetch streaming-provider audio for backfill, redirect old uploads, or retroactively upload private analyses.

Existing server references and pending v1 submissions remain valid and are not deleted. The current storage upgrade preserves them without changing their fingerprint format. They do not become fully covered just by upgrading. Statistics show both acoustic-reference variants and logical recordings; verified old partial and new full references for one recording count as one full logical recording while both remain searchable. Matching ranks each acoustic reference independently, then returns only the strongest variant per verified recording ID so variants do not hide a second distinct recording or add their scores together. For legacy references without a verified recording ID, strongly overlapping and equally aligned references with identical nonempty metadata may still be treated as one recognition candidate.

Full analysis costs more CPU and storage than sparse sampling. MA uses its existing low-priority bounded analysis pool and playback-active serialization; overloaded analysis can be cancelled rather than reported as complete. There is no guarantee of zero performance impact. The rolling PCM buffer is bounded, but returned fingerprints accumulate until publication. Validate latency and memory on the target hardware before enabling many simultaneous analyses.
