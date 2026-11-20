from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import select, text
from app.services import audit, once
from .models import Settings
from .schemas import SettingsData


def record(db, model, actor, identity, lock=False):
    query = select(model).where(model.id == identity, model.organization_id == actor.organization_id)
    if lock: query = query.with_for_update()
    value = db.scalar(query)
    if value is None: raise HTTPException(404, 'Record not found.')
    return value


def operational_shipper(db, actor, identity, lock=False):
    """A Shipper with an operational profile; legacy account-only records (NULL warehouse) are not bookable."""
    from .models import Shipper
    row = record(db, Shipper, actor, identity, lock)
    if row.warehouse is None: raise HTTPException(404, 'Record not found.')
    return row


def version(row, expected):
    if row.version != expected: raise HTTPException(409, 'Record changed. Reload before continuing.')


def company_lock(db, actor):
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))'), {'scope': 'operations:' + str(actor.organization_id)})


def settings(db, actor):
    row = db.scalar(select(Settings).where(Settings.organization_id == actor.organization_id))
    if row is None: raise HTTPException(409, 'Company settings have not been initialized. Run the pricing seeder.')
    return row, SettingsData.model_validate(row.data)


def changed(db, actor, row, action):
    row.version += 1
    audit(db, actor, action, row.id, actor.organization_id)
    db.flush()


def command(db, actor, key, name, payload, run):
    """Persist only public command results, never transient credentials/evidence bytes."""
    return once(db, actor, key, name, payload, run)


def located(address, what):
    """Saved addresses carry a map point: one sent without coordinates is geocoded, or refused naming `what`."""
    if address.latitude is not None: return address
    from app.intake import geocode
    found = geocode.verify(address.text, address.postal_code)
    if not found: raise HTTPException(422, f'We couldn’t place the {what} “{address.text}” on the map. Choose it from the address suggestions.')
    return address.model_copy(update={'latitude': found['latitude'], 'longitude': found['longitude']})


def dump(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns if column.key != 'organization_id'}
