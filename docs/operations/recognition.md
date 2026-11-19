# What's That Song? — public microphone recognition app

[Home](../../README.md) · [Documentation](../README.md)

What's That Song? is an independent song recognition app for Music Assistant. Its
optional `fingerprint_server.recognition` service is a standalone public website
in this repository. Visitors need no account or API keys. It shows a microphone
button, a song result and one collection counter, not the upload queue,
reference variants or administrator tools. It replaces the former browser
microphone test page on the API port.

## What stays on the server

The browser records ten seconds of mono PCM, stops the microphone and sends the
recording to this website's own backend. The backend attaches a dedicated read-only
personal key and application token and calls the existing fingerprint API.
Landmark extraction, resampling and matching still run on the fingerprint API
server. Neither credential is returned to the browser or accepted from browser
headers. The public service has no database credentials and no upload/admin routes.
The page displays the active audio track's device name after capture starts.
The name stays in the browser and is not sent to the server.

Microphone audio is transient: it is not saved, indexed, or used to grow the
collection. Recognition results expose only title, artists and album. Internal
reference IDs, match scores, retained metadata and upstream error details stay
private. The authenticated recognition API remains available for clients.

API diagnostics log capture duration, sample rate, RMS/peak levels, landmark and
candidate counts, aggregate match evidence and the acceptance decision. They do
not log audio, fingerprint hashes, reference IDs or song metadata. These figures
help distinguish capture problems from an insufficient acoustic match without
keeping the recording.

The counter uses `recordings.total` from the authenticated statistics endpoint:
logical **searchable recordings**, not acoustic variants or queued submissions.
Verified recording IDs are grouped; unverified references may count separately.
It is not a promise that every passage or every song will be recognized.
The backend caches the count for up to 60 seconds. The browser loads it on page
opening and refreshes every 60 seconds while visible, including after a failed
request. Refreshes pause in hidden tabs and resume immediately when the page
becomes visible again. Failed refreshes are explicit and are not displayed as zero.

## Album artwork

Successful matches offer a separate, short-lived artwork URL on this service.
The song card appears immediately, with a decorative record placeholder in the
reserved cover area while artwork is loading
or when artwork is absent or unavailable. Loading the image does not move the title.
Artwork is polled
for up to about a minute. A later recognition cancels the previous cover display.
The first three pending responses are retried after one second rather than three,
so fast worker lookups do not incur a full three-second polling delay. Further
pending responses use three-second intervals to limit backend traffic.
The public token expires after ten minutes and does not reveal the reference ID.

The existing ingestion worker resolves the recording's releases through its
shared, throttled MusicBrainz client. It selects a release group only when releases
matching the supplied album name identify one unique group. This is representative
album art; known parenthesized edition labels such as "Deluxe Edition" and
"Deluxe Version" are ignored
when comparing album names. Live/version titles are not stripped. This is
not a guarantee of the exact edition. Ambiguous albums or absent
covers leave the text result intact. Enable MusicBrainz and configure its contact
address on the worker as for metadata verification; no additional key is needed.

Cover Art Archive is queried at most once per 1.1 seconds with a HEAD request.
The worker reads only the redirect, never the image bytes. Group-level cover
results, including confirmed absence, use the existing seven-day enrichment cache.
Per-reference results expire after 30 days. Temporary failures are reported
separately, expire after a minute, and are not cached as missing covers.

The browser loads a cover directly from the archive over HTTPS without a referrer;
this makes its IP address visible to the image host. Only Cover Art Archive and
archive.org image hosts are permitted by validation and the page's image policy.
Recognition credentials are never sent to them.

### Additive database migration

Startup installs the independently versioned artwork extension (version 1), like
the application credential registry: `artwork_requests` plus the `artwork_schema`
entry in existing service state. Installation is transactional and idempotent on
SQLite and PostgreSQL. Existing reference rows, fingerprint formats and core
database versions are unchanged; there is no rescan or bulk MusicBrainz lookup.
The queue/cache is bounded to 4,096 entries and populated only on demand.
The existing core offline database import/export does not transfer this optional
cache; covers are resolved again on demand after conversion.

Deploy the updated worker, API and recognition service and update the gateway's
read-authenticated `/api/v1/dejavu/artwork/*` route. Multiple API replicas share the
queue; only the owned ingestion worker calls MusicBrainz or Cover Art Archive.
If rolling back, retain the additive table and state entry. A server older than
this extension may reject the additional SQLite table during schema validation.

