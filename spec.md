# Dispatra API specification

Updated: 2026-09-29. This is the target FastAPI/PostgreSQL contract. [Implementation state](state.md) records verified manual functionality and remaining automation work. `MUST` and `MUST NOT` are requirements.

## 1. Authority and scope

The API is the system of record for organizations, accounts, Shippers, Drivers, Vehicles, Orders, pricing, dispatch, Routes, POD, invoices, audit, and events. The client may preview but cannot authorize or finalize these decisions. Use FastAPI, Pydantic, SQLAlchemy/Alembic, PostgreSQL, Decimal money, and versioned OpenAPI. V1 is English-first Canadian local delivery. Payment collection, payroll, marketplace, warehouse, maintenance, and branch administration are outside the initial operational scope.

Dispatra presents itself as an AI-powered automatic dispatch system. AI may recommend, rank, explain, or summarize through authorized application services. Deterministic eligibility, state, tenant, price, capacity, and route rules always govern mutations; agent memory is not system state.

## 2. Vocabulary and lifecycle

- **Order:** Customer price and Invoice unit, with one or more pickup and drop-off requirements. Lifecycle: `NEW → ASSIGNED → IN_PROGRESS → COMPLETED → INVOICED`, or `CANCELLED`. Pricing result, attention flags, Route status, and Invoice status are separate.
- **OrderStop / OrderItem:** Stable requested stops and linked physical items. Every item has a pickup and later delivery; multiple pickups/drop-offs and interleaved valid sequences are supported.
- **Route / RouteStop:** Optimized executable visit sequence, possibly containing multiple Orders. A Route binds one driver and one actual fleet vehicle through one active Assignment.
- **PricingSnapshot / ChargeLine:** Versioned, immutable priced result and its auditable parts. Invoice consumes the finalized snapshot; reassignment does not reprice it.
- **Shipper:** Ordering account and commercial defaults. A separately selected billing customer, stop recipient, and driver are different actors.

Risk, late start, pricing review, and failed attempts are attention flags or execution exceptions, not extra Order lifecycle values. Unknown data must remain unknown instead of becoming a fabricated status, coordinate, price, or GPS sample.

## 3. Architecture and tenancy

Use a modular API with application services for accounts, pricing, dispatch, routing, execution, POD, billing, events, and agent tools. Handlers authenticate, authorize, validate, invoke use cases, and return typed responses. A PostgreSQL organization UUID owns tenant records; company slug is a URL identifier, never authorization. Use a restricted runtime database role, forced row-level security, transaction-local tenant/customer context, composite tenant relationships, and cross-tenant non-disclosure. No public signup.

Retryable mutations use payload-bound idempotency keys, expected versions, audit, and a transactional outbox. Outbox consumers claim tenant-scoped leases, use the event ID for provider-side idempotency, and acknowledge or retry without holding a database transaction across external calls. Reusing a key with another payload fails. Background work reports accepted versus completed states accurately. Freeze migrations; do not edit an applied migration to change the contract. Provide least-privilege projections for Shipper and Driver portals.

## 4. Entities and invariants

Every mutable tenant record has a stable ID, organization scope, audit timestamps, and version. Directory deletion archives Rate Cards, Shippers, Drivers and Vehicles; it revokes affected login access and blocks retirement that would interrupt active Orders or Routes. Archived Shippers, Drivers and Vehicles disappear from ordinary lists, remain available for authorized historical reads, and cannot be edited through ordinary profile commands. Historical booking and invoice snapshots remain intact. Operational addresses, contacts, commercial choices, and pricing used by an Order are snapshots; later directory edits do not rewrite history. Preserve legacy fields for historical reads without putting removed controls back in new forms.

Company Profile settings accept a manually entered correspondence address or a retained structured address; operational Driver/Shipper/stop addresses remain structured. Company PNG/JPEG logos are bounded to 200 KB. Saving Company name also updates organization identity. Account email and role are read-only in the company Profile UI.

### 4.1 Order, Shipper, and commercial records

