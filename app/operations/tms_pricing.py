"""Select the company's pricing default with its TMS choice, preserving configured rates."""
from fastapi import HTTPException
from sqlalchemy import select
from app.services import audit
from .models import RateCard
from .schemas import RateData


def apply_workspace_rate(db, actor, settings, external):
    # Caller holds the company command lock and commits settings + rates together.
    org = settings.organization_id
    cards = list(db.scalars(select(RateCard).where(RateCard.organization_id == org).with_for_update()))
    defaults = [card for card in cards if card.is_default]
    native = next((card for card in defaults if card.active and card.data['method'] != 'IMPORTED'), None)
    if external:
        if native: settings.native_rate_card_id = native.id
        target = next((card for card in cards if card.code == 'IMPORTED' and card.data['method'] == 'IMPORTED'), None)
        target = target or next((card for card in cards if card.code == 'TMS_IMPORTED'), None)
        if target and target.data['method'] != 'IMPORTED':
            raise HTTPException(409, 'The TMS pricing code is already used by another pricing method.')
        if target is None:
            target = RateCard(organization_id=org, code='TMS_IMPORTED', active=True, is_default=False,
                data=RateData(name='Imported price (TMS)', method='IMPORTED', minimum_subtotal=0,
                    apply_fuel=False, apply_service=False, apply_vehicle=False, apply_accessorials=False).model_dump(mode='json'))
            db.add(target); db.flush()
            audit(db, actor, 'rate.created', target.id, org)
        name = target.data['name']
    else:
        target = native or next((card for card in cards if card.id == settings.native_rate_card_id
            and card.active and card.data['method'] != 'IMPORTED'), None)
        # The original seeded default is the explicit fallback for older external workspaces.
        target = target or next((card for card in cards if card.code == 'STD' and card.active
            and card.data['method'] != 'IMPORTED'), None)
        if target is None:
            raise HTTPException(409, 'Restore an active Dispatra Rate Card before switching to Dispatra TMS.')
        name = target.data['name']
    for old in defaults:
        if old.id != target.id:
            old.is_default = False; old.version += 1
            audit(db, actor, 'rate.default_removed', old.id, org)
    # Release the one-default index before promoting another row; ORM update order varies by UUID.
    db.flush()
    if not target.active or not target.is_default or target.data['name'] != name:
        target.active = True; target.is_default = True
        target.data = {**target.data, 'name': name}; target.version += 1
        audit(db, actor, 'rate.updated', target.id, org)
    db.flush()
