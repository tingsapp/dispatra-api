# Named TMS connectors

The Connectors tab is independent of Connected TMS and Dispatra API. The six catalogue entries are implemented;
**none is live-certified or activated by default**. A card, OAuth handler, or synthetic mapping test does not establish
vendor compatibility. No developer accounts, vendor applications or live TMS records were available for this work.

`connector-contracts.json` records only facts checked in official documentation, including incomplete coverage.
It is research provenance, not a runnable import configuration. Empty known-fields entries mean that an installed
edition's full specification has not been obtained; do not substitute invented generic field names. Rose Rocket
connected money, task, stop and commodity objects need explicit retrieval/transformation, not guessed dotted paths.

## Activation

1. Obtain the vendor's approved application/API access and sandbox. Choose the supported API edition.
2. Install a reviewed JSON profile for that provider under a server-only file referenced by
   `TMS_CONNECTOR_PROFILES_FILE`. `app/connectors/profiles.py` defines the typed contract. It requires a revision,
   API version, validation evidence, HTTPS account verification, bounded collection/pagination definitions,
   complete Dispatra field maps for enabled resources, source currency and source units. Maps never enter API
   responses, query keys, browser storage or user forms. The six formats are installed independently.
3. Register `https://<Dispatra-host>/api/v1/companies/<slug>/integrations/connectors/<provider>/callback` with the
   vendor, set `CONNECTOR_PUBLIC_ORIGIN`, and set each OAuth application's environment-referenced credentials.
   If the vendor does not allow per-company callbacks, adapt registration to an approved shared callback before
   activation; never guess that wildcard callbacks are supported. Registered callback and PKCE acceptance must
   be exercised in the sandbox. Non-OAuth connections collect only their documented connection credentials.
4. Confirm authentication scopes, account identity, payload shape, multi-stop/cargo linkage, per-item units,
   currency and inclusive total/tax semantics. A resource map must not imply that unavailable data exists.
5. Run migration 0047 and supervise `python -m app.connectors.worker` separately from the existing TMS worker.
   Claim leases serialize each connection; retries are tenant/connection scoped. Imports go through existing
   directory and Order use cases. No missing record is automatically deleted/archived. Operationally locked
   Orders cannot be silently updated. Failed/missing data is held for review, never invented.
6. Enable outgoing statuses only with verified `status_path`, `status_field` and explicit Dispatra-to-vendor
   `status_values`. Unknown statuses are not transmitted. Vendor idempotency support is a prerequisite for
   replay-safe status delivery and must be verified before enabling that resource.

Credentials and OAuth code/verifier/token material are Fernet encrypted, never in command results. OAuth state
is hashed, single-use, expires in ten minutes and is bound to the signed-in dispatcher, company, provider,
profile revision and connection version. Access failures require reconnect; changed account identity is refused.
The profile's declared currency must match the company's current currency. Missing mappings or provider setup
produce Activation required. This status remains visible until real integration prerequisites are satisfied.

## Verification scope

Tests use explicitly synthetic complete provider profiles and injected network responses to exercise the engine,
not to certify all six vendor payloads. Actual endpoint/schema mapping and live auth acceptance remain dependent
on vendor access. Existing manual TMS configuration, tables, field mapping forms and worker are not modified.
