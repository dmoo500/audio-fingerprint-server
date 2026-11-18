# Administrator website

[Documentation](../README.md)

The optional Starlette-Admin service is separate from the public fingerprint API.
It adds searchable, server-paginated metadata tables without introducing ORM
models or changing the database schema. It currently requires PostgreSQL and
APISIX; SQLite installations can continue using the existing API and CLI.

## What it provides

- Database size (including indexes/TOAST, excluding WAL), layout version,
  worker availability, reference counts and submission lifecycle counts.
- Searchable submissions and references, detail views, sortable columns and
  advanced text filters. Tables show at most 100 records per page and scroll
  within the page; raw fingerprint documents are never returned.
- An **Unresolved ISRC submissions** table: all retained submissions with an
  ISRC whose state is not `processed`, including waiting and failed entries.
  This is a processing-state view, not a certificate of ISRC validity.
  Historical processed records do not store whether resolution was based on
  an ISRC association or a strict metadata match, so the UI cannot reconstruct
  that distinction.
- A non-destructive pending review using the existing worker controls.
  This preserves unresolved entries and does not increase their completed
  pending-check count. Normal housekeeping remains a separate operation.
- Confirmed individual/bulk retries and submission deletions. Queued and
  processing jobs cannot be changed. Retry is limited to failed/unresolved
  records, respects shared admission budgets, resets attempt counts and
  preserves completed pending-check counts. Deleting a submission retains its
  acoustic reference and landmarks; it is **not** a reference purge.
- Personal APISIX key creation/revocation. Stored secrets are never displayed;
  a newly generated secret is shown only in its creation response. Names have
  a random suffix to avoid replacing an existing named consumer through
  APISIX's upsert endpoint.
- Separate **Application keys** page for creating and revoking named application
  credentials. Secrets appear once on creation; PostgreSQL stores only SHA-256
  hashes of the randomly generated keys. Revoked identities remain listed for
  audit purposes. Every application still needs a personal key for permissions.
  Internal scope and gateway/admin credentials are not managed here.

MusicBrainz cooldown is worker-local and is not reported by this service.
No data is submitted back to MusicBrainz.

### Application-key registry upgrade

Deploy the new API code to **all replicas before issuing registry keys**.
Gateway-enabled PostgreSQL service startup creates the independent version-1
`application_key_schema` and `application_keys` tables transactionally under
the existing migration/write lock. Repeated startup preserves credentials.
This does not change the fingerprint format version or alter existing rows.
The admin only validates this registry; it never creates or migrates it.

The existing `FP_APPLICATION_KEY` stays valid and is shown as a legacy
environment credential, without revealing its value. It cannot be revoked
through this page. After moving clients to registry keys, rotate that variable
consistently on backend services to retire its old value; removing it is not
currently supported. Restart the admin with the same configuration too.
SQLite gateway deployments retain their existing single-token behavior.

Active registry keys are checked against PostgreSQL on each request without
a positive-authentication cache, so revocation affects the next check on every
updated replica. A database error fails registry authentication with HTTP 503,
not legacy access. Already authenticated requests may finish. Authentication
logs use the opaque `app-<id>` identity, never the secret or its hash.

Back up the registry with the PostgreSQL database. The experimental SQLite
export only exports fingerprint tables and **does not carry these credentials**;
after such a backend change only the configured legacy token is supported.
Rolling back to pre-registry API code similarly disables registry keys; it
does not delete them.

## Live updates

Table search filters the entire dataset on the server, not just the visible
page. Typing, pasting and clearing the search field apply after a 350 ms pause;
Enter applies immediately. Search is case-insensitive and also matches with
spaces and punctuation removed, so `ACDC` finds `AC/DC`. Punctuation-only
queries still use literal matching rather than matching everything. Advanced
field filters retain their literal matching rules.
Click column headings to toggle ascending/descending order, or Shift-click
for multiple sort columns. Numeric counts and verification versions sort
numerically. Selection/action and hidden metadata columns are not sortable.
The query status explicitly shows when a search or table load is running,
then shows the matching-record count. Previous rows stay visible until the
response arrives; a failed request is marked as an error, not an empty result.
Metadata searches still read the stored submission documents and can take
several seconds on larger collections.

