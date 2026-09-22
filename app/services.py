import json
from uuid import UUID, uuid4
from fastapi import HTTPException
from sqlalchemy import select, text, delete
from sqlalchemy.orm import Session
from .models import Organization, User, Customer, AuditEvent, Operation, LoginSession, OutboxEvent
from .database import context
from .security import hash_password, verify
from .schemas import OrganizationView, CustomerAccessView, CustomerView

def audit(db, actor, action, entity_id, organization_id):
    db.add(AuditEvent(actor_id=actor.id, action=action, entity_id=entity_id, organization_id=organization_id))
    db.add(OutboxEvent(organization_id=organization_id, event_type=action, entity_id=entity_id))

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

def create_organization(db, actor, data, key):
    def run():
        organization = Organization(id=uuid4(), slug=data.slug, name=data.name)
        db.add(organization); db.flush()
        db.add(User(organization_id=organization.id, scope=str(organization.id), login_id=data.admin_login,
                    password_hash=hash_password(data.password), role='DISPATCHER'))
        audit(db, actor, 'organization.created', organization.id, organization.id)
        db.flush()
        return OrganizationView.model_validate(organization).model_dump(mode='json')
    return once(db, actor, key, 'organization', data.model_dump(), run)

def create_customer(db, actor, data, key):
    def run():
        values = data.model_dump(exclude={'login_id','password'})
        customer = Customer(id=uuid4(), organization_id=actor.organization_id, **values)
        db.add(customer); db.flush()
        db.add(User(organization_id=actor.organization_id, customer_id=customer.id, scope=str(actor.organization_id),
                    login_id=data.login_id, password_hash=hash_password(data.password), role='CUSTOMER'))
        audit(db, actor, 'customer.created', customer.id, actor.organization_id)
        db.flush()
        return {**CustomerView.model_validate(customer).model_dump(mode='json'), 'login_id': data.login_id}
    return once(db, actor, key, 'customer', data.model_dump(), run)

def update_profile(db, actor, data, key):
    def run():
        customer = db.scalar(select(Customer).where(Customer.id == actor.customer_id,
                               Customer.organization_id == actor.organization_id).with_for_update())
        if not customer: raise HTTPException(404, 'Customer not found.')
        if customer.version != data.version: raise HTTPException(409, 'Profile changed. Reload before saving.')
        for field, value in data.model_dump(exclude={'version'}, exclude_unset=True).items(): setattr(customer, field, value)
        customer.version += 1
        audit(db, actor, 'customer.profile_updated', customer.id, actor.organization_id)
        db.flush()
        return CustomerView.model_validate(customer).model_dump(mode='json')
    return once(db, actor, key, 'profile', data.model_dump(exclude_unset=True), run)

def reset_customer_password(db, actor, customer_id, password):
    user = db.scalar(select(User).where(User.organization_id == actor.organization_id, User.customer_id == customer_id).with_for_update())
    if not user: raise HTTPException(404, 'Customer not found.')
    user.password_hash = hash_password(password)
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    audit(db, actor, 'customer.password_reset', user.id, actor.organization_id)
