# Dispatra API Specification

> Normative FastAPI/Python implementation contract. `MUST`, `MUST NOT`, `SHOULD`, and `MAY` are intentional.
> Last updated: 2026-09-14

## 1. Authority and product scope

The API is Dispatra V1's source of truth for authentication, tenant isolation, domain state, pricing, dispatch, routing, driver execution, proof of delivery, billing, audit, events, and workflow orchestration. PostgreSQL is the system of record. The fixed stack is FastAPI/Python, Pydantic, SQLAlchemy/Alembic, PostgreSQL, React web, React Native mobile, shadcn/ui, and agentic workflows behind application services.

Dispatra is a simple local-delivery dispatch system for organizations operating in British Columbia, Canada. V1 is English-only and is not a full TMS. It defers payments, accounts receivable, payroll/settlement, ELD/HOS, maintenance, claims, brokerage, marketplace/load boards, warehouse/inventory management, complex CRM, advanced BI, additional locale support, and multi-branch operational UI.

Canonical workflow:

```text
Order created -> validate and calculate customer price -> READY_FOR_DISPATCH
-> AUTO/MANUAL dispatch -> driver and vehicle assignment -> automatic route optimization
-> pickup and delivery -> required POD -> finalize actual charges
-> immutable final PricingSnapshot -> Invoice -> send to customer
```

Actors are Organization, Dispatcher/organization user, Customer, Driver, and System/agentic workflow. Every tenant-owned record and query is organization-scoped server-side. Customers can see only their own orders, stops, contacts, tracking projections, and invoices. Drivers can see only their assigned routes/orders.

## 2. Canonical vocabulary and lifecycle

- **Order** is the commercial/customer request and pricing/invoicing unit. It may originate from `CUSTOMER`, `DISPATCHER`, `API`, or `IMPORT`; customer and dispatcher creation are required V1 workflows.
- **DeliveryJob** is optional as an operational child when an Order has multiple final destinations. It MUST NOT introduce a competing commercial lifecycle.
- **OrderStop** is a requested pickup/drop-off requirement and is not necessarily a RouteStop.
- **Route** is an executable optimized sequence assigned to exactly one driver and one vehicle. It may contain work from multiple Orders.
- **RouteStop** is an actual visit with type `PICKUP` or `DROPOFF`. Repeated visits to one address remain distinct stable-ID stops.

Canonical Order lifecycle:

```text
DRAFT -> SUBMITTED -> PRICED -> READY_FOR_DISPATCH -> ASSIGNED
-> IN_EXECUTION -> COMPLETED -> BILLING_FINALIZATION -> INVOICED
```

`CANCELLED`, `FAILED`, and `NEEDS_ATTENTION` are exception/terminal or side states. Route, stop, assignment, pricing, and invoice statuses remain separate. A failed pickup blocks linked deliveries; unrelated feasible deliveries may continue. A skipped stop never means completed.

V1 MUST support one-to-one, one-to-many, many-to-one, and many-to-many pickup/drop-off relationships. Valid routes may interleave `P1 -> D1 -> P2 -> D2` when every linked pickup precedes its drop-off and constraints hold. Every RouteStop MUST have stable ID, type, address snapshot, coordinates, customer/location contact, instructions, time window, planned service duration, ETA, actual arrival/departure, status, linked Order/DeliveryJob, and planned quantity/weight/volume changes. Pickup completion is explicit; visiting a location does not imply loading. Recompute load after every pickup/drop-off and enforce `0 <= load <= capacity` at every leg.

## 3. Architecture and authority boundaries

Use an API-first modular monolith. Keep routers thin and separate responsibilities such as:

```text
app/{routers,schemas,services,domain,repositories,models,
     pricing,dispatch,routing,billing,workflows,events,audit}
```

Routers authenticate, authorize, parse typed Pydantic input, call a use case, translate known errors, and return typed output. They MUST NOT contain pricing formulas, state transitions, dispatch scoring, route optimization, cross-domain SQL, or provider calls. Do not build a giant OrderService.

The Pricing Engine owns rate resolution, formulas, ChargeLines, snapshots, locking, and repricing. Dispatch owns mode, eligibility, scoring, assignment, and decisions. Route Optimization owns precedence, capacity, time windows, ETAs, and sequence validation. Billing consumes finalized PricingSnapshot/ChargeLine records and MUST NOT recalculate an Order. POD owns evidence validation. Agentic workflows call approved application services and never become system state.

