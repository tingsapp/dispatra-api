"""Read-only SaaS payment history for the one authorized Stripe customer."""
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode, urlsplit
from fastapi import HTTPException
from app.security import rate_limit
from . import provider, service
from .models import CompanyBilling
from .schemas import CompanyPaymentHistory, CompanyPaymentEntry

# Stripe charge units, including its legacy two-decimal ISK/UGX exceptions.
# https://docs.stripe.com/currencies
ZERO_DECIMAL = frozenset('BIF CLP DJF GNF JPY KMF KRW MGA PYG RWF VND VUV XAF XOF XPF'.split())


def amount(value, currency):
    return Decimal(int(value)) / (1 if currency in ZERO_DECIMAL else 100)


def receipt(value):
    try:
        parsed = urlsplit(value or '')
        return value if parsed.scheme == 'https' and parsed.hostname == 'pay.stripe.com' and parsed.username is None and parsed.port in (None,443) else None
    except ValueError:
        return None


def payments(db, identity, actor, after=None):
    service.organization(db, identity)
    rate_limit('payment-history:' + str(actor.id), 60)
    row = db.get(CompanyBilling, identity)
    available = provider.available()
    if not row or not row.stripe_customer:
        return CompanyPaymentHistory(available=True, items=[], next_cursor=None)
    if not available:
        return CompanyPaymentHistory(available=False, items=[], next_cursor=None)
    params = {'customer': row.stripe_customer, 'limit': 20}
    if after: params['starting_after'] = after
    result = provider.call('GET', 'charges?' + urlencode(params))
    rows = result.get('data')
    if not isinstance(rows, list) or len(rows) > 20:
        raise HTTPException(503, 'Payment history is temporarily unavailable.')
    items = []
    for charge in rows:
        # Provider filters never replace our explicit company/customer boundary.
        if charge.get('customer') != row.stripe_customer:
            raise HTTPException(503, 'Payment history could not be verified. Please try again.')
        status = {'succeeded':'PAID', 'pending':'PENDING', 'failed':'FAILED'}.get(charge.get('status'))
        if status is None: raise HTTPException(503, 'Payment history is temporarily unavailable.')
        refunded = int(charge.get('amount_refunded') or 0)
        if refunded: status = 'REFUNDED' if refunded >= int(charge['amount']) else 'PARTIALLY_REFUNDED'
        currency = charge['currency'].upper()
        items.append(CompanyPaymentEntry(id=charge['id'], created_at=datetime.fromtimestamp(charge['created'],timezone.utc),
            amount=amount(charge['amount'],currency), refunded_amount=amount(refunded,currency), currency=currency,
            status=status, receipt_url=receipt(charge.get('receipt_url')) if status != 'FAILED' else None))
    return CompanyPaymentHistory(available=True, items=items,
        next_cursor=items[-1].id if result.get('has_more') and items else None)
