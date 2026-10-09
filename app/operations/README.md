# Operational API modules

`routes_*.py` and `reporting.py` are HTTP adapters. They authenticate, validate request schemas, and delegate to the application services below. Business rules do not belong in routes or client components.

| Module | Responsibility |
| --- | --- |
| `schemas.py`, `models.py` | Versioned request/response shapes and tenant-owned PostgreSQL rows |
| `common.py` | Tenant-scoped record access, optimistic versions, transactional idempotency and audit hooks |
| `directory.py`, `archive.py`, `seeding.py` | Company configuration, profiles/accounts, reversible retirement and demo pricing |
| `orders.py`, `pricing.py`, `quotes.py` | Booking, authoritative price snapshots, Quotes and immutable completed Order prices |
| `dispatch.py`, `routing.py`, `travel.py` | Eligibility, assignment, bounded route optimization and travel provider boundary |
| `execution.py`, `issues.py` | Driver duty, stops, evidence, completion and exceptions |
| `queries.py`, `attention.py`, `reporting.py` | Tenant-scoped Orders/Monitor/Analytics projections, derived attention and date-filtered exports |
| `outbox.py` | Internal tenant-scoped event leases, retry backoff and acknowledgement for workers |
| `mail.py`, `smtp_transport.py`, `email_worker.py` | Explicit Quote and delivery-update email requests, TLS SMTP transport and a separate delivery worker |
| `app/events` (`publish.py`, `rules.py`, `feed.py`, `routes.py`) | Commit-time company events from `audit`, notification rules, gap-free client/consumer cursors, `/sync` and the notification inbox |

New integrations and automation must call these application commands with an authenticated, organization-scoped actor and stable idempotency key. They may request a preview or propose a command, but must not write operational tables directly. Each accepted mutation records audit and outbox events in the same database transaction. Workers claim events through `outbox.py` using a tenant context, commit the lease, perform the side effect with a stable event identity when the provider supports idempotency, then acknowledge or schedule retry. Gmail SMTP has no such provider guarantee, so this worker stops automatic resend when acceptance is uncertain. An event or claim is not proof of email delivery or agent completion. Verified completion retains the booked price and evidence. The email worker handles explicit Quote sends and configured delivery updates; it never sends financial documents. Other event consumers can be added without changing domain commands. Dispatch mode and email intake are separate policies. Changing those policies later should add orchestrators and provider adapters without duplicating pricing, assignment, route or pricing rules.

Migrations are append-only in `migrations/versions`. Directory retirement uses `archived_at` so the editable Inactive status remains distinct from removal. Browser demo stores are never imported implicitly. Existing Rate Cards, booking snapshots, pricing revisions remain readable after profile or catalogue retirement.