- **Shipper:** Required display name and email login; Business additionally requires Company name, Individual does not. One phone and one complete Warehouse Address; the address is the default pickup, not a separate Service Area setting. Store rate card, payment terms, discount, instructions, account status, and legacy identities. Assign the customer number server-side. The single email is used for portal login, quotes, and invoices unless an explicitly versioned billing-payer model applies.
- **Shipper account:** Creating the Shipper and its shipper-scoped user occurs in one transaction. Shipper identity (number, name, contact, email, phone, status) and commercial profile live in one `shippers` record; legacy account-only records have no warehouse and are excluded from operational use. The account role is `SHIPPER`; any role rename requires a versioned migration. Generate an initial password and show it once; store only a secure hash. Enforce unique email login within the organization and appropriate portal scope. A Shipper sees only its own profile and authorized Orders/invoices.
- **Order:** Server-generated number; ordering Shipper or explicit prospect quote; service, rate card, scheduled/ready/deadline times, selected Accessorials, stop/item links, booking and payer snapshots, pricing status/snapshot, version, and lifecycle. Do not persist decorative list counters as truth. A Quote does not create an Order. Prospect quotes choose an active Rate Card without an existing Shipper. Authenticated Shippers can create and view their own Orders through the same booking/pricing service; their account identity and allowed commercial terms come from the session. Invalid or incomplete pricing remains explicit Needs Attention and blocks assignment. A booking may name an optional `preferred_driver_id` (a current active company Driver): a dispatch preference shown to the dispatcher, never an assignment, and it does not change the price. Every Shipper carries a Rate Card; a Shipper saved without a choice receives the company Default. When a distance card lacks distance or an hourly card lacks minutes, pricing obtains both from one Google Routes request over the Order's own stops (estimated minutes = driving time plus each stop's service minutes) and freezes them in the pricing context; Shipper projections never include that context.
- **Order history:** Customer and billing snapshots, finalized prices, invoice lines, and previous versions remain readable and unchanged after directory or pricing configuration edits. A permitted explicit reprice creates a new snapshot, never overwrites a final one.

### 4.2 Stops, cargo, and execution

Each OrderStop has stable ID, type, sequence, address/coordinates when resolved, contact snapshot, optional timing and instructions, and supplying pickup IDs. Each OrderItem has stable ID, positive quantity, physical weight/dimensions, handling unit, fragile/DG and other applicable handling tags, and explicit pickup/delivery IDs. Residential is an Accessorial choice, not a duplicate stop checkbox. No manual zone field is required; Zone pricing resolves from the configured address/postal mapping.

Validate every item link and pickup-before-delivery precedence. A failed or short pickup blocks only its dependent quantities. RouteStops preserve distinct visits even at the same address. Across the full proposed Route, physical load must remain between zero and actual vehicle capacity after every stop; cargo must fit the actual vehicle and required equipment. Unknown dimensions or qualifications cannot pass silently. Driver arrival, quantity confirmation and POD use stable revisions, assignment generations, and explicit actual loaded/unloaded quantities.

### 4.3 Driver, Vehicle, and telemetry

- **Driver:** Stable profile and least-privilege `DRIVER` account created transactionally. Name, phone, email login, and Canadian address are required. Derive the service city from the address; an edited address changes future coverage. Issue an organization-unique Driver number on the server, independent of the login email and row UUID. Preserve historical service-area IDs until a deliberate migration. Keep account, duty, work, app connectivity, GPS freshness, and location permission independent. A dispatcher Driver edit can open or close the duty session atomically with profile changes, with version checks and an audit record. It never grants location permission or starts GPS tracking. Store employment, optional owner-operator order-price and fuel-surcharge payout shares, vehicle link, availability/qualifications, and optional individual maximum active Orders override. New drivers inherit the organization default without storing an override.
- **Vehicle:** Actual fleet identity, plate/province, selected type, capacity/weight/volume/dimensions, equipment, optional positive Maximum stops, availability, and current driver/route links. Length, width, and height are required in new vehicle creation. Vehicle-specific pricing/capacity edits do not alter the shared type or another vehicle. Legacy surcharges/licence flags remain readable but are not new-form inputs.
- **Telemetry:** Capture time, server receipt time, accuracy, duty session, app last seen, and location permission. Connectivity derives from timestamps; missing/invalid/future telemetry is unknown. A dispatcher edit or push cannot start mobile GPS. Driver-initiated location sharing controls tracking; local End Duty stops it immediately, even offline. Dispatcher duty changes do not create telemetry.

