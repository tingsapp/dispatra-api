# Dispatra API Agent Instructions

## Authority and stack

- FastAPI/Python, Pydantic, SQLAlchemy/Alembic, PostgreSQL and agentic workflows are fixed V1 technology.
- The API is authoritative for tenant isolation, domain state, pricing, dispatch, route optimization, POD, audit, events and permissions. PostgreSQL is the system of record; money uses `Decimal`/numeric types, never binary floats.

## Architecture rules

- Thin routers: authenticate, authorize, validate typed schemas, call a use case, translate errors, return a typed response.
- Separate domain/application services and repositories for Pricing, Dispatch, Routing, Order lifecycle, eligibility, POD, events and workflows; no giant service class. Pricing formulas, state transitions, hard route constraints and assignment decisions are centralized, never duplicated in handlers, agents or clients.
- Every state-changing operation validates the state machine, tenant, versions, assignment generation, permissions and idempotency key in a transaction.

## Fixed product rules

- Exactly `AUTO` and `MANUAL` dispatch, by direct dispatcher or system assignment. Both optimize automatically after assignment. Drivers cannot self-assign or change price.
- Orders are the commercial/pricing unit with multiple pickups and drop-offs; stable-ID RouteStops belong to Routes, which hold many Orders and bind one driver plus one vehicle.
- Locked planned routes cannot change. Executing routes accept only validated live insertions through the shared assignment command: preserve completed/current visits and cargo custody, require fresh accurate GPS, gate further execution on driver acknowledgement. Capacity, precedence, tenant, state and POD rules cannot be overridden.
- Customer price is independent of driver, deadhead, merged route distance, workload and reassignment. Snapshot booking/billing/contact data; preserve historical pricing. Separate ordering customer, billing customer and stop recipient.
- Required POD and idempotent completion are V1. Stop POD fields are requests; return authoritative evidence requirements without weakening receiver-present/unattended rules. V1 has no invoices, payment terms or payment collection.
- Stable OrderStop/OrderItem identities with explicit pickup/delivery links. Validate per-leg physical load across the whole proposed Route, independently of chargeable weight. Never infer compatibility from unknown data.
- VehicleType (pricing) is distinct from the assigned fleet Vehicle. Enforce driver/vehicle ownership and active-route custody transactionally.
- Account, duty, work, app connectivity and GPS freshness are separate. A dispatcher edit or push cannot start mobile tracking; preserve local End Duty boundaries.
- Driver schemas are least-privilege: no commercial snapshots, payer details, internal notes or unrelated customer data.
- Defaults initialize Orders; directory edits never alter existing bookings. Customer self-service writes only explicit profile fields; account terms and business identity stay administrative.
- Do not copy prototype display enums, free-text identifiers, fake telemetry or browser feasibility shortcuts into production contracts.

## Agentic workflows

Agents may explain, rank feasible choices, orchestrate approved use cases, notify and summarize. They never write directly to the database or bypass authorization, pricing, capacity, route validation, idempotency or audit. Agent memory is not system state; provider failure must not corrupt committed state.

## Security and tenancy

Shared PostgreSQL with transaction-local organization/customer context, forced RLS and composite tenant relationships. Scope every query, upload, worker, export, event and websocket by organization; never leak cross-tenant existence. Never run HTTP handlers with migration/owner credentials. `app.bootstrap` creates the first platform owner; public trial signup may create only a new company and its dispatcher. Argon2 password hashes, opaque hash-stored session tokens, no forced first-login password change. PII-minimized logs, safe uploads, rate limiting. Test RLS on real PostgreSQL with a non-owner role (SQLite is not isolation evidence). Never edit frozen Alembic migrations.

## Testing

Add focused unit/integration tests for tenant isolation, state, concurrency, idempotency, pricing, routing, POD and completion as the change requires; update OpenAPI (`openapi.json`) when the contract changes. A spec, mock or route declaration is not proof of implementation. For documentation-only work, check terminology, links and the diff; don't claim test results.
