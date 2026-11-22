# MusicBrainz and pending metadata

[Home](../README.md) · [Documentation](README.md)

## Optional metadata enrichment

Availability checks never contact MusicBrainz: they use only already published references with a verified recording key. ISRC queries use the stored identity plus title, artist and duration matching; an ISRC-only reference without a verified MBID cannot be considered full. A check does not alter references, recordings, or statistics.

Set `FP_MUSICBRAINZ_ENABLED=true` and `FP_MUSICBRAINZ_CONTACT` to a suitable contact. `FP_MUSICBRAINZ_BASE_URL` defaults to `https://musicbrainz.org/ws/2`; set it to another MusicBrainz-compatible API base for a self-hosted instance, including any path prefix:

```sh
export FP_MUSICBRAINZ_ENABLED=true
export FP_MUSICBRAINZ_CONTACT="operator@example.org"
export FP_MUSICBRAINZ_BASE_URL="https://metadata.example.org/musicbrainz/ws/2"
```

The worker uses `/recording/{mbid}?inc=artists+isrcs+releases&fmt=json` and `/isrc/{isrc}?inc=artists+releases&fmt=json`. HTTP is supported for explicitly configured local instances; use HTTPS for remote endpoints. Embedded credentials, query strings and fragments in the base URL are rejected. Both Compose modes pass these settings through; recreate the service after changing them. Inside a container, `localhost` refers to that container, so use a hostname reachable from the backend. Cache keys include the API endpoint and response version, so older cache entries and results from a different endpoint are not reused.

The worker validates supplied recording MBIDs and ISRCs, checking their relationship when both are supplied. It never approves a recording by title alone. If the ISRC lookup is empty, or neither identifier was supplied, it can resolve a unique recording using the complete title, artist credits and duration as described below. Redirects are withheld rather than assumed valid. Lookups are paced at least 1.1 seconds apart and cached for seven days, including empty and ambiguous results; outages are not cached. HTTP 429, 502 and 503 pause the whole worker and defer the current submission without consuming an attempt. HTTP 503 starts with a 60-second pause; each consecutive HTTP 503 adds ten seconds, up to 15 minutes. An actual HTTP 200 or 404 resets the backoff, so the next HTTP 503 starts at 60 seconds again; cache hits do not reset it. HTTP 429/502 retain exponential backoff starting at 30 seconds, up to 15 minutes. A longer `Retry-After` delay always takes precedence. Other HTTP and network failures retain the three-attempt retry limit for initial processing; pending rechecks instead defer without consuming a completed check. Only missing metadata is filled. A missing album is filled only when the release list is complete, all releases have enough type information, and exactly one distinct album title remains after excluding singles and compilations. A supplied album is retained, and all release names remain usable as matching evidence. Missing or ambiguous albums stay pending. Existing reviews and stored references are not rewritten. Unresolved results follow the bounded pending lifecycle below. MusicBrainz validates identifiers and metadata, not that submitted fingerprints match the recording or that the audio is authentic.

