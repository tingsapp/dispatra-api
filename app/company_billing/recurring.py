"""One durable, leased operation per company. Stripe I/O runs outside DB transactions.

Quantity/rate changes use no proration. Each operation freezes its payload and UUID
before I/O. Recovery lists this customer's subscriptions before creating one, even
when Stripe's 24-hour idempotency cache has expired. Webhooks only request a fresh
read; their snapshots never grant access or replace current state.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4
from urllib.parse import urlencode, urlsplit
from fastapi import HTTPException
from sqlalchemy import select, func, or_, text
from sqlalchemy.orm import Session
from app.database import engine, context
from app.models import Organization, CompanySubscription, AuditEvent, now
from app.operations.models import Vehicle
from .models import CompanyBilling
from . import provider
from .history import ZERO_DECIMAL

TERMINAL = {'canceled', 'incomplete_expired'}
PRODUCT = 'dispatra_saas_vehicle_test'


def terms(db, identity):
    row = db.get(CompanySubscription, identity)
    if row is None: raise HTTPException(503, 'Company subscription terms are unavailable.')
    count = db.scalar(select(func.count()).select_from(Vehicle).where(Vehicle.organization_id == identity, Vehicle.archived_at.is_(None)))
    return row, count


def starts(org, agreement):
    dates = [datetime.combine(agreement.starts_on, datetime.min.time(), timezone.utc)] if agreement.starts_on else []
    if org.trial_ends_at: dates.append(org.trial_ends_at)
    return max(dates) if dates else None


def invoice_url(value):
    try:
        parsed = urlsplit(value or '')
        return value if parsed.scheme == 'https' and parsed.hostname == 'invoice.stripe.com' and not parsed.username and parsed.port in (None,443) else None
    except ValueError: return None


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc) if value else None


def request_sync(row):
    row.next_sync_at = now()


def transaction():
    db = Session(engine, expire_on_commit=False)
    db.begin()
    try:
        unsafe = db.scalar(text("SELECT rolsuper OR rolbypassrls OR EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND pg_has_role(current_user,tableowner,'USAGE')) FROM pg_roles WHERE rolname=current_user"))
        if unsafe: raise RuntimeError('Billing requires a restricted non-owner runtime role.')
        context(db, platform=True)
        return db
    except Exception:
        db.close()
        raise


def claim(identity):
    db = transaction()
    try:
        # Organization first matches card setup and avoids cross-service lock inversions.
        org = db.scalar(select(Organization).where(Organization.id == identity).with_for_update())
        row = db.scalar(select(CompanyBilling).where(CompanyBilling.organization_id == identity).with_for_update())
        if not row or not row.consented_at or (row.lease_until and row.lease_until > now()): return None
        agreement = db.scalar(select(CompanySubscription).where(CompanySubscription.organization_id == identity).with_for_update())
        if agreement.status == 'NOT_SUBSCRIBED' and agreement.version == row.setup_terms_version:
            agreement.status = 'ACTIVE'
            agreement.starts_on = agreement.starts_on or (org.trial_ends_at or now()).date()
            agreement.version += 1
            agreement.updated_at = now()
            db.add(AuditEvent(organization_id=identity, entity_id=identity, action='subscription.company_enrolled'))
        if row.pending is None:
            _, count = terms(db, identity)
            start = starts(org, agreement)
            row.pending = dict(key=str(uuid4()), organization=str(identity), customer=row.stripe_customer,
                subscription=row.stripe_subscription, method=row.payment_method, status=agreement.status,
                price=str(agreement.monthly_price) if agreement.monthly_price is not None else None,
                currency=agreement.currency, quantity=count, restart=row.restart_requested, version=agreement.version, starts=int(start.timestamp()) if start else None,
                paid_until=int(row.paid_until.timestamp()) if row.paid_until else None)
        row.next_sync_at = now() + timedelta(seconds=30)
        row.lease_token = str(uuid4())
        row.lease_until = now() + timedelta(minutes=5)
        token, payload = row.lease_token, dict(row.pending)
        db.commit()
        return token, payload
    finally: db.close()


def checked(subscription, payload):
    if subscription.get('livemode') is not False or subscription.get('customer') != payload['customer'] or subscription.get('metadata', {}).get('organization_id') != payload['organization']:
        raise HTTPException(503, 'Subscription could not be verified.')
    return subscription


def current(payload):
    if payload['subscription']:
        return checked(provider.call('GET', 'subscriptions/' + payload['subscription']), payload)
    # Paginate rather than assume our retry is among the first subscriptions.
    after = None
    matches = []
    for _ in range(100):
        params = {'customer':payload['customer'], 'status':'all', 'limit':100}
        if after: params['starting_after'] = after
        page = provider.call('GET', 'subscriptions?' + urlencode(params))
        rows = page.get('data', [])
        for sub in rows:
            if sub.get('metadata', {}).get('organization_id') == payload['organization'] and sub.get('status') not in TERMINAL:
                matches.append(checked(sub, payload))
        if not page.get('has_more'): break
        if not rows: raise HTTPException(503, 'Subscription lookup is incomplete.')
        after = rows[-1]['id']
    else: raise HTTPException(503, 'Subscription lookup is incomplete.')
    if len(matches) > 1: raise HTTPException(503, 'Multiple company subscriptions require support review.')
    return matches[0] if matches else None


def product():
    obj = provider.call('GET', 'products/' + PRODUCT, missing_ok=True)
    if obj is None:
        obj = provider.call('POST', 'products', {'id':PRODUCT, 'name':'Dispatra subscription per registered vehicle'}, 'dispatra-product-test-v1')
    if obj.get('livemode') is not False: raise HTTPException(503, 'Only test billing is enabled.')
    return obj['id']


def price_data(data, prefix):
    value = Decimal(data['price']) * (1 if data['currency'] in ZERO_DECIMAL else 100)
    if value != value.to_integral_value(): raise HTTPException(422, 'The subscription rate must use whole currency minor units.')
    return {prefix + '[price_data][currency]':data['currency'].lower(),
        prefix + '[price_data][unit_amount]':str(int(value)), prefix + '[price_data][recurring][interval]':'month',
        prefix + '[price_data][product]':product(), prefix + '[quantity]':data['quantity']}


def cancel(sub, key):
    # Cancellation never bills usage/prorations or refunds an already paid period.
    return provider.call('DELETE', 'subscriptions/' + sub['id'], {'invoice_now':'false', 'prorate':'false'}, key)


def remote(data):
    sub = current(data)
    enabled = data['status'] == 'ACTIVE' and data['price'] is not None
    if not enabled:
        if sub and sub['status'] not in TERMINAL: sub = cancel(sub, data['key'] + ':cancel')
        return sub, None
    replace = bool(sub and (sub.get('currency') != data['currency'].lower() or
        (Decimal(sub['items']['data'][0]['price']['unit_amount_decimal']) == 0 and Decimal(data['price']) > 0)))
    if sub and sub['status'] in TERMINAL and not data['restart'] and not replace:
        # Remote cancellation is authoritative. An explicit later consent starts a new enrollment.
        return sub, None
    previous_end = data['paid_until'] or 0
    if replace:
        # Preserve time already paid when currency requires a new Stripe subscription.
        invoice = provider.call('GET', 'invoices/' + sub['latest_invoice']) if sub.get('latest_invoice') else None
        if invoice and invoice.get('status') == 'paid':
            previous_end = max(previous_end, sub['items']['data'][0]['current_period_end'])
        if sub['status'] not in TERMINAL: cancel(sub, data['key'] + ':currency-cancel')
        sub = None
        # Recovery can find the replacement even after the DB publish was interrupted.
        recovery = dict(data, subscription=None)
        sub = current(recovery)
    if sub and sub['status'] in TERMINAL:
        sub = current(dict(data, subscription=None))
    if sub is None:
        params = {'customer':data['customer'], 'collection_method':'charge_automatically',
            'payment_behavior':'allow_incomplete', 'off_session':'true', 'payment_settings[payment_method_types][0]':'card',
            'metadata[organization_id]':data['organization'], 'metadata[operation]':data['key'],
            **price_data(data, 'items[0]')}
        if data['method']: params['default_payment_method'] = data['method']
        start = max(data['starts'] or 0, previous_end)
        if start > int(now().timestamp()) + 5: params['trial_end'] = start
        sub = checked(provider.call('POST', 'subscriptions', params, data['key'] + ':create'), data)
    else:
        items = sub['items']['data']
        if len(items) != 1: raise HTTPException(503, 'Subscription items require support review.')
        item = items[0]
        minor = Decimal(data['price']) * (1 if data['currency'] in ZERO_DECIMAL else 100)
        changed = item['quantity'] != data['quantity'] or Decimal(item['price']['unit_amount_decimal']) != minor
        params = {'proration_behavior':'none'}
        if changed:
            params.update(price_data(data, 'items[0]'))
            params['items[0][id]'] = item['id']
        if data['method'] and sub.get('default_payment_method') != data['method']:
            params['default_payment_method'] = data['method']
        target_start = max(data['starts'] or 0, previous_end)
        if sub['status'] == 'trialing' and target_start != sub.get('trial_end'):
            params['trial_end'] = target_start if target_start > int(now().timestamp()) + 5 else 'now'
        if len(params) > 1:
            sub = checked(provider.call('POST', 'subscriptions/' + sub['id'], params, data['key'] + ':update'), data)
    invoice = provider.call('GET', 'invoices/' + sub['latest_invoice']) if sub.get('latest_invoice') else None
    if invoice and (invoice.get('customer') != data['customer'] or invoice.get('livemode') is not False):
        raise HTTPException(503, 'Subscription payment could not be verified.')
    return sub, invoice


def publish(identity, token, data, result=None, failed=False):
    db = transaction()
    try:
        org = db.scalar(select(Organization).where(Organization.id == identity).with_for_update())
        row = db.scalar(select(CompanyBilling).where(CompanyBilling.organization_id == identity).with_for_update())
        if row.lease_token != token: return
        row.lease_until, row.lease_token = None, None
        row.sync_error = failed
        row.next_sync_at = min(row.next_sync_at or now(), now() + timedelta(seconds=30))
        if not failed:
            sub, invoice = result
            row.pending = None
            agreement = db.get(CompanySubscription, identity)
            if agreement.version == data['version']: row.restart_requested = False
            row.stripe_subscription = sub['id'] if sub else None
            row.subscription_status = sub['status'] if sub else data['status'].lower()
            row.period_end = timestamp(sub['items']['data'][0].get('current_period_end')) if sub and sub.get('items', {}).get('data') else None
            row.invoice_url = invoice_url(invoice.get('hosted_invoice_url')) if invoice and invoice.get('status') == 'open' else None
            access = None
            if data['status'] == 'ACTIVE' and sub:
                if sub['status'] == 'trialing': access = timestamp(sub.get('trial_end'))
                elif sub['status'] == 'active' and invoice and invoice.get('status') == 'paid':
                    access = row.period_end
                    row.paid_until = access
            # Newer cancellation/price decisions will be processed next; never grant
            # access if an owner paused/cancelled while this remote read was running.
            agreement = db.get(CompanySubscription, identity)
            org.billing_access_until = access if agreement.status == 'ACTIVE' else None
            if sub and sub['status'] in TERMINAL and agreement.status == 'ACTIVE' and agreement.version == data['version']:
                agreement.status = 'CANCELLED'
                agreement.version += 1
                agreement.updated_at = now()
                db.add(AuditEvent(organization_id=identity, entity_id=identity, action='subscription.company_cancelled'))
        db.commit()
    finally: db.close()


def synchronize(identity):
    operation = claim(identity)
    if not operation: return False
    token, data = operation
    try:
        result = remote(data)
    except Exception:
        publish(identity, token, data, failed=True)
        raise
    publish(identity, token, data, result)
    return True


def due():
    db = transaction()
    try:
        return list(db.scalars(select(CompanyBilling.organization_id).where(CompanyBilling.consented_at.is_not(None),
            or_(CompanyBilling.next_sync_at.is_(None), CompanyBilling.next_sync_at <= now()),
            or_(CompanyBilling.lease_until.is_(None), CompanyBilling.lease_until <= now()))
            .order_by(CompanyBilling.next_sync_at.asc().nulls_first(), CompanyBilling.organization_id).limit(100)))
    finally: db.close()