Use PostgreSQL constraints/indexes for real invariants. Money uses Python `Decimal`, explicit organization rounding (normally HALF_UP), and numeric database types. State-changing money/state operations are transactional.

## 4. Minimum entities and invariants

Evaluate/model at minimum: `Organization`, `OrganizationUser/Dispatcher`, `Customer`, `CustomerGroup`, `Driver`, `Vehicle`, `VehicleType`, `ServiceType`, `Order`, `OrderStop`, `OrderItem/Package`, optional `DeliveryJob`, `Route`, `RouteStop`, `Assignment`, `DriverLocation`, `RateCard`, `Zone`, `ZoneRate`, `Accessorial`, `OrderAccessorial`, `TaxProfile`, `CustomerPricing`, `PricingSnapshot`, `ChargeLine`, `ProofOfDelivery`, `Exception/NeedsAttention`, `Invoice`, `InvoiceLine`, `InvoiceDeliveryAttempt`, `AuditEvent`, and `DomainEvent/OutboxEvent`.

Use UUIDs or one consistently documented identifier policy, UTC timestamps, tenant foreign keys, created/updated timestamps, and optimistic versions where mutable. Execution/pricing addresses and contacts are snapshots.

An Assignment binds one Route to one Driver and one Vehicle and records `AUTO`/`MANUAL`, generation, expected versions, timestamps, and audit metadata. A Route cannot have two active assignments. Locked, in-progress, completed, and cancelled Routes MUST NOT be silently rewritten by automation. A new Order may join only an unlocked planned Route if feasible, create another planned Route, become the driver's next Route, or remain unassigned/Needs Attention. Active-route insertion/reoptimization is deferred beyond V1.


### 4.1 Order, customer and commercial fields