MusicBrainz HTTP handling, identifier verification, cache access and service-wide pacing/backoff live in `fingerprint_server/musicbrainz.py`; the worker owns submission orchestration and publication. A supplied recording MBID can disambiguate several ISRC recordings, but must agree with a confirmed ISRC association in both lookup directions. For a complete ISRC list with multiple recordings, the first candidate matching the supplied title, an artist credit or available alias, and any available duration within eight seconds is selected. Name comparison approximates MA's loose comparison without depending on MA's helpers; this metadata heuristic does not verify the audio. If no candidate matches, the confirmed ISRC still permits publication when supplied metadata is complete, without assigning a recording MBID or grouping by ISRC. Missing metadata still leaves the submission pending, and incomplete lookup lists do not confirm the ISRC. Per the [MusicBrainz lookup contract](https://musicbrainz.org/doc/MusicBrainz_API#Lookups_for_other_unique_identifiers), ISRC lookup lists are unpaged and complete even without `recording-count`; a single result is not marked ambiguous just because that optional count is absent. Metadata-only resolution uses the separate, stricter checks below.

The shared cooldown is held in memory and resets on restart, but the current submission's delayed retry remains in the queue. Even submissions that could use cached results wait for the cooldown before being claimed. Logs distinguish service-wide cooldowns from per-submission failures, show retry delays in whole seconds and include the lookup method/path (recording MBID or ISRC), without credentials or response bodies. Successful verification and publication emits an INFO message; an HTTP 200 alone is not reported as successful publication. Submissions already marked failed by older versions are not automatically requeued.

 Existing pending submissions keep their housekeeping deadlines rather than being requeued immediately. References and landmarks are retained; successfully verified references become searchable again, while unresolved submissions follow housekeeping rules. A reference with no usable identifier or submission cannot be automatically verified. Disabling verification restores the offline completeness gate; re-enabling retains successful verification and queues newly unverified references rather than reprocessing the entire collection. Verification is disabled by default.

### Additional recording resolution

The worker previews up to 19 eligible submissions alongside the current job
without claiming them. For two or more submissions with at least two distinct
identifiers, it sends one escaped recording search combining exact `isrc` and
`rid` terms with `OR`, requesting at most 100 results. The batch shares the
existing pacing, cooldown and endpoint-versioned cache. Only complete search
responses provide positive evidence; empty or truncated responses fall back
to direct lookups and never mark individual identifiers absent.

MusicBrainz search results normally omit ISRC lists. The worker retains them
as candidates instead of discarding them: for an ISRC-only submission with
exactly one strict title/artist/duration match in the complete batch, it lazily
fetches that recording, using the existing cache and request pacing. Only if
the recording lookup confirms both the ISRC association and the same strict
metadata can it replace the direct ISRC lookup. Missing associations, changed
metadata and ambiguous candidates retain the ordinary direct verification;
upstream errors retain the shared cooldown and retry rules. Candidates are
never all fetched eagerly. This can save a separate metadata search or reuse a
recording already checked for another submission, but does not turn twenty
submissions into one authoritative lookup.

Exactly requested recording IDs with explicit ISRC lists can satisfy a
recording lookup. An ISRC-only submission can use a positive batch association
only with a unique strict title/artist/duration match under the rules below,
and sufficient supplied or returned album metadata. Otherwise normal direct
ISRC verification remains unchanged. Search results do not prove an exhaustive
ISRC-to-recording mapping; batches are an optimization, not a bulk authoritative
lookup API. Missing metadata may still require individual recording lookups.
Logs distinguish candidate counts from usable recording and ISRC evidence,
and report hydration confirmations and fallbacks.
Only one bounded batch of evidence is held in worker memory, and pending-pass
deletion/retry protections are unchanged. No database migration is needed.

When an ISRC has no MusicBrainz result, or the submission has no identifiers,
the worker can query `/recording` using escaped title and artist search terms.
Automatic assignment requires exactly one candidate from a **complete** result
set of at most 25 recordings, followed by a successful direct recording lookup.
Both the search candidate and direct lookup must have the same normalized full
title and artist-credit set as the submission, and an explicit duration within
two seconds. Normalization ignores case, accents, punctuation and spacing, but
does not drop words such as "live", "remix" or "edit". Nonempty MusicBrainz
disambiguation text is conservatively rejected on this path, even if the
recording might otherwise match. Missing durations, partial lists, conflicting
credits and multiple matching recordings stay pending.

The matched recording MBID can then fill missing metadata and enable publication.
This confirms a **metadata match**, not the supplied ISRC association or the
audio itself; the INFO log explicitly distinguishes that basis. Supplied metadata
and ISRCs are retained, and later verification of the recovered MBID uses the
same strict checks if its ISRC association is still missing. A supplied MBID that
does not exist, a conflicting confirmed ISRC association, or an upstream failure
does not trigger this fallback.

Searches share the existing worker lock, 1.1-second pacing, endpoint-specific
seven-day cache, bounded response parser and service-wide cooldowns. Cache
parser version 9 includes release classification for album selection and review hints; older cached
values are not reused. Existing pending deadlines, verification versions and stored submissions
are not migrated or automatically requeued. The normal pending recheck or
explicit operator pass can retry them. No third-party metadata service is
contacted by this fallback.

[AcoustID's public API](https://acoustid.org/webservice) supports lookup by
Chromaprint fingerprint or AcoustID track ID, and
`/v2/track/list_by_mbid` lists AcoustIDs associated with MusicBrainz recording
IDs. It does **not document a direct ISRC search**. A response can contain ISRCs
without offering ISRC as a search input. The MBID reverse lookup does not solve
missing MBIDs, and Dejavu landmarks cannot be submitted as Chromaprint.
AcoustID is therefore not an additional resolver here; a future integration
would require an explicitly supported input and review of its API-key,
rate-limit and usage conditions.

### Pending metadata housekeeping

On both backends, enabling verification withholds existing unverified references from recognition and statistics, and requeues eligible previously processed submissions once. Existing pending submissions keep their housekeeping deadlines. References and landmarks are retained until the normal housekeeping policy permits deletion. Disabling verification restores the offline completeness gate; re-enabling retains successful verification rather than reprocessing the entire collection. Verification is disabled by default. SQLite schema upgrades create the documented pre-upgrade backup; PostgreSQL backups must be arranged separately. In a split deployment, only worker startup applies verification transitions; use matching settings on both processes.

For a one-time non-destructive operator pass, run `docker compose exec fingerprints python -c 'from fingerprint_server.settings import Settings; from fingerprint_server.database import create_store; create_store(Settings()).request_pending_recheck()'` (use `-f compose.gateway.yaml` for the gateway deployment). This selects the configured backend without initializing it or recovering jobs. The running worker picks up the request within its idle polling interval, snapshots existing pending submissions and checks them regardless of their deadline. MusicBrainz verification must be enabled. This pass never deletes submissions or consumes completed rechecks; successful results publish normally and unresolved results remain pending. Service cooldowns still apply, and start/completion are logged. The request is consumed once, not repeated at every restart; an interrupted pass leaves unprocessed rows pending.

Housekeeping runs in the existing worker, only when both MusicBrainz verification and housekeeping are enabled. Configure these environment settings before starting the server; both Compose deployments pass them through. Recreate the backend after changing them.

| Setting | Default | Meaning |
| --- | --- | --- |
| `FP_HOUSEKEEPING_ENABLED` | `true` | Retry eligible pending submissions and delete exhausted or definitively unresolvable submissions. When false, neither automatic pending rechecks nor automatic deletion runs; ordinary processing continues. |
| `FP_PENDING_MAX_RECHECKS` | `3` | Maximum additional completed unresolved checks after initial processing, from 1 to 1000. Delete the submission after the last unresolved check. |
| `FP_PENDING_RECHECK_DAYS` | `7` | Days between completed checks, at least the seven-day MusicBrainz cache lifetime. Changes apply when scheduling the next deadline; existing persisted deadlines are retained. |
| `FP_BACKGROUND_RECHECK_QUEUE_WINDOW` | `2` | Maximum combined background jobs admitted to the shared queue, including the job being processed (1–1000). Applies to scheduled pending checks and mode/version reverification. |

Automatic background work is admitted gradually at the tail of the same FIFO queue as new uploads. FIFO order uses a separate admission timestamp, not the original upload date. With the default window, at most two background jobs are queued or processing at once; a free slot is replenished when the worker next claims work. New uploads retain their queue position ahead of subsequently admitted background jobs. Reverification of previously processed submissions waits as pending work until admitted, without spending a pending-recheck attempt merely for verification. The setting is an admission window, not parallelism: there is still only one ingestion worker and one shared MusicBrainz cooldown. Lowering the window preserves already admitted jobs and their positions, and pauses further admission until capacity is available. If housekeeping is disabled, admitted scheduled pending checks return to pending without consuming a completed check; required mode/version verification can still run when MusicBrainz is enabled.

For example, set `FP_BACKGROUND_RECHECK_QUEUE_WINDOW=2` in the Compose environment or `.env` file and recreate the backend. Both Compose variants forward this setting. Manual operator passes intentionally keep their existing exclusive, non-destructive behavior and bypass the automatic window; this change does not interrupt or reprioritize a currently running manual pass.

Not-found identifiers, ambiguous ISRC results and incomplete metadata get bounded rechecks. Network failures, service cooldowns and malformed responses do not consume completed rechecks or prove that a submission is unresolvable. Successful verification publishes normally. New submissions with no usable identifier or definitive identifier/coverage conflicts are deleted after processing when housekeeping is enabled; with housekeeping disabled they remain pending. Invalid submission documents retain the explicit failed state.

Schema 5 adds a durable recheck counter, deadline and interrupted-check marker. Existing pending submissions start with zero completed rechecks and a first deadline seven days after migration, regardless of their age or reason; the migration deletes nothing. Restarts and verification-mode activation do not bypass that grace period. Interrupted checks recover without consuming a completed check. An existing no-identifier submission can be deleted when its first scheduled check is due.

When the current verification confirms that neither identifier is available, housekeeping also removes its linked reference and landmarks if the reference is unpublished, has no usable identifier, and no other linked or unlinked submission depends on it. Checking unlinked documents may take time for a large queue. If a dependency cannot be checked, the reference is retained. SQLite defers cleanup if the database changes during the check; PostgreSQL holds the shared mutation lock throughout the check and deletion, which can temporarily delay admission. Conflicts and exhausted rechecks still delete only the submission; published and shared references are retained. The deletion is atomic, and a later complete submission can recreate a deleted reference. Existing pending submissions are checked only when their existing deadlines are due; migration itself deletes nothing.

After deletion, the submission-status endpoint returns its normal not-found response; uploading the identical document again starts a new submission lifecycle. Scheduled checks are logged as `Housekeeping checking submission`. Every automatic deletion is logged after the database transaction commits with `housekeeping=true`, the submission ID, reason and completed recheck count where applicable. Missing-identifier deletions also report whether the reference was deleted and the number of landmarks removed (`landmarks_deleted`); there is no separate batch total because housekeeping runs continuously in the worker. These are logical deletions: SQLite reuses freed pages without automatically shrinking the file; PostgreSQL relies on its normal autovacuum maintenance to reclaim reusable space. Housekeeping itself does not run `VACUUM`.
