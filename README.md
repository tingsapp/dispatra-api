# Dispatra API

FastAPI/PostgreSQL backend for company accounts and the manual delivery workflow. Dispatchers and Shippers create Orders through shared validation and pricing services. Dispatchers assign drivers and vehicles; drivers execute ordered visits and submit evidence. Verified completion issues one Invoice and queues its email in the same transaction. A separate worker delivers queued email. The Order agent worker reads company order mailboxes and books complete emails. The Dispatch agent recommends drivers for unassigned Orders and, in AUTO mode, assigns them.

## Local setup

Use Python 3.12+ and PostgreSQL 16+. Create a virtual environment and install `requirements.lock`. Copy `.env.example` to `.env`, set your own database credentials, and load it before running commands. The migration connection must be separate from the restricted runtime login. Never run the API with owner or superuser credentials.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
set -a
. ./.env
set +a
.venv/bin/alembic upgrade head
```

The migration administrator creates the `dispatra_app` NOLOGIN group. Create your own restricted login and set its password with PostgreSQL's `\password` prompt:

```sql
CREATE ROLE dispatra_runtime LOGIN NOSUPERUSER NOBYPASSRLS;
GRANT dispatra_app TO dispatra_runtime;
```

```sh
.venv/bin/python -m app.bootstrap
.venv/bin/uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

## Platform owner

There is no public signup and no default owner password. `app.bootstrap` creates the single `ADMIN` in a migrated database, using migration credentials from the loaded private `.env`. It only reads credentials from an interactive terminal (never pipes, files or arguments), asks for the password twice, requires 12+ characters with three character classes (or a 20+ character passphrase) and rejects a password containing the login ID. Only an Argon2 hash is stored; nothing is printed or logged. A database index permits exactly one owner.

```sh
cd api
set -a; . ./.env; set +a
.venv/bin/alembic upgrade head                   # the command refuses an unmigrated database
.venv/bin/python -m app.bootstrap --status       # "No platform owner exists." / "Platform owner exists: <login>"
.venv/bin/python -m app.bootstrap                # prompts: login ID, password, confirmation
```

Then sign in at `/admin` (for example `http://localhost:3000/admin`). Rerunning the create command when an owner exists changes nothing and exits with an error that names the existing login ID.

Recovery (forgotten password or lockout): run `.venv/bin/python -m app.bootstrap --reset-password` in a terminal on the server. It prompts for a new password, reactivates the owner, revokes every owner session, clears that login's rate-limit bucket and records `platform_owner.password_reset` in the audit log. Use `--status` to recall the login ID. Owners change their own password at `/admin/profile`, which also signs out their other sessions.

The owner cannot sign in to company workspaces and company endpoints return 404 for owner sessions; no impersonation exists. All platform endpoints are beneath `/api/v1/platform/organizations`, require an owner session (anonymous 401, company roles 403), and mutations require the usual `Origin`, `X-Requested-With: Dispatra` and `Idempotency-Key` headers:

| Action | Endpoint |
| --- | --- |
| List/search companies (`search`, `status=ACTIVE|SUSPENDED`, `after`, `limit`) | `GET /platform/organizations` |
| Create company + first dispatcher (one transaction) | `POST /platform/organizations` |
| Company detail and access counts | `GET /platform/organizations/{id}` |
| Edit company name (`version`); the slug is immutable | `PATCH /platform/organizations/{id}` |
| Suspend / activate (`version`) | `POST /platform/organizations/{id}/suspend`, `/activate` |
| Administration audit history | `GET /platform/organizations/{id}/audit` |
| Dispatcher accounts (login ID, status, created, last sign-in, active sessions) | `GET/POST /platform/organizations/{id}/dispatchers` |
| Edit login ID/name (`version`) | `PATCH /platform/organizations/{id}/dispatchers/{user}` |
| Activate / deactivate / reset password (`version`) | `POST .../dispatchers/{user}/activate`, `/deactivate`, `/reset-password` |
| Sign out every session | `POST .../dispatchers/{user}/revoke-sessions` |

