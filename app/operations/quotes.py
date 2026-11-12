"""Prospect quote creation through the authoritative pricing service."""
from fastapi import HTTPException
from app.services import audit
from .models import Quote
from .common import command
from .pricing import quote


def create_quote(db, actor, booking, key):
    if actor.role != 'DISPATCHER': raise HTTPException(403, 'Prospect quotes require dispatcher access.')
    if booking.shipper_id is not None or booking.rate_card_id is None:
        raise HTTPException(422, 'New Quote requires a Rate Card and no Shipper.')
    def run():
        row = Quote(organization_id=actor.organization_id, facts=booking.model_dump(mode='json'), pricing=quote(db, actor, booking))
        db.add(row); db.flush(); audit(db, actor, 'quote.created', row.id, actor.organization_id)
        return {'id': str(row.id), 'version': row.version, 'created_at': row.created_at.isoformat(), 'facts': row.facts, 'pricing': row.pricing}
    return command(db, actor, key, 'quote-create', booking.model_dump(mode='json'), run)
