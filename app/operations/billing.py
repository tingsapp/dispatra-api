"""Manual invoice issuance consumes a finalized immutable pricing snapshot."""
from decimal import Decimal
from datetime import timedelta
from zoneinfo import ZoneInfo
from html import escape
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from app.models import now
from app.services import audit
from .models import Invoice, Order, PricingRevision, Quote, RouteStop, OrderStop, Driver, Route
from .schemas import Booking, InvoiceView
from .common import record, command, company_lock, version, changed, settings
from .pricing import calculate, money, quote
from .orders import authorized_booking


def retain_price(db, order):
    db.add(PricingRevision(organization_id=order.organization_id, order_id=order.id,
        order_version=order.version, snapshot=order.pricing))


def freeze_payout(db, actor, order):
    if not order.route_id: return
    route = record(db, Route, actor, order.route_id)
    driver = record(db, Driver, actor, route.driver_id)
    terms = order.payout or driver.data
    if terms['employment'] != 'OWNER_OPERATOR':
        order.payout = {'employment':'EMPLOYEE','driver_id':str(driver.id),'estimate':None,'basis':'NOT_APPLICABLE'}
        return
    freight_share = Decimal(terms['revenue_share_percent'])
    extra_share = Decimal(terms['fuel_surcharge_share_percent'])
    freight = sum((Decimal(l['amount']) for l in order.pricing.get('lines',[]) if l['group'] in {'FREIGHT','SERVICE'}), Decimal(0))
    extras = sum((Decimal(l['amount']) for l in order.pricing.get('lines',[]) if l['group'] == 'FUEL'), Decimal(0))
    order.payout = {'employment':'OWNER_OPERATOR', 'driver_id': str(driver.id), 'revenue_share_percent': str(freight_share),
        'fuel_surcharge_share_percent': str(extra_share), 'estimate': str(money(money(max(0,freight)*freight_share/100) + money(extras*extra_share/100))),
        'basis': 'ESTIMATE_NOT_PAYMENT', 'currency': order.pricing.get('currency','CAD')}


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
    freeze_payout(db, actor, order)


def create_invoice(db, actor, identity, data, key):
    def run():
        company_lock(db, actor)
        order = record(db, Order, actor, identity, True)
        existing = db.scalar(select(Invoice).where(Invoice.organization_id == actor.organization_id, Invoice.order_id == identity))
        if existing: return InvoiceView.model_validate(existing).model_dump(mode='json')
        version(order, data.version)
        if order.status != 'COMPLETED': raise HTTPException(409, 'Verified Order completion is required before invoicing.')
        settle(db, actor, order, data.actual_minutes)
        _, config = settings(db, actor)
        identity_new = uuid4()
        issued_at = now()
        term_days = {'COD':0,'NET7':7,'NET15':15,'NET30':30,'NET45':45,'NET60':60}[order.booking['payer']['terms']]
        due_date = (issued_at.astimezone(ZoneInfo(config.time_zone)).date()+timedelta(days=term_days)).isoformat()
        invoice = Invoice(id=identity_new, organization_id=actor.organization_id, order_id=identity,
            number='INV-' + identity_new.hex[:12].upper(), subtotal=Decimal(order.pricing['subtotal']),
            tax=Decimal(order.pricing['tax']), total=Decimal(order.pricing['total']),
            snapshot={'order_number': order.number, 'issuer': config.model_dump(mode='json', include={
                'company_name','contact_name','email','phone','address','logo_url','tax_registration_number'}),
                'booking': order.booking, 'pricing': order.pricing, 'issued_at': issued_at.isoformat(), 'due_date':due_date})
        db.add(invoice); db.flush()
        order.status = 'INVOICED'; changed(db, actor, order, 'order.invoiced')
        audit(db, actor, 'invoice.created', invoice.id, actor.organization_id)
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
    snap = row.snapshot
    e = lambda value: escape(str(value))
    lines = ''.join(f'<tr><td>{e(l["label"])}</td><td>{e(l["amount"])}</td></tr>' for l in snap['pricing']['lines'])
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>{e(row.number)}</title>
<style>body{{font:14px sans-serif;max-width:760px;margin:40px auto;color:#111}}table{{width:100%;border-collapse:collapse}}td{{padding:9px;border-bottom:1px solid #ddd}}td:last-child{{text-align:right}}</style>
<h1>Invoice {e(row.number)}</h1><p>{e(snap['issuer']['company_name'])}</p>
<p>Order {e(snap['order_number'])} · Issued {e(snap['issued_at'])}</p>
<p>Bill to: {e(snap['booking']['payer']['company_name'] or snap['booking']['payer']['name'])}<br>{e(snap['booking']['payer']['email'])}</p>
<p>Tax registration: {e(snap['issuer']['tax_registration_number'])} · Terms: {e(snap['booking']['payer']['terms'])} · Due: {e(snap['due_date'])}</p>
<table>{lines}</table><p>Subtotal: {e(row.subtotal)} · Tax: {e(row.tax)}</p><h2>Total: {e(row.total)} {e(snap['pricing']['currency'])}</h2></html>'''


def invoice_view(row, actor):
    result = InvoiceView.model_validate(row).model_dump(mode='json')
    if actor.role == 'SHIPPER':
        result['snapshot'] = {**result['snapshot'], 'pricing': {**result['snapshot']['pricing'], 'context': {}}}
    return result
