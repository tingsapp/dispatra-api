import json
from uuid import UUID, uuid4
from fastapi import HTTPException
from sqlalchemy import select, text, delete
from sqlalchemy.orm import Session
from .models import Organization, User, AuditEvent, Operation, LoginSession, OutboxEvent
from .database import context
from .events import publish
from .security import hash_password, verify
from .schemas import CustomerAccessView, CustomerView

def audit(db, actor, action, entity_id, organization_id, actor_type='USER'):
    db.add(AuditEvent(actor_id=actor.id, action=action, entity_id=entity_id, organization_id=organization_id))
    db.add(OutboxEvent(organization_id=organization_id, event_type=action, entity_id=entity_id))
    publish.record(db, actor, action, entity_id, organization_id, actor_type)

def once(db, actor, key, operation, payload, run):
    scoped = f'{actor.id}:{operation}:{key}'
    serialized = json.dumps(payload, sort_keys=True)
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': scoped})
    previous = db.get(Operation, scoped)
    if previous:
        if not previous.payload_hash.startswith('$argon2') or not verify(serialized, previous.payload_hash):
            raise HTTPException(409, 'Operation key was already used for different data.')
        return previous.result
    result = run()
    db.add(Operation(key=scoped, organization_id=actor.organization_id, payload_hash=hash_password(serialized), result=result))
    db.flush()
    return result

def create_customer(db, actor, data, key):
    def run():
        from .operations.models import Shipper
        values = data.model_dump(exclude={'login_id','password'})
        # Legacy account-only Shipper: no warehouse, so it stays out of operational lists until profiled.
        customer = Shipper(id=uuid4(), organization_id=actor.organization_id, company_name=data.name, warehouse=None, **values)
        db.add(customer); db.flush()
        db.add(User(organization_id=actor.organization_id, shipper_id=customer.id, scope=str(actor.organization_id),
                    login_id=data.login_id, password_hash=hash_password(data.password), role='SHIPPER'))
        audit(db, actor, 'customer.created', customer.id, actor.organization_id)
        db.flush()
        return {**CustomerView.model_validate(customer).model_dump(mode='json'), 'login_id': data.login_id}
    return once(db, actor, key, 'customer', data.model_dump(), run)

def update_profile(db, actor, data, key):
    def run():
        from .operations.models import Shipper
        customer = db.scalar(select(Shipper).where(Shipper.id == actor.shipper_id,
                               Shipper.organization_id == actor.organization_id).with_for_update())
        if not customer: raise HTTPException(404, 'Shipper not found.')
        if customer.version != data.version: raise HTTPException(409, 'Profile changed. Reload before saving.')
        if customer.warehouse is not None:
            fields = data.model_dump(exclude_unset=True)
            if 'address' in fields and fields['address'] != customer.address:
                raise HTTPException(422, 'Update the complete Warehouse Address through the Shipper profile endpoint.')
            if 'email' in fields and fields['email'] != customer.email:
                raise HTTPException(422, 'Ask the dispatcher to change the account login email.')
        for field, value in data.model_dump(exclude={'version'}, exclude_unset=True).items(): setattr(customer, field, value)
        customer.version += 1
        audit(db, actor, 'customer.profile_updated', customer.id, actor.organization_id)
        db.flush()
        return CustomerView.model_validate(customer).model_dump(mode='json')
    return once(db, actor, key, 'profile', data.model_dump(exclude_unset=True), run)

def reset_customer_password(db, actor, customer_id, password):
    user = db.scalar(select(User).where(User.organization_id == actor.organization_id, User.shipper_id == customer_id).with_for_update())
    if not user: raise HTTPException(404, 'Shipper not found.')
    user.password_hash = hash_password(password)
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    audit(db, actor, 'customer.password_reset', user.id, actor.organization_id)