Dispatcher creation and password reset return a server-generated `initial_password` once; idempotent replays, later reads, audit rows and stored operation results never contain it (replays return `null`). Login IDs are unique within a company across all its roles. Deactivating, resetting a password or revoking sessions deletes that account's sessions. The last active dispatcher of a company cannot be deactivated. Suspension deletes every session in the company and closes open driver duty; afterwards a correct password receives `403` "workspace is suspended", an incorrect one the normal `401`. Activation does not restore ended sessions. Orders, invoices, directory records and audit history are unchanged by any of these commands. Queued quote/invoice email for a suspended company is not cancelled.

Swagger is at `/docs`; the versioned API is `/api/v1`. Use the web public entry at `/`, platform administration at `/admin` (companies at `/admin/companies`, a company at `/admin/companies/{id}`, owner profile at `/admin/profile`), and company workspaces at `/{company-slug}/`. Authenticated Profile uses database company settings, session email/role, and real password changes. Other operational frontend screens use browser demo records; connecting them while preserving their original UI is a separate integration step.

## Demo pricing in PostgreSQL

New companies automatically receive the versioned [demo pricing seed](app/operations/seeds/demo_pricing_v1.json). It preserves the current client presets: six Rate Cards including archived history, four Service Levels, ten Vehicle Types (nine active with dimensions and equipment, plus the retired `veh_5_ton`), twelve Accessorials, fuel surcharge, GST/HST settings, and the 0–99 lb / Zone 1 / $20 starter matrix. No real driver, Shipper, Order, or payment account is created by this pricing seed.

For an existing company:

```sh
.venv/bin/python -m app.operations.seeding --company acme
```

The dispatcher can also call `POST /api/v1/companies/{slug}/pricing/seed` with an `Idempotency-Key`. Reruns add missing preset codes and leave existing values, archived/deleted catalogue entries, and default-card selection unchanged. Browser localStorage is never imported. The seed is demonstration configuration, not a determination of a company's applicable tax rates.

## Persistent demo accounts

Run the explicit administrator command after loading `.env` and applying migrations:

```sh
.venv/bin/python -m app.demo
```

This ensures the installation's admin and creates the dedicated `demo` company (Dispatra Demo), these accounts, one linked demonstration van, and the pricing presets above:

| Account | Email login | Portal path |
| --- | --- | --- |
| Admin (platform) | `admin@example.com` | `/admin` |
| Dispatcher | `dispatcher@example.com` | `/demo/` |
| Shipper | `shipper@example.com` | `/demo/shipper` |
| Driver | `driver@example.com` | `/demo/driver` |

All four seeded demo accounts start with password `123456`, stored as separate Argon2 hashes in PostgreSQL. The first run writes a private, Git-ignored `.local/demo-accounts.json` file (mode 0600) containing initial login details. Reruns preserve passwords, profile edits, disabled accounts, and pricing edits. The admin is created only when the installation has none; an admin created earlier with `app.bootstrap` is kept unchanged and reported with no password. Change the demo admin password at `/admin/profile`, and never run `app.demo` on a production database. The command refuses an unrelated company already using `demo`; it is never run automatically on application startup. For a new database, use `--credentials-file` with a new path if an older credentials file already exists. The file contains initial credentials only and is not updated by later password changes.

The demo Shipper uses the Fixed card so pricing works without an external distance request. Driver and Warehouse addresses are fictional Vancouver examples. The driver starts off duty, with no fabricated GPS or completed work. Driver login is implemented at the API; the web Driver route still needs its operational screen. Operational frontend screens continue using their existing browser stores until connected.

### This workspace's local database

The local development database is `dispatra_demo` on `127.0.0.1:55432`. Its data and PostgreSQL runtime are in the ignored `api/.local/` directory, outside disposable test storage. Owner and restricted runtime credentials are in the private `api/.env`. Normal server restarts preserve the records. After reboot, start it explicitly:

