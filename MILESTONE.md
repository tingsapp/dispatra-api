# Company/customer access — milestone 1

Implemented on September 14, 2026. This is the account-access foundation, not a production deployment or the complete operational customer portal.

## Delivered

- Platform owner can create a company and its first dispatcher account.
- Dispatcher can create a customer with initial credentials, copy login details and reset access.
- Customer can sign in immediately, complete contact information and optionally change their password.
- Shared PostgreSQL isolation is enforced by the API, restricted database role, RLS and tenant relationships.
- Existing browser-only dispatcher operations and saved customer data are preserved separately.

## Verification

- **16 PostgreSQL integration tests pass** using a restricted non-owner runtime role: account flow, tenant/customer/role isolation, forbidden-field injection, direct RLS enforcement, transaction context reset, composite foreign keys, atomic duplicate handling, idempotent replay/conflicts, concurrency, profile versions/partial updates, Argon2 retry verifiers, password resets/changes/session revocation, origin checks, validation redaction, rate limits, disabled companies/session expiry and pagination.
- **64 existing client tests pass**, along with TypeScript and the production build.
- **Fresh migrations, downgrade/reapply, ORM/schema parity and forced-RLS checks pass.**
- **Real Chrome browser journey passes**: owner → company → dispatcher → customer → profile save/reload → optional password change → dispatcher sees updated profile. Desktop and mobile screenshots were inspected; no horizontal mobile overflow.
- npm dependency audit reported **zero known vulnerabilities** after compatible patches. Existing Vite prototype-bundle size advisory and a Starlette test-client deprecation warning remain.

## Run it

Follow [API setup](README.md) and [client setup](../client/README.md). Choose your own platform-owner credentials through the bootstrap command. No production owner password or public signup route is embedded in the application. The verification used only disposable local test accounts.

## Next slice

Integrate the full customer directory and operational dispatcher screens with explicit organization mapping, then implement persisted customer order viewing and booking using the authoritative shared Order/pricing contract. Subscription billing, live tracking, POD/invoices, external email and driver execution remain later integrations.
