# Dispatcher TMS connector preparation

The Canada/US launch catalogue is Rose Rocket, CartonCloud, Truckbase, Tailwind TMS and AscendTMS, in that order. The last three remain visible with configuration guidance and a disabled Connect action until implemented and reviewed. Their internal `request_setup` classification is an activation guard, not a request workflow. Installing an arbitrary profile cannot activate them. Existing saved TruckMate, McLeod, Aljex and Turvo connections remain usable and appear after the launch catalogue until disconnected; their records and import links are preserved.

The first two share the existing encrypted credentials, OAuth state/PKCE, identity verification, import review, versioned commands, leases and tenant isolation. `app/connectors/prepared.py` now supplies documented authentication defaults. These defaults are preparation, not vendor acceptance. No live credentials or reviewed vendor import profiles are installed locally.

## Rose Rocket

Preparation supplies the Network API origin, `/api/v1/me` identity endpoint, `https://a.roserocket.com/authorize`, `https://a.roserocket.com/oauth/token`, `offline_access email profile`, and the `https://roserocket.com` audience. Register a Dispatra vendor application and its HTTPS callback, then set server-only `ROSE_ROCKET_CLIENT_ID` and `ROSE_ROCKET_CLIENT_SECRET`. Confirm the installed Rose Rocket product/API version; Classic and Network contracts must not be mixed. The operator must verify the organization identity path in `/me`; a personal user's ID is not an acceptable company identity. Confirm refresh-token organization scope and PKCE support in the sandbox.

Source: https://roserocket.readme.io/docs/rose-rocket-api-oauth-20-authentication-guide
Order relationships and field reference: https://roserocket.readme.io/docs/order

## CartonCloud

Dispatra is a software vendor, so use the vendor authorization-code flow. Customer API clients also offer client-credentials authentication, but this implementation does not substitute that for the vendor flow. Obtain the registered ISV authorization URL and scopes directly from CartonCloud; the public guide does not specify those values. Preparation supplies the API origin, `/uaa/oauth/token`, form encoding, HTTP Basic token authentication and `Accept-Version: 8`. Confirm the ISV token authentication method with CartonCloud; operator overrides are supported. Set `CARTONCLOUD_CLIENT_ID` and `CARTONCLOUD_CLIENT_SECRET` server-side.

`GET /users/me` returns a user and its accessible tenants. The connector binds to a company tenant, never the personal user ID. With one accessible tenant the worker uses its ID/name. With several tenants, set reviewed server-side `tenant_id`; no first-entry guess is permitted. Subsequent syncs require continued access to the saved tenant. Resource paths may use `{account_id}` to include that verified tenant, for example `/tenants/{account_id}/consignments/search`. The complete response, pagination, permissions, price/currency, service, cargo and address mapping remain account-specific and must be verified.

Sources:
- https://api-docs.cartoncloud.com/latest/guides/authentication
- https://api-docs.cartoncloud.com/latest/guides/account-info
- https://api-docs.cartoncloud.com/latest/api-reference/consignments
- https://help.cartoncloud.com/knowledge/API-Roles

## Activation

Keep the private reviewed profile JSON outside the repository and reference it with `TMS_CONNECTOR_PROFILES_FILE`. Each provider entry includes `provider`, `revision`, `api_version`, `evidence`, identity mapping/scope, import `resources` and complete `maps`, units and currency. Known defaults are merged before strict Profile validation; operator-supplied values override defaults. An incomplete profile or missing registered application credentials keeps the connector unavailable. Set `CONNECTOR_PUBLIC_ORIGIN` to the served HTTPS origin and register the exact `/api/v1/companies/{slug}/integrations/connectors/{provider}/callback` URL. Run the separately supervised `python -m app.connectors.worker` with the restricted runtime role.

Before activation, exercise the vendor sandbox with real payloads: company identity and renewal, full pagination, drivers/vehicles where supported, multi-stop orders, supplied price and tax, cargo units, duplicate/retry handling, invalid-record review, tenant isolation, and vendor-approved status writes. Status delivery stays off unless its endpoint, field and value translations have been explicitly reviewed. Synthetic tests demonstrate Dispatra behavior, not vendor acceptance. Missing required information must remain a review item rather than being invented.

## Connector visibility and admin counts (2026-10-17)

Dispatcher Connectors shows all listed launch providers and any existing non-disconnected account needed for safe Disconnect/Review. Ready OAuth Connect/Reconnect immediately opens the server-issued sign-in URL; credential connectors use their secure entry form. Unready connectors show a single Configuration required status and a disabled Connect button. Provider descriptions are limited to two lines without a duplicate configuration message. No setup request, request form or automatic email is provided; support remains available through the existing Help/contact flow.

Admin `/admin/integrations` is one read-only page of connector cards with TMS name/monogram and Connected dispatchers count. The only owner connector HTTP endpoint is GET `/api/v1/platform/integrations/providers`, returning id, name, initials and connected_dispatchers. There are no tabs, controls, company connection lists or requests. Count active dispatcher accounts in active workspaces with a credentialed, unpaused CONNECTED connection, or SYNCING after a prior successful sync, and a ready enabled provider. Do not count initial verification, failed/disconnected accounts or inactive users/workspaces. No identities or secrets are returned.

Vendor registration, credentials, public callback origin and reviewed adapters stay server-side. Existing provider flags, pause guards and historical request records from frozen migration 0055 are retained privately; the request/control HTTP APIs and clients have been removed. This presentation change does not activate any provider or mutate imported records.

## Rose Rocket retrieval progress (2026-10-17)

The Network order-board adapter in `app/connectors/roserocket.py` uses the documented POST `/api/v2/platformModel/objects/search` with offset/limit and GET `/api/v2/platformModel/objects/{id}` with explicit boardId/relationship paths. Configure a reviewed board ID, timezone and detail paths through `orders_resource`; map the expanded payload using the existing complete server profile. It checks each summary/detail organization against the saved company, rejects changed totals, repeated IDs, malformed/truncated pages and unexpected record identities, and retrieves the complete source before imports. It does not guess board IDs, installed edition or relationship names.

OAuth keeps valid access tokens until near expiry, refreshes with the saved organization scope, saves exchanged/rotated tokens before identity/data reads, and renews once after an unauthorized read. Outbound account path substitution is supported, but status writes remain gated by reviewed translations; POD delivery is not claimed. Registered vendor access, actual board payloads/mappings and live sandbox verification are still required. These changes are tested with synthetic payloads, not certified by Rose Rocket.

Board retrieval sources: https://roserocket.readme.io/docs/using-boards-in-rose-rocket and https://roserocket.readme.io/reference/get_objects-recordid.

The catalogue targets Canada and the US, but the current operational address contract accepts Canada only. US address/validation support must be completed separately before importing US jobs.
