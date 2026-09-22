# Dispatra API Agent Instructions

Last updated: **2026-09-14**

Read `spec.md` and `state.md` before work. `spec.md` is normative; `state.md` is verified status. Current user requirements outrank both.

## Authority and stack

- FastAPI/Python, Pydantic, SQLAlchemy/Alembic, PostgreSQL, and agentic workflows are fixed V1 technology.
- The API is authoritative for tenant isolation, domain state, pricing, dispatch, route optimization, POD, invoices, audit, events, and permissions.
- Use PostgreSQL as system of record. Use `Decimal` and numeric database types for money; never binary floating point for authoritative currency.

## Architecture rules

- Keep routers thin: authenticate, authorize, validate typed schemas, call an application use case, translate errors, return a typed response.
- Separate domain/application services and repositories for Pricing, Dispatch, Routing, Order lifecycle, eligibility, POD, Billing, events, and workflows. Do not build a giant service class.
- Centralize pricing formulas, state transitions, hard route constraints, and assignment decisions. Do not duplicate them in handlers, agents, or clients.
- Invoice service consumes finalized `PricingSnapshot`/`ChargeLine` records and never recalculates an Order independently.
- Every state-changing operation validates the state machine, tenant, versions, assignment generation, permissions, and idempotency key in a transaction.

## Fixed product rules

- Exactly `AUTO` and `MANUAL` dispatch modes; use direct dispatcher or system assignment only.
- Orders are the commercial/pricing/invoice unit. Multiple pickups and drop-offs are mandatory; stable-ID RouteStops belong to Routes, which contain many Orders and bind one driver plus one vehicle.
- Drivers cannot self-assign or change price. Both modes automatically optimize after assignment.
- Never mutate locked/in-progress routes automatically. Hard capacity, precedence, tenant, state, and POD rules cannot be overridden by AI or a dispatcher.
- Customer price is independent of driver, deadhead, merged route distance, workload, and reassignment.
- Required POD and invoice generation/idempotency are V1 behavior.

## Agentic workflows

Agents may explain, rank feasible choices, orchestrate approved use cases, notify, and summarize. They MUST NOT write directly to the database or bypass authorization, pricing, capacity, route validation, idempotency, or audit. Agent memory is not system state. Provider failure must not corrupt committed domain state.

## Security and tenancy

Apply organization scope to every query, upload, worker, export, event, and websocket. Never leak cross-tenant existence. Use TLS, secure secrets, least privilege, safe uploads, PII-minimized logs, rate limiting where appropriate, and immutable pricing/invoice history.

## Testing and workflow

Read relevant code/tests, implement a cohesive slice, add unit/integration/tenant/state/concurrency/idempotency/pricing/routing/POD/invoice tests, update OpenAPI, and record actual results. Test all pricing methods, Rate Card precedence/conflicts, AUTO/MANUAL eligibility, multi-stop precedence/capacity, locked routes, offline POD, completion, invoice retries, audit, and tenant isolation. Update `state.md` after implementation changes. A spec, dependency, mock, or route declaration is not proof of implementation.

## Entity and client contract reconciliation

- Implement the field groups and invariants in `spec.md` §§4.1–4.3 and the compatibility mappings in §11.1; add contract tests for those mappings. Consult [client spec](../client/spec.md), [client state](../client/state.md) and [driver spec](../driver/spec.md); frontend fixtures inform acceptance but never grant API authority.
- Separate ordering customer, billing customer and stop recipient. Snapshot booking/billing/contact data and preserve historical pricing and invoices. Defaults initialize Orders; directory edits do not alter bookings.
- Use stable OrderStop/OrderItem identities and explicit pickup/delivery links. Validate per-leg physical load across the entire proposed Route, independently of chargeable pricing weight. Never infer compatibility from unknown data.
- Keep VehicleType pricing selection distinct from the assigned fleet Vehicle. Enforce driver/vehicle ownership and active-route custody transactionally; profile edits cannot bypass assignment commands.
- Separate account, duty, work, app connectivity and GPS freshness. A dispatcher edit or push cannot start mobile tracking. Preserve local End Duty boundaries during reconciliation.
- Treat stop POD fields as requests. Resolve and return authoritative evidence requirements without weakening receiver-present/unattended-delivery rules.
- Generate least-privilege driver schemas; exclude commercial snapshots, payer details, internal notes and unrelated customer data at the API boundary.
- Preserve explicit method/source/discount mappings until a versioned client migration replaces them. Do not copy prototype display enums, local invoice previews, free-text identifiers, fake telemetry or browser feasibility shortcuts into production contracts.
- For documentation-only work, verify cross-document terminology, relative links, status claims and diffs. Do not run unrelated app builds or claim runtime/device/API test results. Keep `state.md` explicit about source inspection versus planned requirements.

## Company/customer access

Shared PostgreSQL with transaction-local organization/customer context, forced RLS and composite tenant relationships is the approved first implementation. Never run HTTP handlers with migration/owner credentials. Use `app.bootstrap` for the first platform owner; public signup is prohibited. First login does not require a password change. Customer self-service uses explicit profile fields; account terms and business identity remain administrative. Passwords are Argon2 hashes and session tokens are opaque/hash-stored. Test actual PostgreSQL RLS with a non-owner role; SQLite is not isolation evidence. Preserve frozen Alembic migrations independently of future ORM model changes.
