"""Invoice issuance consumes a finalized immutable pricing snapshot."""
from decimal import Decimal
from datetime import timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from app.models import now
from app.services import audit
from .models import Invoice, Order, PricingRevision, Quote, RouteStop, OrderStop, Driver, Route, EmailDelivery
from .schemas import Booking, InvoiceView
from .common import record, command, company_lock, version, changed, settings
from .pricing import calculate, money, quote
from .orders import authorized_booking


def retain_price(db, order):
    db.add(PricingRevision(organization_id=order.organization_id, order_id=order.id,
        order_version=order.version, snapshot=order.pricing))


def settle(db, actor, order, actual_minutes=None):
    if order.pricing.get('status') != 'PRICED': raise HTTPException(409, 'Pricing review is required.')
    retain_price(db, order)
    if order.pricing['method'] == 'HOURLY' and order.pricing['context']['card'].get('settle_actual', True) and actual_minutes is None:
        from math import ceil
        visits = list(db.execute(select(RouteStop,OrderStop.kind).join(OrderStop,OrderStop.id == RouteStop.stop_id).where(
            RouteStop.organization_id == actor.organization_id,OrderStop.order_id == order.id,RouteStop.status == 'COMPLETED')))
        arrivals = [v.arrived_at for v,kind in visits if kind == 'PICKUP' and v.arrived_at]
        deliveries = [v.completed_at for v,kind in visits if kind == 'DROPOFF' and v.completed_at]
        if not arrivals or not deliveries: raise HTTPException(409,'Confirm actual billable minutes; arrival evidence is incomplete.')
        actual_minutes = max(1,ceil((max(deliveries)-min(arrivals)).total_seconds()/60))
    order.pricing = calculate(Booking.model_validate(order.facts), order.pricing['context'], actual_minutes, final=True)


def issue_invoice(db, actor, order, actual_minutes=None):
    """Issue once inside the caller's transaction, so completion and email queue commit together."""
    existing = db.scalar(select(Invoice).where(Invoice.organization_id == actor.organization_id, Invoice.order_id == order.id))
    if existing: return existing
    if order.status != 'COMPLETED': raise HTTPException(409, 'Verified Order completion is required before invoicing.')
    settle(db, actor, order, actual_minutes)
    _, config = settings(db, actor)
    identity_new = uuid4()
    issued_at = now()
    term_days = {'COD':0,'NET7':7,'NET15':15,'NET30':30,'NET45':45,'NET60':60}[order.booking['payer']['terms']]
    due_date = (issued_at.astimezone(ZoneInfo(config.time_zone)).date()+timedelta(days=term_days)).isoformat()
    invoice = Invoice(id=identity_new, organization_id=actor.organization_id, order_id=order.id,
        number='INV-' + identity_new.hex[:12].upper(), subtotal=Decimal(order.pricing['subtotal']),
        tax=Decimal(order.pricing['tax']), total=Decimal(order.pricing['total']),
        snapshot={'order_number': order.number, 'issuer': config.model_dump(mode='json', include={
            'company_name','contact_name','email','phone','address','logo_url','tax_registration_number'}),
            'booking': order.booking, 'pricing': order.pricing, 'issued_at': issued_at.isoformat(), 'due_date':due_date})
    db.add(invoice); db.flush()
    order.status = 'INVOICED'; changed(db, actor, order, 'order.invoiced')
    audit(db, actor, 'invoice.created', invoice.id, actor.organization_id)
    return invoice


def auto_invoice(db, actor, order):
    """Queue the frozen invoice on completion; unresolved hourly time stays for dispatcher review."""
    if order.pricing.get('method') == 'HOURLY' and order.pricing['context']['card'].get('settle_actual', True):
        visits = list(db.execute(select(RouteStop, OrderStop.kind).join(OrderStop, OrderStop.id == RouteStop.stop_id).where(
            RouteStop.organization_id == actor.organization_id, OrderStop.order_id == order.id, RouteStop.status == 'COMPLETED')))
        if not any(v.arrived_at for v, kind in visits if kind == 'PICKUP') or not any(v.completed_at for v, kind in visits if kind == 'DROPOFF'):
            return None
    from .orders import company_scope
    from .mail import queue_invoice
    with company_scope(db, actor):
        invoice = issue_invoice(db, actor, order)
        queue_invoice(db, actor, invoice)
    return invoice


def create_invoice(db, actor, identity, data, key):
    def run():
        company_lock(db, actor)
        order = record(db, Order, actor, identity, True)
        existing = db.scalar(select(Invoice).where(Invoice.organization_id == actor.organization_id, Invoice.order_id == identity))
        if existing:
            queued = db.scalar(select(EmailDelivery.id).where(EmailDelivery.organization_id == actor.organization_id,
                EmailDelivery.invoice_id == existing.id).limit(1))
            if not queued:
                from .mail import queue_invoice
                queue_invoice(db, actor, existing)
            return InvoiceView.model_validate(existing).model_dump(mode='json')
        version(order, data.version)
        invoice = issue_invoice(db, actor, order, data.actual_minutes)
        from .mail import queue_invoice
        queue_invoice(db, actor, invoice)
        return InvoiceView.model_validate(invoice).model_dump(mode='json')
    return command(db, actor, key, 'invoice:' + str(identity), data.model_dump(mode='json'), run)


def create_quote(db, actor, booking, key):
    if actor.role != 'DISPATCHER': raise HTTPException(403, 'Prospect quotes require dispatcher access.')
    if booking.shipper_id is not None or booking.rate_card_id is None:
        raise HTTPException(422, 'New Quote requires a Rate Card and no Shipper.')
    def run():
        row = Quote(organization_id=actor.organization_id, facts=booking.model_dump(mode='json'), pricing=quote(db, actor, booking))
        db.add(row); db.flush(); audit(db, actor, 'quote.created', row.id, actor.organization_id)
        return {'id': str(row.id), 'version': row.version, 'created_at': row.created_at.isoformat(), 'facts': row.facts, 'pricing': row.pricing}
    return command(db, actor, key, 'quote-create', booking.model_dump(mode='json'), run)


def invoice_document(row):
    from .invoice_pdf import render
    return render(row.number, row.snapshot, row.subtotal, row.tax, row.total)


def invoice_view(row, actor):
    result = InvoiceView.model_validate(row).model_dump(mode='json')
    if actor.role == 'SHIPPER':
        result['snapshot'] = {**result['snapshot'], 'pricing': {**result['snapshot']['pricing'], 'context': {}}}
    return result