A vehicle cannot be actively assigned to two drivers/Routes. Profile edits cannot release an in-use vehicle or alter loaded-goods custody; use an authorized assignment transition. Enforce organization-scoped unique numbers and plate/jurisdiction constraints.

## 5. Accounts and URL contract

The public site is `/`; platform owner administration is `/admin`; a dispatch company's Monitor is `/{company-slug}/`. Dispatcher pages live beneath it, including `/{company-slug}/shippers`. The authenticated Shipper portal is `/{company-slug}/shipper-portal`; the Driver portal is `/{company-slug}/driver`. Older `/platform`, `/{slug}/dispatch`, and `/{slug}/customer` client paths are compatibility routes. Reserve route words such as `admin`, `api`, `platform`, `prototype`, `ready`, and `login` from company slugs.

The platform owner provisions companies and first dispatchers. Exactly one `ADMIN` exists; it is created and recovered only by the interactive `app.bootstrap` command with migration credentials, never through HTTP or with default credentials. Owner sessions use the same cookie, hashing, expiry, rate-limit, origin and no-store rules; they cannot use company endpoints or impersonate a company account. Platform endpoints list/search, inspect and rename companies (the slug is immutable), suspend or activate them, show administration audit history, and create, edit, activate/deactivate, reset and sign out dispatcher accounts. Every mutation checks the record version, is idempotent and audited. Generated dispatcher passwords are returned once and never stored in replay results or audit. The last active dispatcher of a company cannot be deactivated. Suspension revokes every company session and open driver duty and blocks company login (a correct password receives an explicit suspension response); activation does not restore sessions. Company history, Orders, invoices and audit records are never deleted by these commands. Driver/Shipper email is the login ID, with a generated one-time initial password. Use Argon2 hashes, opaque hash-stored sessions, expiry/revocation, attempt limits, exact-origin mutation checks, secure cookies, no-store responses, and redacted credential handling. Do not force a first-login password change unless the product decision changes. Password reset revokes old sessions. Customer profile writes cannot change role, tenant, commercial terms, or identity outside allowed fields.

## 6. Pricing authority