```sh
cd api
scripts/local-db start
.venv/bin/uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

Use `scripts/local-db status` or `scripts/local-db stop` to manage it. This script controls the local installation prepared in this workspace; a fresh clone can use the PostgreSQL/Compose setup above, then run `app.demo`. Keep `.local/` and `.env` out of Git and retain the database directory for persistent data. Never point test commands at `dispatra_demo`.

## Manual workflow

All paths below are beneath `/api/v1/companies/{slug}`. Mutations require an allowed `Origin`, `X-Requested-With: Dispatra`, and an `Idempotency-Key` of 16–80 alphanumeric/hyphen characters. Edits/transitions require the returned `version`; driver Route commands also require `generation`. Keep the same key and identical payload when retrying a command. Stop and item IDs are client-generated UUIDs that must remain stable across retries.

| Workflow | Endpoints |
| --- | --- |
| Company identity, taxes, units, fuel, dispatch limit | `GET /settings`, `PUT /settings` |
| Service Levels, Accessorials, Vehicle Types | `GET/POST /catalog`, `PUT/DELETE /catalog/{id}` |
| Rate Cards | `GET/POST /rate-cards`, `PUT /rate-cards/{id}`, `POST /rate-cards/{id}/archive` |
| Shipper profile and generated login | `GET/POST /shippers`, `GET/PUT /shippers/{id}`, `POST /shippers/{id}/archive` |
| Driver profile, duty edit and generated login | `GET/POST /drivers`, `GET/PUT /drivers/{id}`, `POST /drivers/{id}/archive`, `POST /drivers/{id}/reset-password` |
| Fleet vehicles | `GET/POST /vehicles`, `GET/PUT /vehicles/{id}`, `POST /vehicles/{id}/archive` |
| Shipper self-service | `GET/PATCH /shipper/profile`, `GET /booking-options` |
| Preview / prospect quote | `POST /pricing/preview`, `POST /quotes`, `GET /quotes/{id}` |
| Booking / revision / cancellation | `GET/POST /orders`, `GET/PUT /orders/{id}`, `POST /orders/{id}/cancel` |
| Manual assignment / planned Route release | `POST /orders/{id}/assign`, `GET /routes`, `POST /routes/{id}/release` |
| Driver profile and duty | `GET /driver/profile`, `POST /driver/duty`, `POST /driver/duty/{id}/end` |
| Driver telemetry and manifests | `POST /driver/location`, `GET /driver/routes`, `GET /driver/routes/{id}` |
| Route start / execution / finish | `POST /driver/routes/{id}/start`, `POST /driver/routes/{id}/stops/{visit}/arrive`, `POST /driver/routes/{id}/stops/{visit}/complete`, `POST /driver/routes/{id}/finish` |
| Evidence | `POST/GET /driver/stops/{stop}/evidence`, `GET /evidence/{id}`, `GET /orders/{id}/delivery-proof` |
| Issues and resolution | `POST /orders/{id}/issues`, `GET /issues`, `POST /issues/{id}/resolve` |
| Invoice review and recovery | `POST /orders/{id}/invoice`, `GET /invoices`, `GET /invoices/{id}`, `GET /invoices/{id}/document` |
| Operational reporting | `GET /monitor`, `GET /analytics`, `GET /analytics/export.csv`, `GET /drivers/{id}/activity` |

Logins use `/api/v1/auth/login` with `portal: dispatch`, `customer`, or `driver`, the company slug, email/login ID, and password. A new operational Shipper or Driver and its user account are created in one transaction. The generated password is returned once, is never persisted in idempotency results, and is absent on replay. If the first response is lost, use the reset workflow. Existing customer-account endpoints remain compatible; operational Shipper email changes are managed by the dispatcher, and Warehouse Address edits use the structured Shipper endpoint.

Shipper identities come from the authenticated session. Shippers cannot select another account/payer, override Rate Cards, submit arbitrary pricing distance, adjust prices, assign drivers, or issue invoices. Driver manifests exclude pricing, payer records, internal notes, and other drivers' work.

## Pricing and routing

Money uses Decimal and cent rounding. Pricing supports Base + Distance, Fixed, Zone, Hourly, and explicit legacy imported totals; fixed service/vehicle charges; flat Accessorials; fuel; Shipper discounts; card minimums; and separate enabled GST/HST and provincial tax lines. Canonical units are km/kg/cm. Saved Orders retain the pricing context; explicit revisions retain previous snapshots. Invoice issuance settles under the quoted rules and consumes that result. Hourly settlement uses recorded first-pickup arrival through final delivery; the dispatcher may supply confirmed actual minutes. A Quote never creates an Order.

A booking with missing rates/distance or a failed routing provider is retained as `NEW` with `pricing.status: NEEDS_ATTENTION`; it cannot be assigned until reviewed. No fabricated zero price is used. Dispatchers can provide verified standalone Order distance. Shipper distance-based bookings use server routing when configured or wait for dispatcher pricing review.

- `ROUTING_PROVIDER=demo`: local geodesic planning at 35 km/h, clearly labeled `DEMO_GEODESIC_35_KPH`. This makes no external requests and is not a road ETA. It never supplies authoritative distance-based prices.
- `ROUTING_PROVIDER=google`: set a server-restricted `GOOGLE_ROUTES_API_KEY` with Routes API enabled. Assignment obtains a road-time matrix; standalone Order pricing obtains road distance. Only required fields are requested. Routing calls occur on explicit pricing/planning commands, not Monitor reads or map animation. Replayed successful mutation keys reuse committed results. Raw provider responses are not cached in PostgreSQL.
- Tracking map: `GET /orders/{id}/tracking/map` returns a Google Static Maps PNG of the tracking view. Enable the Maps Static API on the Google Cloud project; the key is `GOOGLE_MAPS_STATIC_API_KEY` (falls back to `GOOGLE_ROUTES_API_KEY`). Set `GOOGLE_MAPS_URL_SIGNING_SECRET` (the project's URL signing secret) so every request is signed. Requires `ROUTING_PROVIDER=google`; otherwise the endpoint answers 409 and clients show no map. Road geometry is cached in memory per Order version and images for 60 seconds per exact map.
- Unconfigured routing fails for assignment instead of silently inventing travel times. Google-backed planning is capped at 25 stops per Route; standalone pricing supports 27 stops. Road times exclude live traffic and do not certify commercial vehicle road restrictions.

Reference: [Google route matrix](https://developers.google.com/maps/documentation/routes/compute_route_matrix), [route directions](https://developers.google.com/maps/documentation/routes/compute_route_directions), and [provider storage/display policies](https://developers.google.com/maps/documentation/routes/policies). Production UI must retain required attribution.

Both travel modes validate explicit item links, pickup precedence, per-stop physical weight/volume/pallet capacity, package fit, driver duty, pickup service city, DG qualifications, crew/equipment, exclusive service, active Order limits, vehicle custody, shift/windows, and stop limits. Search is bounded and reports whether it exhausted its search budget; no feasible result means dispatcher review. Executing Routes cannot be silently rearranged. Release a planned Route before changing its assignments.

## Completion, invoices, and evidence

Drivers record arrival and confirm actual quantities at every ordered visit. A short load or failed delivery is reported as an issue and cannot be treated as completed cargo. Receiver-present delivery requires a recipient name and signature; unattended delivery requires permission, safe placement and a photo. Required photos cannot be bypassed. Bounded PNG/JPEG evidence is stored in tenant-scoped PostgreSQL rows, served only through authorized downloads, and retained until server confirmation.

Offline clients retain action UUIDs, expected versions, generation, capture times, dependencies, and evidence until the server acknowledges them. Start Route requires an online request; saved execution actions can be replayed in order. End Duty defines the final allowed capture time; later uploads can contain only earlier samples. Logout and driver password reset close open duty sessions.

Valid completion issues one frozen Invoice and queues email to the saved billing address in the same transaction. An hourly Order completed by a dispatcher without pickup arrival evidence stays `COMPLETED` until a dispatcher supplies actual billable minutes through `POST /orders/{id}/invoice`. One database-unique invoice per Order prevents duplicate issuance even with different command keys. The Invoice document is an API-served PDF. The separate SMTP worker must be running to deliver queued email; payment processing is not performed.

## Quote and invoice email

A dispatcher queues a priced, unexpired prospect quote with `POST /quotes/{id}/send` and `{ "version": 1, "recipient": "buyer@example.com" }`. Completion queues invoice email automatically; `POST /invoices/{id}/send` and `{ "version": 1 }` is available for dispatcher recovery or an explicit resend. Explicit commands require an `Idempotency-Key` and return a delivery record with `PENDING` status. `GET /email-deliveries/{id}` reports `PENDING`, `SENDING`, `SENT`, `FAILED`, or `UNKNOWN`. A resend is a new explicit command after the preceding attempt reaches a terminal state. Queueing is not proof of SMTP acceptance or inbox delivery.

Configure the server-only `SMTP_*` values from `.env.example` in private `.env`. For Gmail app-password submission use `smtp.gmail.com`, port 465 with TLS, and the complete Gmail address as both username and sender. Run a separate worker alongside the API:

```sh
.venv/bin/python -m app.operations.email_worker
```

`--once` processes currently eligible requests and exits. Invoice email includes the frozen PDF as an attachment. The worker claims only email events and commits before SMTP I/O. A temporary failure retries up to five times. A rejected message becomes `FAILED`; an uncertain acceptance or interrupted send becomes `UNKNOWN` and is never auto-resubmitted. Inspect that status before explicitly sending again. SMTP does not provide provider-side idempotency, so this conservative boundary avoids automatic duplicate invoices but cannot prove delivery to a recipient's inbox. The API never returns the app password or email body in the delivery-status projection.

## Order agent (email intake)

A dispatcher connects one company IMAP mailbox under Settings → Mailbox (host, port 993, username, app password, folder, optional default service). The password is checked by signing in, then stored encrypted with the server-only `MAILBOX_ENCRYPTION_KEY` (a Fernet key; see `.env.example`) and never returned. Mailbox hosts that resolve to private, loopback or other non-public addresses are refused. Run the agent worker alongside the API:

```sh
.venv/bin/python -m app.intake.worker
```

Every minute (`--interval`, minimum 15 s; `--once` runs one cycle) it reads new mail read-only (messages stay unread; the first connection starts at the mailbox's current end, so earlier mail is never imported) and stores each Message-ID once. OpenAI (`OPENAI_API_KEY`, model `ORDER_AGENT_MODEL`, default `gpt-5.5`, `store=false`) extracts structured booking facts only; deterministic code then maps units to kg/cm, verifies addresses through Google Geocoding (`GOOGLE_GEOCODING_API_KEY`, falling back to `GOOGLE_ROUTES_API_KEY`; requires `ROUTING_PROVIDER=google` and the Geocoding API enabled) and validates the shared `Booking` schema. An Order is created automatically only when every required fact is present, the sender is an active Shipper's email and the receiving provider's topmost `Authentication-Results` shows DMARC or aligned DKIM pass; the agent then books through `orders.create_order` as that Shipper's own account, recorded as `AGENT`, with source `EMAIL`. Otherwise the email waits for a dispatcher as `NEEDS_REVIEW` (with the exact missing facts), `UNKNOWN_SENDER`, `NOT_AN_ORDER` or `FAILED` (AI errors retry up to three times). Emails that are not Orders yet appear as Draft rows in the dispatcher's Orders list, where they can complete a draft in the normal order form, link an unknown sender to a Shipper, ask the agent to read an email again, or discard it. Logs contain error class names only.

## Dispatch agent

For an unassigned, priced Order the agent lists every active driver: drivers that fail a hard check (no attached vehicle, off duty, another active or started Route, service area, qualifications, equipment, capacity, windows, shift, maximum work time) are excluded with the check's own message; the rest are feasible candidates with metrics (estimated extra fleet driving, distance to pickup from fresh GPS or the home address, Orders already on the planned Route it would join, slack before the earliest deadline, Shipper-requested driver, booked vehicle type). Screening uses the built-in travel estimator, so it never calls a paid routing provider; the assignment command re-checks everything with road travel before saving. OpenAI (`OPENAI_API_KEY`, model `DISPATCH_AGENT_MODEL`, default `ORDER_AGENT_MODEL`, reasoning `DISPATCH_AGENT_REASONING`, `store=false`) only orders those candidates and writes a one-line reason; it sees metrics and Order counts, never addresses, contacts, prices or notes. It cannot add or drop a candidate, and any AI failure falls back to the rule score (`ranked_by: RULES` with an `error_code`). Every evaluation is stored in `dispatch_decisions`.

- `POST /companies/{slug}/orders/{id}/dispatch-suggestions` (dispatcher; `?refresh=true` re-evaluates, otherwise a suggestion under two minutes old is reused) and `GET .../dispatch-decisions` (history).
- `POST /companies/{slug}/dispatch-decisions/{id}/approve` (`version`, `driver_id`, Idempotency-Key) assigns a recommended driver through the shared `assign` command, joining that driver's planned Route when there is one.
- Company settings `dispatch_mode` is `MANUAL` (default) or `AUTO`. In AUTO the worker assigns priced `NEW` Orders scheduled within `DISPATCH_AGENT_HORIZON_HOURS` (default 12) to the best candidate as a system dispatcher; the audit row has no user and the event actor type is `AGENT`. When the command refuses a candidate, the next one is tried (up to three). With no driver the decision is `NO_CANDIDATE`, Monitor shows a `NO_DRIVER` Needs Attention item with the most common reason, dispatchers are notified once per Order version, and the Order is evaluated again every five minutes. Dispatchers are notified of each automatic assignment.

```sh
.venv/bin/python -m app.dispatch.worker
```

`--interval` defaults to 30 s (minimum 10 s); `--once` runs one cycle. Late-delivery re-planning and reassignment of already assigned Orders are not part of this agent yet.

## Events, sync and notifications

Audited company mutations append rows to `events` and create recipient `notifications` in the same transaction (`app/events`). Every web and native client polls `GET /api/v1/companies/{slug}/sync?cursor=` and waits the returned `next_poll_ms`; set `SYNC_POLL_MS` (default 15000, bounded 1000–300000) to tune it per deployment without client changes. A worker or later agent reads the log with `app.events.feed.consume`/`advance` under its own consumer name. Events are not yet pruned, and remote push and email for notifications are later slices.

## Verification and contracts

```sh
.venv/bin/python -m app.export_openapi
cd ../client
npm run generate:api
npm run lint
```

For tests, migrate a disposable PostgreSQL database named `*_test`, set `TEST_OWNER_DATABASE_URL` and `TEST_DATABASE_URL` to its owner/restricted logins, and run `.venv/bin/python -m pytest -q`. Tests truncate that database. They use explicit demo travel or mocked Google responses; they make no billable provider calls. See [state.md](state.md) for verified scope and outstanding integrations.

The [operations module map](app/operations/README.md) describes command and query ownership and the future automation boundary. Tenant-scoped outbox leases and retries support the quote/invoice email worker and future automation workers. This local workspace runs the email worker separately; a deployed environment must start and supervise it explicitly. Order listing accepts tenant-scoped `date_from`, `date_to`, `status`, `service_id`, `pricing_status`, `search`, `after`, and `limit` filters. Archive commands require the current record `version` and an `Idempotency-Key`; they preserve historical Orders and block active dependencies. Normal Shipper/Driver/Vehicle lists omit archived rows; dispatcher reads can request `include_archived=true` for historical records. New Drivers receive a stable organization-unique number from the API.

## Web API contract

The original dispatcher UI and role portals now call these operational endpoints while retaining their established layouts. `/prototype` remains a separate local demonstration. Booking actors can read `/booking-preferences` (units/timezone/currency only); dispatcher quote history uses paginated `GET /quotes`; driver issue reports use `POST /driver/stops/{id}/issue`. Proof and reporting projections are typed in OpenAPI. See the [client state](../client/state.md).

## Native Android driver client

The [driver app](../driver/README.md) uses `POST /api/v1/auth/driver/login` with `{organization, login_id, password}` and sends the returned opaque token as `Authorization: Bearer dm_…`, plus `X-Requested-With: Dispatra`. It does not send a browser session cookie or spoof an Origin. Browser CSRF rules are unchanged. `POST /api/v1/auth/driver/password` returns a replacement native session; logout and administrative revocation invalidate these same sessions.

Migration `0019_driver_notifications` stores private assignment/release inbox records. `GET /api/v1/companies/{slug}/driver/notifications` lists the driver’s newest messages; `POST /driver/notifications/{id}/read` requires version and idempotency key. The inbox is polled by the app; Firebase remote push is not configured.