These are target schema requirements, not existing database tables. [Client specification §11](../client/spec.md#11-v1-entity-property-audit--september-14) and [client implementation state](../client/state.md) describe the current local forms. The API owns the versioned wire contract; §11.1 records deliberate compatibility mappings.

| Record | Required field groups and behavior |
| --- | --- |
| Common | Stable `id`, server-controlled `organization_id`, nullable `branch_id`, `created_at`, `updated_at`, actor/audit records, mutable `version`, optional `external_reference` and bounded integration `metadata`. Tenant membership and branch scope are server-validated; branch administration stays hidden in V1. |
| Order identity | Tenant-unique `order_number`; `source`; optional `external_reference`; `customer_id`; optional `billing_customer_id`; `order_type` (`DELIVERY`, `PICKUP`, `RETURN`, `TRANSFER`, `SERVICE_CALL`); `priority` (`NORMAL`, `HIGH`, `URGENT`); `service_type_id`/service-level relationship; scheduled window start/end; canonical lifecycle from §2. A walk-in may omit a directory customer only with an explicit booking contact snapshot and sufficient billing information before invoicing. |
| Order operations | `commodity_description`, `reference_numbers` (PO/invoice/retailer references), `instructions`, private `dispatcher_notes`, `required_vehicle_type_id`, required skills/equipment, service area, tags, order communication/tracking preferences, cancellation time/reason/actor. `assigned_driver_id`, actual `assigned_vehicle_id`, and route references are projections of Assignment, never competing writable assignment fields. |
| Order cargo | Child OrderItems define pieces, per-piece weight/dimensions, volume, declared value and handling. Expose derived piece count, total physical weight (kg), total volume (m³) and declared value; do not trust independently editable totals. Physical payload differs from pricing dimensional/chargeable weight. |
| Order commercial | Explicit currency, selected/resolved rate-card ID/version, pricing method/status, current PricingSnapshot reference, ChargeLines, subtotal, accessorial total, discount/adjustments, tax and final total. Preserve imported amount/source/reference/tax treatment and override reason/actor/time. Do not create independent mutable copies of amounts already owned by the snapshot. Pricing status and estimate/final stage are separate from Order lifecycle, billing state and risk. |
| Customer identity | Tenant-unique `customer_number`, type (`BUSINESS`, `INDIVIDUAL`), legal/display names, contact name/phone/email, billing email, status (`ACTIVE`, `INACTIVE`, `ON_HOLD`), tags and internal operational notes. Legacy “Preferred” is an Active customer classification tag, not a lifecycle state. The purchaser is distinct from each stop’s recipient. |
| Customer defaults | Stable-ID saved pickup/delivery/billing/depot addresses with label, address, contact, phone and instructions; default service, local time window and delivery instructions; payment terms (inherit, COD, NET7/15/30/60), currency, tax profile/exemption, selected rate card, customer group, discount, communication/tracking preferences; optional permitted branches and integration metadata. English-only; currency must match organization/rate-card pricing in V1. |

At booking, copy customer, separate payer when present, and per-stop contact/address details into snapshots. Customer directory edits MUST NOT rewrite historical Order, route-execution, pricing or Invoice data. Defaults initialize a new booking; they do not continuously overwrite Order-specific edits. Authorized pre-execution account changes explicitly replace the relevant booking snapshot and trigger required repricing/version checks. Repricing with the same account does not silently refresh its contact/billing snapshot. Preserve the original and any replacement versions for audit.

Reject duplicate numbers within a tenant, invalid emails/numeric inputs, cross-tenant customer/payer IDs, and inactive/on-hold accounts for new work. Existing work/history must remain readable under authorization. Customer order counts/history come from linked Orders; do not persist decorative counters as authoritative totals. Directory records referenced by historical work must retain a resolvable identity when deactivated.

### 4.2 Stops, items and execution policy

| Record | Required field groups and behavior |
| --- | --- |
| OrderStop | Stable `id`, `order_id`, requested `sequence`, type `PICKUP`/`DROPOFF`, original/normalized address, coordinates when resolved, recipient/contact name/phone/email, window start/end, service duration minutes, instructions/access requirements, reference, residential flag, recorded/estimated wait, requested POD requirement and supplying pickup IDs. Coordinate/normalization and execution timestamps may be absent before resolution; fabricated coordinates are prohibited. |
| OrderItem | Stable `id`, `order_id`, description, positive whole quantity, per-piece weight kg and length/width/height cm, derived volume, declared value, handling unit (`ITEM`, `BOX`, `PALLET`, `TOTE`, `PACKAGE`), optional barcode/reference, fragile/stackable/two-person flags, handling tags, `pickup_stop_id`, `delivery_stop_id`. Split a line into separately linked quantities when goods have different pickup/delivery movements. Scanning remains deferred; storing a reference is supported. |
| RouteStop | Stable visit ID and route/revision, explicit OrderStop/OrderItem references, optimized sequence, planned/actual arrival and departure, stop status, ETA, planned and actual item/quantity load/unload changes, evidence and attempt references. Keep requested OrderStop order distinct from server-validated executable route order. One shared street address does not merge recipients or POD. |

Every item link stays within its Order and refers to a pickup before its linked delivery; each delivery’s supplying-pickup set must agree with item movements. Reorder/remove/update must reject orphaned links and invalid precedence atomically. Validate weight, volume and pallets after every stop across every Order already on the proposed Route; enforce physical item fit and handling compatibility. Upright item fit may allow base rotation, not arbitrary turning of fragile freight onto its side. An empty/unknown dimension or qualification in a legacy record does not establish compatibility. Pickup confirmations record actual loaded quantities; short loads and failed pickups constrain dependent deliveries.

The client currently stores a requested POD value of `NONE`, `PHOTO`, `SIGNATURE` or `PHOTO_AND_SIGNATURE`. This field MUST NOT bypass §9's receiver-present/unattended-delivery rules. Publish the resolved allowed actions, unattended-delivery permission and required evidence in the driver projection; a client's `NONE` or default is not evidence of an approved waiver. Photo-plus-signature requests may add evidence requirements. The API validates the resulting requirement and completion, while the driver app follows the returned policy and preserves pending evidence.

### 4.3 Driver and vehicle profiles

| Record | Required field groups and behavior |
| --- | --- |
| Driver | Stable auth/user link, driver number, name/contact/photo; account `ACTIVE`/`INACTIVE`; duty `ON_DUTY`/`OFF_DUTY`; work `AVAILABLE`/`BUSY`/`ON_BREAK`; employment type `EMPLOYEE`/`CONTRACTOR`/`TEMPORARY`; operational licence class, skills/tags, service areas, qualified vehicle-type IDs, home depot, shift start/end, availability periods, maximum work minutes, preferred start location, current vehicle/route projections, notes/reference/audit. No payroll, HR or compliance module; a future simple qualification-expiry record remains possible. |
| Driver telemetry | Separate `app_last_seen_at`, `location_captured_at` and server receipt timestamps, duty session, location-permission status (`GRANTED`, `DENIED`, `UNKNOWN`), coordinates/accuracy. Connectivity derives from timestamps and policy; users cannot select it. Absence, invalid/future timestamps or missing permissions remain explicit unknown/unavailable states. |
| Vehicle | Vehicle number, normalized `vehicle_type_id`, name/make/model/year, licence plate and province, record `ACTIVE`/`INACTIVE`, availability `AVAILABLE`/`IN_USE`/`UNAVAILABLE`, payload kg, cargo volume m³, cargo length/width/height cm, pallet capacity, equipment/tags, service areas/home depot, optional independent location, unavailable-from/until/reason, current driver/route projections, reference/notes/audit. No maintenance/fuel/insurance/inspection-management workflows in V1. |

Enforce tenant-unique driver/vehicle numbers and normalized plate-plus-jurisdiction uniqueness. Normalize skills/areas/equipment against tenant-scoped identifiers when building schemas; free-text prototype labels are not authorization or compatibility evidence. Availability periods and shifts must have valid ordered bounds, including overnight shifts. Store instants in UTC while retaining the organization's named timezone for scheduling; explicitly resolve invalid/ambiguous daylight-saving wall times instead of interpreting them in a device timezone. Daily default windows remain local times until a service date is selected. Do not treat `maximum_work_minutes` as an ELD/HOS compliance system.

A driver’s selected vehicle, the vehicle’s current driver and active Assignment must agree transactionally, with optimistic versions and uniqueness constraints. A profile edit cannot release or switch an in-use asset or mutate loaded-goods custody; use an authorized route/assignment transition. Ending duty does not automatically finish an active route. Web prototype edits to duty/work state are local simulations: they never authorize mobile GPS collection or restart an Off Duty device. Only the driver-started duty-session flow in [driver specification §4](../driver/spec.md#4-duty-and-location) can start tracking.

The web prototype labels app freshness online through two minutes, stale through fifteen, and offline afterward. These are provisional display thresholds, not approved production tracking cadence or server policy. Agree and publish server freshness thresholds separately; app connectivity, GPS freshness, duty and workload remain independent.

## 5. Tenant security and API conventions

Require authenticated authorization, least privilege, TLS, rate limiting where appropriate, safe uploads, secure secrets, PII-minimized logs, and no client provider secrets. Use `/api/v1`, typed Pydantic schemas, ISO-8601 timezone-aware timestamps, cursor pagination for large lists, and an error envelope containing code, message, details, field errors, retryable flag, and request ID. Cross-tenant existence MUST NOT leak.

Retryable mutations MUST carry idempotency key/operation ID and expected entity/assignment versions. Repeating the same key and payload returns the original result; reusing it with another payload fails. Persist operation result, payload hash, audit, and outbox in the same transaction. Async work returns operation ID/status URL; accepted/queued is not completed.

## 6. Pricing authority and configuration

The API is the only authoritative customer-pricing engine. The web client may configure/display and use a faithful static copy for prototype behavior. The driver app MUST NOT calculate prices.

Rate Card precedence is:

```text
1 explicit Order override/import
2 active customer-specific card
3 active Customer Group card
4 organization service-specific card
5 organization default card
```

An eligible Rate Card explicitly selected on the customer or group profile wins within that level; a missing/ineligible selection falls back to eligible matches. Customer-level matches precede group selections, and explicit Order overrides remain first. For remaining matches within a level choose the most specific valid combination: service+vehicle+zones, service+zones, service+vehicle, service, general. An equally specific tie creates `PRICING_CONFIGURATION_CONFLICT`; never choose arbitrarily. Support `FIXED`, `ZONE`, `BASE_DISTANCE`, `HOURLY`, and `IMPORTED`. Rate Cards require ID, name, method, scope/scope ID, service, optional vehicle, effective dates, priority, active status, version, and method values.

Distinguish order facts, configurable constants/rates, and derived values. Organization settings include currency, units, money precision, rounding, dimensional divisor, fuel default, tax profile, wait allowance/increment, included stops, extra-stop fee, and price-lock event. Rate Cards support minimum, fixed/base, included distance/weight/pieces/stops, distance/weight/piece/extra-stop rates (time only on explicit hourly contracts), service multiplier, fuel, wait, hourly rounding/minimum/basis, dimensional settings, and zone no-match behavior. Vehicle settings include type, surcharge, fuel eligibility, weight/volume capacity, and equipment. Customer settings use `null` for inherit and numeric `0` for explicit zero.

### 6.1 Pricing formulas

September 12 adopted contract: see [pricing rules](../client/pricing-rules.md). Add explicit hourly clock start/stop, handling/wait inclusion and actual-settlement policy; Admin / Dispatch Fee applicability; minimum-order override/waiver; discount inheritance stop/continue; contract zone matrices with optional organization fallback; unique pickup-to-delivery movements; imported freight versus preserved final-total mode with supplied/included/exempt tax. Waiting trigger is explicit and allowance precedence is card → accessorial → organization. Apply freight minimum after multiplier; apply net order minimum after discounts and adjustments. Normalize inclusive tax consistently before modifiers; profit uses net revenue and flags incomplete cost inputs. Preserve quoted settings and final amounts across edits and reassignment. This pricing requirement remains unimplemented; the account-access milestone does not implement the pricing engine.

Calculate: resolve card; transportation; load/vehicle/stop/accessorial charges; fuel; discounts; authorized adjustment; tax; total. Use Decimal arithmetic and explicit rounding.

```text
volume = sum(quantity * length * width * height)
dimensional_weight = volume / dimensional_divisor
chargeable_weight = max(actual_weight, dimensional_weight) when enabled
weight_charge = max(0, chargeable_weight - included_weight) * weight_rate
piece_charge = max(0, pieces - included_pieces) * piece_rate
load_charge = max(weight_charge, piece_charge) when configured, otherwise configured basis

distance_charge = max(0, priced_order_distance - included_distance) * distance_rate
extra_stop = max(0, stop_count - included_stops) * extra_stop_rate
charged_wait = ceil(max(0, actual_wait - free_wait) / wait_increment) * wait_increment
wait_charge = charged_wait * wait_rate_per_minute
```

Ordinary delivery pricing never charges routine duration. Retire obsolete routine minute rates/included minutes on Base Distance and Zone cards idempotently, with one version increment on each affected card. Do not block new pricing on retired fields. Retry only unfinished estimates previously blocked by the retired migration rule; preserve successful quotes, final snapshots, completed Orders and invoice-bearing records. Hourly contracts and recorded-wait charges retain their behavior. Never hard-code a dimensional divisor. `FIXED` uses fixed amount; `ZONE` resolves origin/destination/service/vehicle with configurable `ERROR` or base-distance fallback; `BASE_DISTANCE` uses base fee plus distance/load/piece/stop charges, then the service multiplier and freight minimum; `HOURLY` rounds to configured increment and applies minimum billable minutes with estimated or actual basis; `IMPORTED` distinguishes freight with explicitly permitted contract modifiers from a final agreed total with supplied/included/exempt tax. Final agreed totals bypass all fees, discounts, minimums and rounding.

Vehicle surcharge is additive. Fuel is a separate ChargeLine calculated only from eligible positive lines. Discounts are `INHERIT`, `NONE`, `PERCENT`, or `FIXED`, with scopes `TRANSPORT_ONLY` or `PRE_TAX_SUBTOTAL`, and create negative ChargeLines. Manual adjustments retain calculated total, override total, reason, user, and timestamp. Tax uses configurable TaxProfiles and taxable ChargeLines; exemptions are supported.

Use generic Accessorial definitions, not one Order column per charge. Each supports code, name, active, calculation type (`FLAT`, `PER_UNIT`, `PER_MINUTE`, `PER_HOUR`, `PERCENT`, `PASS_THROUGH`), rate, unit, allowance, increment, min/max, fuel eligibility, taxable flag, and optional auto rule. OrderAccessorial/ChargeLine stores quantity and result. Waiting, stairs, elevator, helper, parking/toll, inside/residential, liftgate, after-hours, weekend, special handling, extra stop, return/redelivery/redirect are configurable examples.

### 6.2 Pricing snapshots and locks

When price locks, persist an immutable versioned PricingSnapshot containing rate-card ID/version/effective date, resolved constants, order pricing facts, priced distance, estimated duration, multiplier, dimensional divisor, fuel, discounts, accessorial rules/rates, tax profile, ChargeLines, subtotal, tax, total, and timestamp. Historical Orders/Invoices MUST remain reproducible after settings change. Repricing creates a new version and never mutates the old one.

Use separate rate lock and amount lock. Fixed/Zone/ordinary Base Distance rates and customer amount lock at `READY_FOR_DISPATCH`; usage-based rates lock before dispatch while actual quantity remains open. Completion calculates allowed completion-dependent lines using locked rates, then freezes the final snapshot. Reassignment and operational route distance never reprice.

Store `priced_order_distance`, `operational_route_distance`, and `deadhead_distance` separately. Customer distance excludes driver position, other customers, deadhead, and merged operational route distance. Internal fulfillment cost may use those operational values for ranking but is never customer-visible.

## 7. Dispatch mode, eligibility, and ranking

There are exactly two organization dispatch modes: `AUTO` and `MANUAL`. The toggle affects eligible unassigned work only and never silently unassigns or mutates executing work.

In AUTO, a priced Ready Order triggers eligibility evaluation, best feasible candidate selection, assignment, insertion into an eligible unlocked planned Route or Route creation, and automatic optimization. No feasible candidate creates Needs Attention with a clear reason. In MANUAL, the Order remains unassigned until a dispatcher selects Driver/Vehicle; the API validates the same hard constraints, assigns, and automatically optimizes. AI may rank/recommend in Manual but MUST NOT assign. A dispatcher may override soft recommendations with an audited reason, never hard constraints.

`max_active_orders_per_driver` belongs under Organization Settings -> Billing, Tax & Cost -> General (Dispatch card); the Manual/Auto switch is in the web sidebar. A driver at or above the maximum is ineligible for an additional active Order in both AUTO and MANUAL. It never affects customer price. Also evaluate tenant ownership, active driver/vehicle records, On Duty and work availability, driver skills and vehicle-type qualifications, driver/vehicle service areas, shift and availability periods, maximum work minutes, actual fleet asset type/equipment/capacity, route duration/stop count, windows, SLA, precedence, route state, valid addresses/coordinates, and resolved/unexpired pricing. A free-text “Two people” skill from the prototype does not establish the presence of a second person; require an authorized operational crew confirmation when that handling requirement applies, without adding payroll/HR scope. Missing data needed to establish feasibility creates a specific rejection/Needs Attention reason; it is never unlimited capacity or implicit qualification.

After hard filtering, rank with explainable components such as incremental distance/duration, deadhead, predicted ETAs, lateness/SLA risk, disruption, workload, vehicle fit/capacity, route efficiency, and internal cost. Persist eligibility/rejection reasons, score components, selected candidate, mode, override, timestamp, and algorithm/version. Deterministic constraints and optimization remain authoritative.

## 8. Route optimization and execution

Optimization is required after both AUTO and MANUAL assignment. It considers pickup/drop-off precedence, many-to-many relationships, capacity after every stop, time windows, service duration, SLA, start location, distance/duration, priority, and locked stops. Output ordered stable RouteStops, distance, duration, ETA, load after each stop, feasibility, and optimizer/version reason. Manual reorder is allowed only through a backend command that reruns validation.

Driver flow is Login -> On Duty -> assigned Route -> start -> arrive -> pickup/drop-off -> complete -> finish -> Off Duty. Pickup must be explicitly confirmed. A failed pickup blocks linked deliveries; unrelated work may continue. Route completion validates outcomes, custody, and POD rather than claiming every delivery succeeded.

## 9. Completion, POD, and Invoice

Resolve each stop’s requested POD requirement into an authoritative execution policy before dispatch (see §4.2). Receiver present requires recipient name and signature. Receiver unavailable requires a photo only when shipment may safely be left. Each delivery keeps its own evidence even at the same address. POD stores Order/DeliveryJob, RouteStop, recipient, signature/photo, timestamp, permitted coordinates, note, and sync status. Offline evidence is preserved until server confirmation and all upload/completion mutations are idempotent.

On completion: confirm pickups/drop-offs; validate POD; capture actual usage; calculate allowed final lines; freeze final PricingSnapshot; generate one Invoice; send it to the frozen billing customer email (falling back to the ordering customer only when no separate payer was selected). Payment terms and billing identity come from that same booking/billing snapshot. Missing billing email, missing POD, unresolved actual usage, tax conflict, pricing conflict, or manual billing hold creates Needs Attention.

Invoice includes organization/customer/order IDs, invoice number, currency/status, issued/sent timestamps, billing snapshot, subtotal, discount/adjustment/tax/total, PricingSnapshot ID, and audit timestamps. InvoiceLines derive from finalized ChargeLines. Statuses are `DRAFT`, `FINALIZED`, `SENT`, `DELIVERY_FAILED`, `VOID`. V1 excludes payment collection and accounts receivable. A unique active invoice relationship or idempotency key guarantees one active V1 invoice per completed Order. Email retries resend the existing finalized invoice and record InvoiceDeliveryAttempt audit data.

## 10. Agentic workflows, events, and audit

Agentic workflows may react to domain events, request dispatch evaluation, orchestrate assignment/optimization through domain services, send notifications/invoices, summarize operations, and flag exceptions. They MUST NOT bypass tenant authorization, state machines, pricing, capacity, route validation, idempotency, permissions, or audit. Agent memory is not operational state.

Events include order created/priced/ready/assigned/completed/cancelled; pricing estimated/locked/repriced/finalized/error; dispatch mode/evaluation/assignment; route created/optimized/started/completed/exception; stop arrived/completed/failed; driver duty/location; POD; invoice generated/sent/delivery failure; exception created/resolved. Events carry organization ID, entity ID, timestamp, correlation ID, schema/version, and minimized sensitive payload.

Audit material decisions: source, pricing/card/snapshot, overrides, mode changes, candidate evaluation, assignment, optimization/reorder, edits, stop/POD/completion, invoice generation/sending, and exception resolution. The trail answers who/what, when, inputs/rules, change, and reason.

## 11. REST/OpenAPI contract

Publish typed OpenAPI for both clients. Cover authentication/current user; organizations/settings and dispatch mode/configuration; customers/groups/pricing; drivers/locations; vehicles/types; services; Orders/stops/items; pricing calculation; Rate Cards/zones/rates/accessorials/taxes; dispatch evaluation/assignment; Routes/optimization/stops; POD/issues/Needs Attention; invoices/history/audit. Use existing conventions when choosing paths, but do not expose competing job-driver writes. Retryable commands require idempotency and state preconditions.


### 11.1 Frontend compatibility and driver projections

Use `/api/v1/orders` as the canonical commercial resource. Do not expose the frontend `Job` alias as a second API lifecycle. Define read/write Pydantic schemas before generating both clients; preserve these semantic mappings explicitly in tested adapters until the prototype migrates:

| Existing frontend representation | API contract / integration rule |
| --- | --- |
| `Order` with `Job` alias, `jobNumber` | Order and `order_number`; alias exists only for older Monitor components. |
| `lifecycleStatus`; legacy `status` risk strings | Order lifecycle from §2; at-risk/late/on-time/unassigned displays remain separate computed indicators. Never infer completion from a risk badge. |
| `customerId`, `billingCustomerId`, `customerSnapshot`, `billingCustomerSnapshot` | Customer/payer relationships and immutable booking snapshots; contact at each stop is a separate person. |
| `pricingInput.vehicleId` and legacy Order `vehicleId` | Required/priced **VehicleType**, not an actual fleet asset. Driver `currentVehicleId` refers to a Vehicle. Assignment identifies the actual driver/vehicle/route. |
| `pricingInput.stops`, `pickupIds`, package movement IDs | OrderStops, explicit pickup dependencies and OrderItems. Map separately to RouteStop visit IDs in the execution projection. |
| Source `CUSTOMER_PORTAL` | API source `CUSTOMER`; `DISPATCHER` and `IMPORT` map directly. API-originated work uses `API`; add typed handling instead of silently coercing it. |
| Method `BASE_PLUS_DISTANCE` | API method `BASE_DISTANCE`; remaining method values map directly. |
| Discount scope `SUBTOTAL` | API scope `PRE_TAX_SUBTOTAL`; `TRANSPORT_ONLY` maps directly. |
| Pricing `PRICED`/`NEEDS_ATTENTION`/`UNAVAILABLE` plus `ESTIMATE`/`FINAL` | Separate pricing result status and stage; neither replaces lifecycle or Invoice state. |
| Local `InvoicePreview` with status `PREVIEW` | Prototype only. Never import as an issued/sent Invoice or claim an email was delivered. |
| Camel-case fields, formatted address/vehicle strings, browser wall time | Typed snake-case fields/IDs, explicit units, normalized instants plus organization timezone; do not parse display strings as canonical identifiers. |

Driver responses are purpose-specific projections of their own profile, duty session, assigned Vehicle/Route, ordered visits, operational Order references, recipient snapshots, item movements, requirements and valid execution actions. Do not return full Customer records, payer/billing emails, tax/payment terms, PricingSnapshots, ChargeLines, declared monetary values when not operationally required, private dispatcher notes, integration metadata or unrelated customer history. Filtering only in the mobile UI is insufficient; those fields must be absent from driver responses, caches, push payloads and logs.

Compare frontend fixtures with generated contracts and migrate local saves deliberately. Preserve successful historical snapshots and stable IDs; leave missing legacy capacities, timestamps or ambiguous item allocations unresolved. The local single-Order capacity checks are useful acceptance fixtures, not an implementation of authoritative multi-Order route feasibility.

## 12. Security, privacy, and scope boundaries

Use secure client credential storage, TLS, minimal push PII, private evidence storage, safe uploads, coordinate/dimension/weight validation, tenant-scoped tracking, and immutable pricing/invoice history. Driver location is operational while On Duty, stops Off Duty, uses adaptive cadence, and exposes freshness/stale state. Public tracking is a token-scoped projection for one customer/order and never leaks another customer's route.

Also defer dynamic fuel integrations, arbitrary pricing formulas, complex volume plans, active-route insertion without approval, self-modifying scoring, general AI chatbot, and other full-TMS capabilities. Schema may be multi-branch-ready but branch operations are not V1.

## 13. Required tests and acceptance

API tests MUST cover Rate Card precedence/effective dates/conflicts; every pricing method and formula, dimensions/pieces, surcharge/accessorial/wait/fuel/discount/tax/rounding, override, lock/reprice, immutable snapshots; AUTO/MANUAL eligibility, max count, capacity/windows/SLA, no candidate, soft override and audit; all pickup/drop-off cardinalities including `P1-D1-P2-D2`, precedence rejection, intermediate overload, repeated addresses, multiple Orders, and locked route protection; failed pickup dependency, unrelated continuation, duplicate completion, offline POD, completion-dependent charge; invoice generation/matching/idempotent retry/resend/failure audit; and tenant isolation.

Acceptance requires no driver self-claim, automatic optimization in both modes, customer price unchanged by reassignment/merged route, explainable decisions, durable outbox/idempotency, and OpenAPI/schema tests. Record actual commands/results in `state.md`; specification text is not evidence of implementation.

Entity reconciliation acceptance adds customer/payer/recipient separation and snapshot immutability; default inheritance; Preferred-tag migration; identifier/plate uniqueness; enum/field/unit/timezone adapter roundtrips; invalid/ambiguous windows; missing measurements/qualification handling; item split/dependency/remove/reorder checks; orthogonal account/duty/work/connectivity states; concurrent vehicle binding and loaded-asset protection; driver projection field exclusion; and requested-versus-resolved POD requirements. Test that private notes and prices never reach mobile responses or offline storage. These are required future API tests, not results from the frontend suite.


## 13. Company and customer access milestone


The platform owner provisions dispatch companies. A permanent organization UUID owns company records; a unique company slug identifies /{company}/dispatch and /{company}/customer. /platform is the owner portal. One shared PostgreSQL database uses organization constraints, application authorization and row-level security. Dedicated databases are deferred.

Dispatchers create customer records and login credentials; no public registration or invitation acceptance flow exists. First login does not force a password change. Customers may change their password voluntarily, and complete contact name, email, phone and address. Business identity, status and commercial settings remain dispatcher-controlled. Passwords are hashed; generated initial/reset credentials are shown only in the creating browser and must not be stored in browser persistence or API response logs.

Milestone acceptance: owner creates company and first dispatcher; dispatcher creates customer access and copies login details; customer signs in, edits own profile, and optionally changes password. Changes persist in PostgreSQL. Cross-company/customer access, role escalation, replay conflicts and unauthorized profile fields are rejected. Password reset revokes old sessions.

This milestone adds authenticated account pages alongside the existing local dispatch prototype. Orders/pricing/driver/vehicle integrations, subscriptions, email delivery and customer bookings remain later milestones. Existing browser-only customer records are not silently imported into an arbitrary company. Future migration must preserve their full commercial/default fields and require an explicit organization mapping.
