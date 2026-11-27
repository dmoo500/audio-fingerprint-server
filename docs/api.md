# HTTP API

[Home](../README.md) · [Documentation](README.md)

## API

### Release hints on uploads

When upload capabilities advertise `release_hints_version: 1`, a submission's
`metadata` may include the MusicBrainz UUIDs `release_mbid` (an exact edition)
and `release_group_mbid` (the group of editions). Omit them on older receivers.
Use identifiers already available from the source; clients do not need to
query MusicBrainz. Include the fields in the canonical document before
computing its `Idempotency-Key`. The landmark format is unchanged.

A supplied Recording MBID still takes the direct verification path. When the
worker falls back to searching by track metadata, it tries the release scope
first and checks the association on the recording before using it. Missing,
incomplete or conflicting evidence falls back to ordinary matching and review.
Release hints do not replace the submitted album or become recording identity.
Update all replicas behind a shared gateway before using the capability.

### Availability

`POST /api/v1/dejavu/availability` requires upload credentials (and the application key in gateway mode). Send JSON `{"version":1,"items":[{"index":0,"format_id":"ma-dejavu-landmarks-v2","recording_mbid":"e9c2a2a3-4f4b-4b6a-9c31-3a8b2f3c7f10"}]}` with at most 100 items and 128,000 bytes. Items may also contain `isrc`, `title`, `artists`, `duration`, and `album`; an ISRC-only lookup needs title, artists and duration to select a matching stored recording.

The ordered response is `{"version":1,"items":[{"index":0,"status":"full","recording_mbid":"e9c2a2a3-4f4b-4b6a-9c31-3a8b2f3c7f10"}]}`. Status is `full`, `partial`, or `unknown`; unknown has a null recording MBID. Only published verified references with compatible landmark formats can be full. A supplied MBID and ISRC must both match the stored identity; a missing or ambiguous identity is unknown. This is a point-in-time hint, not an upload acknowledgement. The capabilities endpoint advertises support as `availability_version: 1`; absence means the feature is unavailable.

### Client-side landmark recognition

Clients such as a future recognize plugin can extract a short capture locally
and send only landmarks to `POST /api/v1/dejavu/recognize/landmarks`. This does
not upload a reference, publish anything or retain audio/query landmarks.
No `Idempotency-Key` or track metadata is needed. The plugin itself is not
implemented by this endpoint.

Normalize the capture to mono 44,100 Hz and extract with the compatible
`audio_fingerprint.fingerprint(samples)` algorithm, using `start_sample=0`.
Do not take absolute offsets from an existing full-track analysis: offsets
must be relative to the beginning of this capture. `sample_count` is the
normalized capture's actual sample count, between 132,300 and 661,500 inclusive
(3–15 seconds).

```json
{
  "version": 1,
  "format_id": "ma-dejavu-landmarks-v2",
  "sample_rate": 44100,
  "offset_unit": "fft_hops",
  "sample_count": 220500,
  "fingerprints": [
    ["0123456789abcdef0123", 0],
    ["abcdef0123456789abcd", 12]
  ]
}
```

The pairs above illustrate the wire shape, not a recognisable recording.
Each pair contains a lowercase 80-bit hex hash and an integer anchor offset
in 2,048-sample hops. Pairs must be distinct; offsets must be nonnegative and
no larger than `(sample_count - 4096) // 2048`. Legacy
`ma-dejavu-landmarks-v1` hashes are also supported because the extractor is
compatible. This query accepts JSON pairs only, not compact reference-document
encoding or reference windows. Unknown fields and mismatched formats are rejected.

Use the read key in direct mode, or a read/both personal key **and** the
application key through APISIX. The server validates credentials before
reading the body. Both recognition endpoints together have a backend-wide
30-request rolling-minute budget and two recognition slots, shared across
PostgreSQL API replicas. Gateway route quotas also apply.

The response is exactly the PCM recognition response:
`{"status":"matched","match":{...}}`, `{"status":"no_match","match":null}`
or `{"status":"ambiguous","match":null}`. A matched result includes metadata,
reference ID, offset in seconds, matched/query landmark counts, fraction and
matched span; these scores are not calibrated probabilities. An empty landmark
list returns `no_match` with reason `No acoustic landmarks`. It is never a
successful result when matching fails: excessive candidate expansion returns
503, invalid capture/query fields return 422, oversized requests 413 and
exhausted rate/concurrency limits 429.

Upload-authenticated capabilities advertise `recognition_landmarks_version: 1`.
Read-only clients can use the published OpenAPI contract; they do not gain access
to the upload-only capabilities endpoint. Reapply gateway `configure` after
upgrading to install the new route without replacing existing consumers.

### OpenAPI and generated clients

The canonical [OpenAPI 3.1 specification](../openapi.json) describes the APISIX deployment, including concrete responses, both submission encodings, raw PCM bodies and the two required authentication headers. Use it with an OpenAPI 3.1-capable client generator (such as OpenAPI Generator); pin the specification to a release or commit when generating your client. Acoustic coverage and canonical hashing constraints still require client-side implementation; generated models alone cannot enforce the entire fingerprint contract. Statistics now add a compatible logical-recording count alongside the existing acoustic-reference count; the endpoint remains `/api/v1`.

Generate or refresh the gateway artifact from the repository root with `python -m fingerprint_server.openapi`. For a direct deployment, use `python -m fingerprint_server.openapi --legacy --output openapi-legacy.json` instead; legacy read/upload keys are not the gateway's personal keys. The exporter starts no workers, opens no database, contacts no services and requires no credentials. The committed artifact is checked against generation by `pytest -q tests/test_openapi.py` and the OpenAPI workflow. Regenerate it after API or schema-generation dependency updates.

