"""Quote email queueing; SMTP runs after the transaction commits."""
from datetime import datetime
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from app.email_templates import quote as _message
from app.models import now
from app.services import audit
from .common import command, record, settings, version
from .models import EmailDelivery, Quote
from .schemas import EmailDeliveryView


def _queue(db, actor, *, recipient, subject, body_text, body_html, quote_id):
    source = EmailDelivery.quote_id == quote_id
    duplicate = db.scalar(select(EmailDelivery.id).where(
        EmailDelivery.organization_id == actor.organization_id, source,
        EmailDelivery.recipient == recipient,
        EmailDelivery.status.in_(['PENDING', 'SENDING'])))
    if duplicate:
        raise HTTPException(409, 'An email for this recipient is already pending.')
    identity = uuid4()
    row = EmailDelivery(id=identity, organization_id=actor.organization_id,
        quote_id=quote_id, requested_by=actor.id,
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
            subject=f'{company.company_name}, {title}', body_text=body_text,
            body_html=body_html, quote_id=row.id)
    return command(db, actor, key, 'quote-send:' + str(identity), data.model_dump(mode='json'), run)
