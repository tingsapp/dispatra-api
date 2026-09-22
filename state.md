# Dispatra API Implementation State

Last updated: **2026-09-14**

Read [spec.md](spec.md) for the target contract and [AGENTS.md](AGENTS.md) for implementation rules. This file reports observed implementation, not the entire planned platform.

## Current stage

The first company/customer access milestone is implemented. Operational Orders, pricing, dispatch, routing, driver execution, POD and billing remain unimplemented backend domains.

## Implemented

- PostgreSQL models and versioned Alembic migrations for organizations, customer accounts, users, sessions, audit, idempotency records, login attempt limits and transactional account-event outbox records.
- Platform-owner bootstrap through an interactive local command; owner authentication and company provisioning with the company's first dispatcher account.
- Unique company slug and permanent organization UUID. Shared tenant-owned tables use forced row-level security, transaction-local organization/customer context, composite tenant/customer relationships and a restricted runtime database role. HTTP requests reject direct or inherited table-owner privileges and privileged runtime roles.
- Dispatcher-created customer records and login credentials; no public signup. A customer can log in immediately without a forced password change. This milestone has one login per customer account.
- Customer profile read/update for own contact name, email, phone and address only, with optimistic versions and omitted-field preservation. Business identity and administrative fields cannot be changed through this endpoint.
- Optional password changes, dispatcher password resets, Argon2 hashes, opaque hash-stored sessions, cookie authentication, session expiry/revocation, exact-origin mutation checks, redacted validation errors and no-store responses.
- Database-backed attempt limits; tenant-scoped customer numbers and login IDs; cursor pagination; transactional create/profile idempotency, audit and outbox. Credential-bearing idempotency payloads use slow Argon2 verifiers and never store plaintext credentials.
- Typed Pydantic/OpenAPI contract, generated web types, readiness/process endpoints, dependency lockfile, local setup documentation and optional PostgreSQL Compose configuration.

## Scope and remaining work

The old API contained only welcome/health endpoints. The frontend account milestone now integrates with this API, while the existing dispatch prototype's customers/orders/settings remain browser-local. No automatic import assigns that data to a company. Full customer commercial/default/address models and an explicit field-preserving import require later integration.

No operational Order endpoints, pricing authority, database-backed dispatcher Monitor, booking portal, live tracking, driver login/workflow, POD, invoice issuance, email provider, subscriptions/payment processing or outbox delivery worker are implemented. Outbox rows are persisted; no events are delivered externally. Company suspension is represented/enforced, but lifecycle-management UI and additional dispatcher membership management are later slices.

No production database was provisioned or deployed. Local verification uses a disposable PostgreSQL server and test accounts. Production setup still requires operator-chosen owner credentials, restricted database credentials, HTTPS/origin configuration, tested backup/restore, expiry cleanup and deployment infrastructure.

## Verification

See [milestone verification](MILESTONE.md): 16 PostgreSQL integration tests, 64 client tests, TypeScript, production build and the browser journey pass; fresh migration/rollback/schema-parity/RLS checks pass. Tests run against actual PostgreSQL using a non-owner application login; no SQLite substitutes. The account browser journey exercises owner company creation, dispatcher customer creation, immediate first login, profile persistence, optional password change, dispatcher visibility and responsive layout.

## Next milestones

1. Integrate the full customer directory with explicit organization mapping while preserving existing commercial/default fields and historical snapshots.
2. Implement authoritative Order persistence and customer-scoped order viewing, then booking using shared pricing/order rules.
3. Add operational dispatch/routing, driver workflows, POD and invoice integration in separately verified slices.