FastAPI's backend `/openapi.json`, `/docs` and `/redoc` endpoints describe the running deployment's authentication. APISIX does not expose these endpoints, and this work does not change gateway routing or CORS. Gateway security is an AND requirement: `X-API-Key` and `X-Application-Key` belong to one security requirement object, not two alternative objects. Gateway errors may differ from backend error models; treat HTTP status and optional `Retry-After` independently of an error body's shape.

A self-contained, searchable API reference is prepared in [docs/reference](reference). It uses only bundled HTML, CSS and JavaScript with no CDN, analytics or remote validator, and does not execute API requests or collect credentials. The [Pages workflow](../.github/workflows/api-docs.yaml) publishes only those assets and the specification. Enable GitHub Pages with **GitHub Actions** as the source in repository Settings → Pages before publishing the workflow; once deployed, the reference is available at `https://dmoo500.github.io/audio-fingerprint-server/`. GitHub's repository viewer shows JSON as source, not as an interactive OpenAPI page. To preview locally, copy the three reference assets and `openapi.json` into a temporary directory and serve it with `python -m http.server`; all documentation commands otherwise assume the repository root.

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
  With the optional `include_candidates=true`, an `ambiguous` response also includes
  `candidates`, containing at most two individually qualified entries with `metadata`,
  `reference_id` and `match_fraction`. This fraction is the proportion of query landmarks matching a consistent
  offset, not the probability that a recording is correct. Without the option, responses are
  unchanged. `no_match` does not return candidates. Deploy the updated fingerprint API to all
  replicas before updating the recognition app, which requests this option.
- `POST /api/v1/dejavu/recognize/landmarks`: read-authenticated JSON query with
  precomputed capture-relative landmarks, at most 10,000 pairs and 512,000 bytes.
  Uses the same recognition result, thresholds, backend request budget and
  concurrency slots as PCM; see [client-side extraction](#client-side-landmark-recognition).
- `GET /health`: 503 if the ingestion worker has stopped.
- `GET /api/v1/dejavu/statistics`: read-key authenticated counts of logical recordings,
  acoustic references (including retained variants), uploads by queue state including
  `pending_metadata`, and worker status. References are grouped as one recording only when
  their recording MBID was verified by the current MusicBrainz verification version; uncertain
  or unverified references remain separate. A recording is full if any searchable variant is
  fully covered, partial if it has searchable variants but none is full, and excluded only if
  none of its variants is searchable. The [recognition app](operations/recognition.md) uses
  only the searchable recording count.

Coverage is computed from stored windows rather than a separate stored coverage flag. Full coverage means the union of windows starts at zero, has no gaps and reaches the exact track duration in samples; even small unsampled gaps count as partial. This describes sampled audio, not guaranteed recognition of every passage. New v2 references have full coverage; legacy references usually have partial coverage because only three windows were analyzed. Different acoustic references with the same verified recording MBID count as one logical recording but remain separate acoustic variants. References without a verified MBID count separately; matching titles, artists, durations or ISRCs alone never merge records. Duplicate uploads of an identical reference still count once. Queue counts describe submissions, not recordings or analyses still running inside MA before upload.

Reference hashes and offsets are validated against hop-aligned coverage. V2 requires complete coverage and permits overlap; legacy v1 windows must not overlap. Only the worker can publish them. Exact acoustic submissions deduplicate even when metadata differs; original supplied metadata is retained rather than overwritten by a later conflicting contribution. Acoustic deduplication here means identical landmarks/coverage, **not** merging all different encodings or partial recordings. An excluded reference can receive complete supplied metadata from an exact duplicate; already complete reference metadata is never overwritten. Logical recording grouping does not remove acoustic variants or reduce database storage automatically.

Submissions are limited to 60/minute; recognition and status reads use their respective credential scopes (30 read requests/minute). Two recognition computations can run at once. Statistics have a separate 30 requests/minute budget, so refreshes do not consume recognition requests. Queries with excessive candidate counts fail explicitly rather than returning a misleading match; ordinary music can also reach this limit. The queue is bounded by submission count and by 256,000,000 bytes of queued, processing and pending-metadata stored JSON (`FP_QUEUE_BYTES_LIMIT`); full queues return 503 with Retry-After. This is a private pilot, not a hardened anonymous public contribution service.

### Upgrade from sampled references

Deploy this receiver before enabling the full-track MA provider or updating its local fingerprint library to 0.2.2. The provider increments its analysis version to 2, making old analyses eligible for replacement on the next normal full playback or an existing filesystem-only scan. It does not fetch streaming-provider audio for backfill, redirect old uploads, or retroactively upload private analyses.

Existing server references and pending v1 submissions remain valid and are not deleted. The current storage upgrade preserves them without changing their fingerprint format. They do not become fully covered just by upgrading. Statistics show both acoustic-reference variants and logical recordings; verified old partial and new full references for one recording count as one full logical recording while both remain searchable. Matching ranks each acoustic reference independently, then returns only the strongest variant per verified recording ID so variants do not hide a second distinct recording or add their scores together. For legacy references without a verified recording ID, strongly overlapping and equally aligned references with identical nonempty metadata may still be treated as one recognition candidate.

Full analysis costs more CPU and storage than sparse sampling. MA uses its existing low-priority bounded analysis pool and playback-active serialization; overloaded analysis can be cancelled rather than reported as complete. There is no guarantee of zero performance impact. The rolling PCM buffer is bounded, but returned fingerprints accumulate until publication. Validate latency and memory on the target hardware before enabling many simultaneous analyses.
