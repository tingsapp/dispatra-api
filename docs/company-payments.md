# Company recurring subscriptions (Stripe TEST)

The approved company SaaS rule is **the company's saved Decimal price per non-archived registered vehicle each month, beginning after its original recorded free trial ends**. Inactive registered vehicles count. No fleet means zero total; a zero rate is allowed. Delivery/customer invoices, receivables and payment collection remain outside V1.

This implementation permits only `sk_test_` credentials. No production/live rollout is included. Keep these in the server's private environment: `STRIPE_SECRET_KEY`, matching `STRIPE_WEBHOOK_SECRET`, and `BILLING_WEB_ORIGIN` (also allowed by `WEB_ORIGINS`). Hosted Checkout needs no browser publishable key. The provider pins API `2025-06-30.basil`; no raw card number/CVC reaches Dispatra.

Apply additive migration `0052_recurring_company_billing` after backing up the local database. Start the API and **a required billing worker** using the same restricted non-owner/NOBYPASSRLS database credentials and matching test configuration:

```sh
python -m app.company_billing.worker
```

Run it under the deployment's process supervisor if this slice is later approved for rollout. It scans due companies every five seconds, reconciles each enrolled company at least every 30 seconds, and automatically retries transient failures. Fleet create/archive and owner-term edits request an earlier sync. Do not start duplicate workers unnecessarily; database leases also arbitrate concurrent workers.

## Consent, creation and recovery

The Payment methods panel displays the saved unit rate, currency, current fleet count/total, trial/start timing and recurring status. An unchecked recurring-charge consent and the exact terms version are required before setup. A changed rate/version requires reloading and reviewing terms. Checkout remains in setup mode to verify/save the card securely. Verified completion records the consent timestamp, actor and exact commercial terms; it queues subscription enrollment. Existing saved cards receive no automatic enrollment: they require this fresh recurring consent. Replacing an enrolled company's card updates the existing subscription.

Only a consented initial NOT_SUBSCRIBED agreement at the accepted version is activated automatically. The worker uses `max(original trial end, agreed UTC start date)` without resetting the recorded trial. Past cutoffs begin billing upon enrollment; elapsed periods are not back-billed. Stripe creates the monthly licensed subscription with the saved rate and authoritative non-archived fleet quantity. An unset rate prevents setup/enrollment.

Each durable operation freezes its payload and UUID before provider I/O. Short database transactions claim a five-minute lease and publish verified results; recurring Stripe requests run outside transactions. Retries retain the same payload/key. Before creating a subscription, recovery paginates this customer's subscriptions and verifies company metadata/test mode, even beyond Stripe's idempotency cache lifetime. Multiple active matches fail closed for support review. Provider snapshots from webhooks never grant access; current API reads reconcile them.

Fleet/rate changes use `proration_behavior=none`: they change the next renewal, with no mid-month extra invoice or automatic credit. Currency changes and zero-price-to-paid changes require replacing the Stripe subscription: cancel without invoicing/proration and preserve the already paid period as the replacement's trial. At most one subscription collects at a time. Card replacement never creates a new subscription. Owner pause/cancel revokes paid access in the same transaction and queues provider cancellation; resume creates a replacement with previously paid time preserved. An external Stripe cancellation updates the agreement to CANCELLED and never silently recreates it; owner activation is required to resume. Administrative workspace suspension remains independent of billing.

Stripe handles automatic monthly invoice finalization, payment attempts and configured retry rules. The application does not create delivery invoices. Current open subscription invoices expose only an allowlisted hosted Stripe payment link for issuer authentication/payment recovery. A replacement card becomes the subscription default; Stripe retries and hosted payment recovery settle outstanding invoices. Verified paid current invoices restore access; a saved card alone cannot bypass failure. Pending, incomplete, past-due, unpaid or cancelled subscriptions do not grant paid access. Failed operations retain the durable retry and show a waiting/retry state.

## Access and history

A company with a future recorded trial retains free workspace access. After cutoff, Python and SQL eligibility require a valid card and an unexpired provider-verified subscription entitlement. A company without trial dates retains previous access until it explicitly enrolls; missing dates never imply trial expiry. Paid access expires at the verified period boundary even if the worker stops. Existing suspension, email verification, roles and tenant restrictions remain in force. Dispatchers retain login/account/Subscription recovery; operational APIs, keys, WebSockets, other roles and workers observe the same gate. Owner activation cannot waive payment.

Dispatcher Subscription retains exactly Payment methods and Payment history tabs in the account menu under Settings. Owner Subscription & billing combines the agreement and shared billing views. After verified setup, the client refreshes billing/account caches and polls status; no manual Refresh payment method button exists. History is the actual authorized Stripe customer's read-only charges with exact Decimal amounts/refunds, bounded pages and safe receipt links. It never presents expected revenue as collected payments.

## Webhooks and local listener

Configure snapshot events at `/api/v1/billing/stripe/webhook`:

- `checkout.session.completed`
- `customer.updated`, `customer.deleted`
- `payment_method.detached`, `payment_method.updated`, `payment_method.automatically_updated`
- `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`
- `invoice.created`, `invoice.upcoming`, `invoice.paid`, `invoice.payment_failed`, `invoice.payment_action_required`, `invoice.voided`, `invoice.marked_uncollectible`

For local development, preserve the existing listener and extend its event list. The matching CLI signing secret belongs only in private `.env`; listener logs contain it and must remain private. Pass the secret key through `STRIPE_API_KEY`, never a command argument. Restart only the affected local processes after configuration changes.

## Verification

Focused automated tests use a disposable migrated PostgreSQL database and a non-owner/NOBYPASSRLS runtime; ordinary tests explicitly replace Stripe transport. The opt-in `test_company_recurring_stripe.py` requires `STRIPE_TEST_ACCEPTANCE=1` and real test-only credentials. It uses disposable database companies and Stripe test clocks/customers, confirms actual SetupIntent/subscription/invoice behavior, advances monthly periods, checks rate × quantity, replacement, decline/recovery and zero-fleet renewal, and deletes its Stripe test clock in `finally`. Some acceptance invoices are explicitly finalized/paid to avoid waiting for account-specific finalization/retry timing; this does not certify production automatic collection or issuer authentication.

Primary references: [monthly quantities](https://docs.stripe.com/billing/subscriptions/quantities), [trials](https://docs.stripe.com/billing/subscriptions/trials), [subscription creation](https://docs.stripe.com/api/subscriptions/create?api-version=2025-06-30.basil), [price changes including zero prices](https://docs.stripe.com/billing/subscriptions/change-price), [webhook signatures](https://docs.stripe.com/webhooks/signature), [test clocks](https://docs.stripe.com/billing/testing/test-clocks).