The latest simplified [web pricing surface](../client/spec.md#6-pricing-and-company-profile) is the new-quote contract. Preserve older saved cards and frozen quotations through versioned compatibility. Use Decimal/numeric monetary values and explicit cent rounding. Company units are display/edit conventions; canonical physical values remain consistent.

Active new Rate Cards use `BASE_PLUS_DISTANCE`, `FIXED`, `ZONE`, or `HOURLY`; existing `IMPORTED` cards remain supported for historical/edit flows. Resolve an explicit permitted Order card, then the Shipper's attached card, then the organization Default; ties or missing required rates produce a review error, never an arbitrary price. A Zone card uses a single central pickup, destination-zone columns, and weight From/To bands. A new card starts with 0–99 lb and $20 for Zone 1. The configured zone is resolved from the destination address/postal mapping; no matching price means Needs Attention. Chargeable Zone weight uses the greater of actual and dimensional weight where configured. Base + Distance does not charge weight; package weight still constrains actual vehicle fit.

Service Level is a fixed additional charge, Vehicle and Accessorial charges are configured separately, Fuel Surcharge is a percentage of eligible positive charges, and the Shipper owns its discount. A Rate Card owns its minimum subtotal. Ordinary delivery pricing does not charge routine travel time; Hourly contracts and explicit recorded waiting are separate. Imported final agreed totals bypass new modifiers according to their frozen terms.

### 6.1 Formula and tax

For a new quote, calculate method-specific freight, then permitted service, vehicle, fuel, Accessorial, and adjustment lines, less discount. Apply the card minimum to the resulting subtotal before tax:

```text
subtotal = max(minimum, freight + service + vehicle + fuel + accessorials + adjustments - discounts)
tax = GST/HST taxable base × enabled GST/HST rate
    + provincial taxable base × enabled provincial rate
total = subtotal + tax
```

Each tax is a separate ChargeLine. The optional GST/HST registration number is invoice information; it does not silently toggle either tax. Disabled rates do not charge. Explicitly flagged special/imported/exemption conflicts require tax review before assignment or invoicing. Freeze tax rules, rates, and taxable bases in the quote snapshot. Historical destination/profile tax decisions remain unchanged until an allowed explicit reprice.

### 6.2 Snapshots and locks

Freeze card ID/version, formula inputs, method, charge lines, discounts, minimum, tax, subtotal, total, and relevant booking context in a versioned PricingSnapshot. Rate changes never mutate an existing quote, final price, or Invoice. Reassignment, merged-route distance, deadhead, and driver changes do not change the customer price. Completion-dependent approved usage may finalize only under the locked quoted rules. Keep customer-priced distance separate from actual operational route distance and internal cost.

## 7. Dispatch and eligibility

There are exactly `AUTO` and `MANUAL` modes. AUTO evaluates a priced, ready Order and assigns a feasible driver and actual vehicle; MANUAL leaves it unassigned until dispatcher selection. Both modes apply identical hard checks and automatically optimize afterward. No feasible candidate creates a specific Needs Attention reason. An individual driver's maximum active Orders override takes precedence over the company default and applies in both modes.

Check tenant ownership, active accounts, driver duty/work state, address-derived pickup service city, vehicle type/equipment/capacity, qualifications, shift/availability, maximum work time/stops, Order windows, quote validity, item links, physical load after each stop, route state, and custody. A text skill alone does not prove a two-person crew. AI may rank feasible candidates, but cannot assign in MANUAL or waive hard checks. Preserve reasons, scores, chosen candidate, mode, actor, versions, and audit.

## 8. Routes and driver execution

Optimize multiple Orders with pickup/drop-off precedence, capacity, time windows, service duration, locked visits, distance, and ETA. Return stable RouteStops with planned loads and revision. Do not silently mutate locked/in-progress Routes or insert into active Routes without an explicit safe command. The Driver app receives only its own assignment and allowed actions. Route start needs online validation; authorized work on a started cached Route may continue offline. Failed pickup dependencies and exception history remain explicit.

## 9. POD, completion, and Invoice

Resolve server POD policy per delivery visit. Receiver present requires name and signature, plus a photo when policy requires one. Unattended delivery requires permission, safe placement, and photo; otherwise record a failed attempt. A client `NONE` request cannot waive server evidence. Protect evidence through offline retry and accept it with tenant/version/idempotency checks before completion.

After valid completion and an authorized manual invoice request, finalize permitted actual charges under the frozen rules, persist one final PricingSnapshot, and create one active Invoice from its ChargeLines and billing snapshot. A separate authorized send command queues an email from that frozen Invoice to its billing email; a priced, unexpired prospect Quote can be sent to an explicitly entered recipient. Email delivery has independent pending, sent, failed, and uncertain states, and an uncertain SMTP acceptance must not trigger automatic resend. Missing POD, tax/price conflicts, or billing identity hold invoicing for review. Payment collection is outside this invoice workflow.

## 10. Agentic workflows, events, and audit

The manual services and transactional events are built first. In the later automation phase, agents may extract emailed bookings, recommend feasible assignments, explain exceptions, summarize operations, and invoke approved application commands under the actor's permissions. They never write tables directly or bypass idempotency, tenant scope, pricing, capacity, locks, POD, or audit. Provider failure does not corrupt committed state. Outbox events notify clients and workers; clients recover missed, duplicate, or out-of-order events from authoritative snapshots/versions.

## 11. REST/OpenAPI contract

Expose versioned `/api/v1` typed resources, cursor pagination, structured errors with request IDs and field details, expected versions, and idempotency keys. Generate client types from OpenAPI. Role-specific endpoints return minimum necessary fields; a hidden UI control is not authorization. The manual operational API extends the account API with company settings, catalogues, Shippers/Drivers/Vehicles, Quotes, Orders, Route execution, evidence, invoices, and reporting. The Order list supports tenant-scoped date, lifecycle, service, pricing-state and literal-text search with bounded pagination. Monitor derives pricing and timing attention from stored Orders, deadlines and planned visits; these are not lifecycle states. Consult state.md for verified coverage. The current phase exposes MANUAL assignment and explicitly requested invoice creation; email intake and AUTO workflows follow later.

### 11.1 Frontend compatibility and driver projections

Map historical `Job`/customer and local pricing terms into the canonical Order/Shipper contract without changing frozen commercial history. Keep legacy customer contacts, service-area IDs, old Rate Card methods/discounts, tax modes, and invoice previews readable where applicable. Do not import browser localStorage into an arbitrary company. Seed current demo pricing automatically for new companies and through an explicit idempotent command for existing companies; reruns preserve edited values. Seeds have explicit organization ownership and include Rate Cards, service levels, vehicle types, Accessorials, fuel, and taxes. An explicit administrator demo seed creates a dedicated company with persistent dispatcher, Shipper and Driver accounts, a linked vehicle, and the same pricing. The explicit demo seed also ensures a single `ADMIN` (`admin@example.com`) when none exists. The four seeded demo accounts use initial password `123456`; the seed preserves edits and credentials on reruns, and must not run implicitly for production companies. A local `InvoicePreview` is not an issued Invoice, and simulated map telemetry is not DriverLocation.

Driver responses contain only own profile, duty, assigned vehicle/Routes/stops, recipient and item snapshots, POD policy, and allowed actions. Exclude customer prices, payer/billing details, unrelated Shippers, private dispatcher notes, and other drivers' locations from API responses, caches, push, and logs. Shipper responses are similarly restricted to that Shipper's data.

## 12. Security and privacy

Apply organization and actor scope to every HTTP query, worker, export, upload, websocket, event, and agent tool. Use TLS, secure secrets, safe media upload, bounded retention, PII-minimized logs/notifications, and tested backup/restore. Public tracking, if enabled, is a token-scoped final-leg projection only; it never exposes a full Route or other customers.

## 13. Required acceptance

Verify tenant isolation with a non-owner PostgreSQL role; account creation/login/reset; unique email and reserved slug rules; quote formulas and historical snapshots; every Rate Card method and tax/minimum/fuel/discount interaction; AUTO/MANUAL eligibility, no-candidate reasons, and locked Route protection; all pickup/drop-off cardinalities and intermediate capacity; driver duty/location boundary; POD and offline idempotency; exactly one Invoice and safe resend; and cross-role field exclusion. A schema, mock, browser preview, or passing build alone is not integrated evidence.

### Web projections

Provide typed Monitor, Analytics, Driver profile/activity and delivery-proof responses; dispatcher Quotes are cursor-paginated. `/booking-preferences` exposes only currency, timezone, measurement units and the company's GST/HST, provincial tax and fuel surcharge switches and rates to booking actors; `/booking-drivers` exposes only the id and name of active Drivers, and the Shipper profile includes the name of the Rate Card that prices its Orders. Driver stop-issue reporting derives the Order from an assigned stop and applies the same route-ownership checks as operational commands. Every evidence/document download enforces session and tenant/customer scope.

## Public entity IDs

Orders, Drivers, Shippers and Vehicles have server-issued public numbers in the format `D{company initial}{entity letter}-{random number}`. Entity letters are O, D, S and V respectively. The company initial is the first Latin letter in the company name, falling back to the slug. Numbers start with four digits and expand only after that company/entity exhausts its combinations. Generation is serialized per company and checked against archived and active numbers; database uniqueness remains enforced. UUIDs remain internal relation keys. Published numbers are stable across profile edits and company renames. Vehicles retain a separate editable Unit number. Existing operational records receive public numbers through migration; frozen invoices and commercial snapshots retain their saved historical text.

## Shipper card setup

Stripe Connect card setup belongs to each dispatch company. An administrator verifies and links an existing connected account, separately for test and live modes. Customer-scoped API endpoints list masked cards directly from Stripe and create hosted Checkout setup sessions after consent. Persist only account/customer references, never PAN/CVC. Tenant/customer RLS, authenticated identity, stable provisioning keys, and setup audit govern this flow. External Stripe requests occur outside database transactions. Ambiguous customer creation past the provider idempotency window requires reconciliation. Invoice terms remain dispatcher-managed. Saving a card does not collect invoice payments, authorize automatic charging, or mark invoices paid.