The overview receives counts, database size and worker availability through an
authenticated Server-Sent Events (SSE) connection. One shared sampler checks
the database every five seconds while browsers are connected, regardless of
how many tabs are open. Slow clients receive the latest snapshot rather than
an ever-growing event queue. Connection and database failures are shown
explicitly; displayed numbers must not be treated as fresh during an outage.
On the overview and database table pages, a small **?** indicator at the top
right shows green for fresh data, yellow while connecting/reconnecting, and
red for failed checks, stale data or an ended session. Hover, keyboard focus
or click reveals the explanation and last successful check time. Click pins
the explanation open; click again to unpin it. Key-management pages do not
open a live database stream and therefore have no connection indicator.

Submission, unresolved-ISRC and reference tables keep their rows, filters,
sorting, selection and scroll position when changes arrive. A **Newer data
available - refresh** button lets the operator reload rows explicitly, keeping
the current search, sort, page and scroll position. That explicit refresh
clears selected rows to avoid applying actions to an outdated selection.
API-key pages are not part of this database stream.

Table notices are advisory: they use PostgreSQL's asynchronous per-table write
counters plus lifecycle/reference counts, not expensive metadata or landmark
scans. Metadata-only updates are detected even when row counts stay the same,
but PostgreSQL statistics can lag and unrelated writes can trigger a notice.
`track_counts=on` is required. This adds no schema changes or database triggers.

The SSE stream rechecks session validity on each update and at least every ten
seconds; logout or expiry ends updates. The browser reconnects automatically
after a connection failure. A service restart invalidates sessions and requires
signing in again. Reverse proxies must disable buffering for `/admin/events`
and allow long-lived responses.

## Local activation

Install `.[admin]` for native use, or add `compose.admin.yaml` **after** the
existing gateway/PostgreSQL Compose files. The optional image build installs
the admin extra; ordinary API/worker images do not need it.

Set these additional values in a private environment file outside version
control, with owner-only permissions:

- `FP_ADMIN_KEY`: an independently generated URL-safe secret of at least
  32 characters. This is the password for username **admin**.
- `FP_ADMIN_POSTGRES_DSN`: a PostgreSQL connection string reachable from the
  admin container, using the existing service database role, not the database
  superuser. Native runs use `FP_POSTGRES_DSN` instead.
- `FP_GATEWAY_ADMIN_KEY`: the existing private APISIX administration key.

Reuse the existing internal read/upload/application keys and schema setting;
set `FP_GATEWAY_ENABLED=true` when providing the application key (already set
by the Compose overlay).
Set `FP_QUEUE_LIMIT` and `FP_QUEUE_BYTES_LIMIT` to the **same values as the
ingestion service**, so manual retries enforce its queue budgets too.

For example, append the overlay to your normal Compose command:

```sh
docker compose --env-file /private/path/gateway.env \
  --env-file /private/path/admin.env \
  -f compose.gateway.yaml -f /private/path/postgres.compose.yaml \
  -f compose.admin.yaml up -d --build --no-deps admin
```

Keep any existing project name and local overlays in that command. The admin
service joins the backend, private administration and outbound networks and publishes
only `127.0.0.1:8099`. It does not start a worker, acquire API ownership, create
tables or apply migrations. The database and gateway must already be running.
Open <http://127.0.0.1:8099/admin/> and sign in as **admin**.

Use **one admin process/replica**: sessions and login admission are held in that
process. Login is capped at ten attempts per minute across the service.
Sessions expire after one hour, are revoked on logout, and are invalidated by
an admin restart. Cookies are HttpOnly/SameSite=Strict; the login secret is not
stored in them. State-changing requests require POST and a same-origin Origin
header. Responses are marked `no-store`.

## Future remote access

Do not expose APISIX's administration port or etcd. Prefer access through a VPN;
otherwise use a dedicated HTTPS reverse proxy/hostname with additional access
control, and set `FP_ADMIN_COOKIE_SECURE=true`. Preserve the original Host and
configure Uvicorn's trusted proxy handling explicitly so origin validation sees
the correct HTTPS origin. Do not trust forwarded headers from arbitrary peers.

The administration service holds privileged database and APISIX credentials.
It is not a public multi-tenant portal. Public operation needs deployment-specific
access control and edge request limits; adding OIDC later belongs in the auth
provider/proxy, without replacing the tables or data adapters.

Before upgrading Starlette-Admin, rerun admin tests and browser checks,
particularly POST-only row actions, authentication, filtering and key creation.
