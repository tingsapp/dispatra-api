"""Signed Stripe events reconcile current state, so replay/order cannot restore an old card."""
import hashlib
import hmac
import json
import os
import time
from fastapi import HTTPException
from sqlalchemy import select
from app.database import context
from .models import CompanyBilling
from . import service, recurring


def verify(payload, signature):
    secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
    if not secret: raise HTTPException(503, 'Payment notifications are unavailable.')
    try:
        parts = [part.split('=', 1) for part in signature.split(',')]
        timestamp = next(value for name, value in parts if name == 't')
        valid_time = abs(time.time() - int(timestamp)) <= 300
        expected = hmac.new(secret.encode(), timestamp.encode() + b'.' + payload, hashlib.sha256).hexdigest()
        valid_signature = any(name == 'v1' and hmac.compare_digest(expected, value) for name, value in parts)
        if not valid_time or not valid_signature: raise ValueError()
        event = json.loads(payload)
        if not isinstance(event, dict) or not isinstance(event.get('type'), str) or not isinstance(event.get('data'), dict) or not isinstance(event['data'].get('object'), dict): raise ValueError()
        return event
    except (ValueError, StopIteration, TypeError):
        raise HTTPException(400, 'Invalid payment notification.') from None


def receive(db, event):
    kind = event.get('type')
    if kind not in {'checkout.session.completed', 'customer.updated', 'customer.deleted', 'payment_method.detached', 'payment_method.updated', 'payment_method.automatically_updated',
        'customer.subscription.created', 'customer.subscription.updated', 'customer.subscription.deleted',
        'invoice.created', 'invoice.upcoming', 'invoice.paid', 'invoice.payment_failed', 'invoice.payment_action_required', 'invoice.voided', 'invoice.marked_uncollectible'}:
        return
    obj = (event.get('data') or {}).get('object') or {}
    customer = obj.get('id') if kind in {'customer.updated','customer.deleted'} else obj.get('customer')
    context(db, platform=True)
    try:
        predicate = CompanyBilling.stripe_customer == customer if customer else CompanyBilling.payment_method == obj.get('id')
        row = db.scalar(select(CompanyBilling).where(predicate)) if customer or obj.get('id') else None
        if not row: return
        if kind == 'checkout.session.completed':
            if row.setup_session == obj.get('id'):
                service.complete(db, row.organization_id, obj['id'])
        elif kind.startswith(('invoice.', 'customer.subscription.')):
            if event.get('livemode') is True or obj.get('livemode') is True: raise HTTPException(400, 'Only test billing is enabled.')
            recurring.request_sync(row)
        else:
            service.refresh(db, row.organization_id)
        db.flush()
    finally:
        context(db)