## Configuration and local development

Store configuration in a private environment file outside the repository. Required:

| Variable | Purpose |
| --- | --- |
| `FP_WEB_READ_KEY` | Dedicated personal credential with the `read` role, not an upload/both/admin key |
| `FP_WEB_APPLICATION_KEY` | Dedicated application token when using APISIX; omit only for the legacy non-gateway API |
| `FP_WEB_API_URL` | Fingerprint API or APISIX origin; default `http://127.0.0.1:8097` |
| `FP_WEB_PUBLIC_URL` | Exact browser-visible origin, including scheme and port; default `http://127.0.0.1:8100` |
| `FP_WEB_REQUESTS_PER_MINUTE` | Recognition attempts per client address per minute; default 4, maximum 30 |

Use the admin pages to issue the dedicated credentials. Do not copy the admin
login key, an MA login token, or an upload credential. This adds no schema change
or stored-data migration.

After loading the private environment:

```bash
uv run uvicorn fingerprint_server.recognition:app \
  --host 127.0.0.1 --port 8100 --workers 1 --no-proxy-headers \
  --log-config fingerprint_server/logging.json
```

Open `http://127.0.0.1:8100`. Remote microphone access requires HTTPS.
Opening the root page through another hostname redirects to `FP_WEB_PUBLIC_URL`
before microphone capture, so aliases such as `localhost` cannot cause a later
Origin-check failure. Behind a TLS-terminating proxy, preserve the public Host
and configure trusted forwarded scheme handling as described below.
No new frontend dependencies or build step are needed. Assets ship in the Python package.

## Gateway deployment

Add [compose.recognition.yaml](../../compose.recognition.yaml) to the existing
gateway/PostgreSQL Compose invocation, and include an owner-only environment file
containing the `FP_WEB_*` values. In that overlay, the API origin is
`http://gateway:9080`, and both dedicated credentials are required.

```bash
docker compose --env-file /private/path/gateway.env \
  --env-file /private/path/recognition.env \
  -f compose.gateway.yaml -f compose.recognition.yaml \
  up -d --build --no-deps recognition
```

Keep all of your existing deployment-specific Compose overlays and project name
in the command; the example is not a replacement for them. The service binds to
loopback port 8100. Publish **only this service** behind an HTTPS reverse proxy,
not the fingerprint API's test page or admin service.
`GET /health` checks that the web process is alive; it does not certify upstream
availability. Recognition and counter outages are reported explicitly in the UI.

## Admission and deployment limits

- One Uvicorn worker and **one recognition replica**. Limits and the counter cache
  are in memory. Do not scale this service out without shared admission accounting.
  The fingerprint API's existing replica coordination is unaffected.
- At most two active recognition requests, including body reads; each upload is
  bounded to 3–15 seconds of mono float32 audio at 8–192 kHz, with a 30-second
  body-read timeout. Browser capture remains ten seconds.
- Per-address recognition limits, 30 counter requests per minute, and bounded
  short-lived accounting for at most 4,096 address/scope pairs. Excess work is
  rejected with explicit 429/503 responses, not queued in an unbounded buffer.
  The upstream API/APISIX admission limits still apply to the dedicated identity.
- The Compose command ignores forwarding headers by default. Behind a reverse
  proxy, visitors therefore share its address and its recognition budget.
  To distinguish visitors, configure Uvicorn to trust **only the actual proxy's
  address**, have that proxy replace incoming forwarding headers, and prevent
  clients from reaching the web service directly. Never trust `*` on a
  publicly reachable listener.
- Apply connection, upload-size, body-timeout and request-rate limits at the
  public reverse proxy as well. Origin checks prevent cross-site browser use;
  they do not stop non-browser clients from spoofing an Origin header.
  This is bounded anonymous access, not bot-proof or distributed-DDoS protection.
- No CORS, cookies, external scripts/fonts or analytics. Recognition POSTs require
  the configured origin and an explicit same-origin request header. A strict CSP
  and microphone permissions policy apply to every response.

This service is implemented but is **not enabled automatically** in an existing
deployment. Choose the public origin and configure credentials and proxy limits
before exposing it.
