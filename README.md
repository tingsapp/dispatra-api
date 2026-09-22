# Dispatra API — company/customer access milestone

FastAPI + PostgreSQL + SQLAlchemy/Alembic. The API now supports platform-owner company provisioning, first dispatcher credentials, dispatcher-created customers, customer profile completion, password changes/resets, opaque server sessions, audit and transactional account-event records. Orders and operational services remain future work.

## Local setup

Use Python 3.12+ and PostgreSQL 16+. Start a local PostgreSQL server or use `docker compose up -d db`. Set `POSTGRES_PASSWORD` before using Compose; no default password is provided. Never expose PostgreSQL publicly.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
cp .env.example .env
```

Edit `.env` with your actual database credentials. `MIGRATION_DATABASE_URL` is a privileged migration/owner connection; `DATABASE_URL` is a different, restricted application login. `.env` is ignored and must not be committed. Load it in your shell before running migrations/bootstrap; Uvicorn also supports `--env-file .env`.

```sh
set -a
. ./.env
set +a
.venv/bin/alembic upgrade head
```

The migration administrator needs permission to create the `dispatra_app` NOLOGIN group role (or an administrator must pre-create it). Create a separate runtime login through PostgreSQL administration:

```sql
CREATE ROLE dispatra_runtime LOGIN NOSUPERUSER NOBYPASSRLS;
GRANT dispatra_app TO dispatra_runtime;
```

Set its password using the PostgreSQL `\password dispatra_runtime` prompt. Put that login in `DATABASE_URL`. It must not own the database tables or inherit an owner/superuser role. Tenant tables use forced row-level security. Runtime requests reject direct owner/superuser/BYPASSRLS credentials.

Create the platform owner interactively, once:

```sh
.venv/bin/python -m app.bootstrap
.venv/bin/uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

In `../client`, run `npm ci` and `npm run dev`. Vite proxies `/api` to port 8000 (`API_PROXY_TARGET` can override it). Open `http://localhost:3000/platform` and use the owner credentials you chose. Create Acme, copy the dispatcher credentials, sign out, and open `/acme/dispatch`. Create a customer, copy its credentials, sign out, and open `/acme/customer`. Customer settings allow an optional password change; no first-login change or public signup exists.

The existing browser-only dispatch prototype stays at `/` and `/prototype`; its records are not automatically copied into new organizations. Authenticated company URLs currently provide the account-management milestone, not the entire operational dispatcher app. Existing browser records require an explicit, field-preserving company migration before operational integration.

## Authentication and isolation

- Passwords use Argon2; only hashes are stored. Login IDs are lowercase, unique per organization (platform owner uses a separate scope). This milestone has one portal login per customer.
- Browser-generated initial/reset passwords are transient. The UI can copy login details, but no plaintext password is returned by read APIs, retained in operation results or audit, or written to localStorage.
- Opaque 12-hour sessions use HttpOnly, SameSite=Strict cookies. Password changes revoke other sessions and rotate the current one. Dispatcher resets revoke all target sessions. Login/password attempts have persistent database-backed limits.
- Production requires HTTPS, `COOKIE_SECURE=true`, and exact `WEB_ORIGINS`. Use a same-origin reverse proxy for `/api`; there is no permissive CORS configuration. Mutations require the allowed Origin and `X-Requested-With: Dispatra` header. Configure request-size and additional network abuse limits at the deployment proxy.
- Transaction-local organization/customer contexts are established from validated sessions, never a supplied organization ID. API checks enforce role and URL membership. Database RLS and composite customer foreign keys enforce tenant relationships. Global organization/authentication/session/rate-limit tables are private control data with no general client CRUD endpoints.
- Customer profile writes allow only contact name, email, phone and address, with a version check. Directory changes do not yet affect orders because the order API is not implemented.
- Create/profile mutations require `Idempotency-Key`; credential-bearing payload fingerprints use Argon2 to avoid a fast password-verification side channel. Retries with the same payload return the same result, and conflicting reuse fails. Password/session commands are intentionally not automatically retried: after an uncertain change, sign in with the new credentials, or have the dispatcher reset again. Account events contain IDs/action only; an outbox delivery worker is not yet implemented.
- Audit history is append-only for the runtime role. Backups and restores must use an authorized administrative role that can capture all organizations; test restoration before deployment. Schedule expiry cleanup for session/rate-limit/idempotency records before production scale.

## API contracts and verification

```sh
.venv/bin/python -m app.export_openapi
cd ../client
npm run generate:api
npm run lint
npm test
npm run build
```

For integration tests, migrate a disposable PostgreSQL database whose name ends in `_test`. Use a restricted runtime login that inherits `dispatra_app`, and set `TEST_OWNER_DATABASE_URL` and `TEST_DATABASE_URL`. Tests truncate that disposable database; never point them at customer data.

```sh
.venv/bin/python -m pytest -q
```

`GET /health` checks the process. `GET /ready` checks basic database/schema access and role privileges. An operational readiness review, HTTPS deployment, backup automation, provider integrations and subscription billing are outside this milestone.
