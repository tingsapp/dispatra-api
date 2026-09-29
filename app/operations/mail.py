"""Explicit dispatcher email commands; SMTP runs after the transaction commits."""
from datetime import datetime
from html import escape
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from app.models import now
from app.services import audit
from .common import command, record, settings, version
from .models import EmailDelivery, Invoice, Quote
from .schemas import EmailDeliveryView


def _price_lines(pricing):
    return [(str(line['label']), str(line['amount'])) for line in pricing.get('lines', [])]


def _message(company, title, recipient_name, pricing, details):
    currency = pricing.get('currency', 'CAD')
    lines = _price_lines(pricing)
    summary = [('Subtotal', str(pricing['subtotal'])), ('Tax', str(pricing['tax'])),
               ('Total', f"{pricing['total']} {currency}")]
    text_lines = [company, title, '', f'To: {recipient_name}', *details, '',
                  *(f'{label}: {amount}' for label, amount in lines + summary)]
    body_text = '\n'.join(text_lines) + '\n'
    table = ''.join(f'<tr><td>{escape(label)}</td><td>{escape(amount)}</td></tr>'
                    for label, amount in lines + summary)
    body_html = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                 f'<h1>{escape(title)}</h1><p>{escape(company)}</p>'
                 f'<p>To: {escape(recipient_name)}</p>'
                 + ''.join(f'<p>{escape(detail)}</p>' for detail in details)
                 + f'<table>{table}</table></html>')
    return body_text, body_html


def _queue(db, actor, *, recipient, subject, body_text, body_html, quote_id=None, invoice_id=None):
    source = (EmailDelivery.quote_id == quote_id) if quote_id else (EmailDelivery.invoice_id == invoice_id)
    duplicate = db.scalar(select(EmailDelivery.id).where(
        EmailDelivery.organization_id == actor.organization_id, source,
        EmailDelivery.recipient == recipient,
        EmailDelivery.status.in_(['PENDING', 'SENDING'])))
    if duplicate:
        raise HTTPException(409, 'An email for this recipient is already pending.')
    identity = uuid4()
    row = EmailDelivery(id=identity, organization_id=actor.organization_id,
        quote_id=quote_id, invoice_id=invoice_id, requested_by=actor.id,
        recipient=recipient, subject=subject, body_text=body_text, body_html=body_html,
        message_id=f'<dispatra-{identity.hex}@dispatra.com>')
    db.add(row)
    db.flush()
    audit(db, actor, 'email.requested', row.id, actor.organization_id)
    return EmailDeliveryView.model_validate(row).model_dump(mode='json')


def send_quote(db, actor, identity, data, key):
    def run():
        row = record(db, Quote, actor, identity)
        version(row, data.version)
        if row.pricing.get('status') != 'PRICED':
            raise HTTPException(409, 'Resolve quote pricing before sending.')
        if datetime.fromisoformat(row.pricing['expires_at']) <= now():
            raise HTTPException(409, 'Quote has expired; create a new quote.')
        _, company = settings(db, actor)
        pickup = next((s for s in row.facts['stops'] if s['kind'] == 'PICKUP'), None)
        dropoff = next((s for s in row.facts['stops'] if s['kind'] == 'DROPOFF'), None)
        details = [f"Pickup: {pickup['address']['text']}" if pickup else 'Pickup: not specified',
                   f"Delivery: {dropoff['address']['text']}" if dropoff else 'Delivery: not specified',
                   f"Valid until: {row.pricing['expires_at']}"]
        title = f'Quote {str(row.id)[:8].upper()}'
        body_text, body_html = _message(company.company_name, title, str(data.recipient), row.pricing, details)
        return _queue(db, actor, recipient=str(data.recipient).lower(),
            subject=f'{company.company_name} — {title}', body_text=body_text,
            body_html=body_html, quote_id=row.id)
    return command(db, actor, key, 'quote-send:' + str(identity), data.model_dump(mode='json'), run)


def send_invoice(db, actor, identity, data, key):
    def run():
        row = record(db, Invoice, actor, identity)
        version(row, data.version)
        snap = row.snapshot
        recipient = snap['booking']['payer']['email'].strip().lower()
        if not recipient:
            raise HTTPException(409, 'Invoice billing email is missing.')
        title = f'Invoice {row.number}'
        details = [f"Order: {snap['order_number']}", f"Issued: {snap['issued_at']}",
                   f"Due: {snap['due_date']}"]
        body_text, body_html = _message(snap['issuer']['company_name'], title,
            snap['booking']['payer']['company_name'] or snap['booking']['payer']['name'],
            snap['pricing'], details)
        return _queue(db, actor, recipient=recipient,
            subject=f"{snap['issuer']['company_name']} — {title}",
            body_text=body_text, body_html=body_html, invoice_id=row.id)
    return command(db, actor, key, 'invoice-send:' + str(identity), data.model_dump(mode='json'), run)
