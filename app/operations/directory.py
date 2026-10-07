"""Company settings, catalogues, and transactional profile/account administration."""
import secrets
from uuid import uuid4
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, delete
from app.models import User, LoginSession, Organization, now
from app.security import hash_password
from app.services import audit
from .models import Catalog, RateCard, Shipper, Driver, Vehicle, Route, Settings, DutySession
from .identifiers import issue_number
from .schemas import SettingsView, CatalogView, RateView
from .common import record, version, company_lock, command, changed, dump, settings, operational_shipper


def save_settings(db, actor, data, key):
    def run():
        company_lock(db, actor)
        row, _ = settings(db, actor)
        version(row, data.version)
        if data.data.default_service_id:
            service = db.get(Catalog, data.data.default_service_id)
            if service is None or service.organization_id != actor.organization_id or service.kind != 'SERVICE' or not service.active:
                raise HTTPException(422, 'Choose an active service level as the default.')
        row.data = data.data.model_dump(mode='json')
        db.get(Organization, actor.organization_id).name = data.data.company_name
        changed(db, actor, row, 'settings.updated')
        return SettingsView.model_validate(row).model_dump(mode='json')
    return command(db, actor, key, 'settings', data.model_dump(mode='json'), run)


def save_catalog(db, actor, data, key, identity=None):
    def run():
        company_lock(db, actor)
        if identity:
            row = record(db, Catalog, actor, identity, True)
            version(row, data.version)
            if not row.active: raise HTTPException(409, 'Deleted catalogue entry cannot be edited.')
            row.data = data.data.model_dump(mode='json')
            changed(db, actor, row, 'catalog.updated')
        else:
            row = Catalog(organization_id=actor.organization_id, kind=data.kind, code=data.code, data=data.data.model_dump(mode='json'))
            db.add(row); db.flush()
            audit(db, actor, 'catalog.created', row.id, actor.organization_id)
        return CatalogView.model_validate(row).model_dump(mode='json')
    return command(db, actor, key, 'catalog:' + str(identity or 'new'), data.model_dump(mode='json'), run)


def save_rate(db, actor, data, key, identity=None):
    def run():
        company_lock(db, actor)
        if data.is_default and not getattr(data, 'active', True): raise HTTPException(422, 'Default rate card must be active.')
        if data.is_default:
            for old in db.scalars(select(RateCard).where(RateCard.organization_id == actor.organization_id, RateCard.is_default.is_(True))):
                if old.id != identity:
                    old.is_default = False
                    changed(db, actor, old, 'rate.default_removed')
        if identity:
            row = record(db, RateCard, actor, identity, True)
            version(row, data.version)
            if not row.active: raise HTTPException(409, 'Archived Rate Card cannot be edited.')
            row.data = data.data.model_dump(mode='json')
            row.is_default, row.active = data.is_default, data.active
            changed(db, actor, row, 'rate.updated')
        else:
            row = RateCard(organization_id=actor.organization_id, code=data.code, is_default=data.is_default, data=data.data.model_dump(mode='json'))
            db.add(row); db.flush()
            audit(db, actor, 'rate.created', row.id, actor.organization_id)
        return RateView.model_validate(row).model_dump(mode='json')
    return command(db, actor, key, 'rate:' + str(identity or 'new'), data.model_dump(mode='json'), run)


def remove_catalog(db, actor, identity, data, key):
    def run():
        row = record(db, Catalog, actor, identity, True)
        version(row, data.version)
        if str(row.id) == str(settings(db, actor)[1].default_service_id):
            raise HTTPException(409, 'Set another service level as Default before deleting this one.')
        row.active = False
        changed(db, actor, row, 'catalog.deleted')
        return {'id': str(row.id), 'version': row.version}
    return command(db, actor, key, 'catalog-delete:' + str(identity), data.model_dump(), run)


def default_rate(db, actor):
    return db.scalar(select(RateCard).where(RateCard.organization_id == actor.organization_id, RateCard.is_default.is_(True), RateCard.active.is_(True)))


def shipper_view(db, actor, row):
    """`rate_card_name` is the card that prices this shipper's orders: its own, else the company Default."""
    card = record(db, RateCard, actor, row.rate_card_id) if row.rate_card_id else default_rate(db, actor)
    return jsonable_encoder({**{k: v for k, v in dump(row).items() if k not in {'contact_name', 'address'}},
        'rate_card_name': card.data['name'] if card else None})


def save_shipper(db, actor, data, key, identity=None):
    password = None
    def run():
        nonlocal password
        company_lock(db, actor)
        payload = data.data if identity else data
        if payload.rate_card_id:
            rate = record(db, RateCard, actor, payload.rate_card_id)
            if not rate.active: raise HTTPException(422, 'Choose an active Rate Card.')
        if identity:
            row = customer = operational_shipper(db, actor, identity, True); version(row, data.version)
            if row.archived_at: raise HTTPException(409, 'Archived Shipper cannot be edited.')
            user = db.scalar(select(User).where(User.shipper_id == identity, User.organization_id == actor.organization_id).with_for_update())
        else:
            identity_new = uuid4()
            row = customer = Shipper(id=identity_new, organization_id=actor.organization_id,
                number=issue_number(db, actor, Shipper, 'S'), name=payload.name)
            db.add(row); db.flush()  # the account row references shippers
            password = secrets.token_urlsafe(18)
            user = User(organization_id=actor.organization_id, shipper_id=identity_new, scope=str(actor.organization_id),
                role='SHIPPER', password_hash=hash_password(password), login_id=payload.email)
            db.add(user)
        if identity and data.status is not None:
            customer.status = data.status
            user.active = data.status != 'INACTIVE'
            if not user.active: db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        customer.name, customer.email, customer.phone = payload.name, payload.email, payload.phone
        customer.address = payload.warehouse.text
        row.kind, row.company_name = payload.kind, payload.company_name
        row.warehouse, row.discount = payload.warehouse.model_dump(mode='json'), payload.discount.model_dump(mode='json')
        # Every shipper carries a Rate Card; without a choice it is the company Default at save time.
        default = None if payload.rate_card_id else default_rate(db, actor)
        row.rate_card_id = payload.rate_card_id or (default.id if default else None)
        row.terms, row.instructions, row.email_updates = payload.terms, payload.instructions, payload.email_updates
        if user.login_id != payload.email:
            user.login_id = payload.email
            db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        if identity: changed(db, actor, row, 'shipper.updated')
        else:
            db.flush(); audit(db, actor, 'shipper.created', row.id, actor.organization_id)
        return shipper_view(db, actor, row)
    result = command(db, actor, key, 'shipper:' + str(identity or 'new'), data.model_dump(mode='json'), run)
    return {**result, 'initial_password': password}


def in_use(db, actor, field, identity):
    return db.scalar(select(Route.id).where(Route.organization_id == actor.organization_id,
        getattr(Route, field) == identity, Route.status.in_(['PLANNED', 'IN_PROGRESS']))) is not None


def save_vehicle(db, actor, data, key, identity=None):
    def run():
        company_lock(db, actor)
        payload = data.data if identity else data
        vehicle_type = record(db, Catalog, actor, payload.type_id)
        if vehicle_type.kind != 'VEHICLE_TYPE' or not vehicle_type.active: raise HTTPException(422, 'Choose an active vehicle type.')
        if identity:
            row = record(db, Vehicle, actor, identity, True); version(row, data.version)
            if row.archived_at: raise HTTPException(409, 'Archived Vehicle cannot be edited.')
            if in_use(db, actor, 'vehicle_id', identity): raise HTTPException(409, 'Vehicle has an active Route; finish or cancel it before editing.')
        else:
            row = Vehicle(organization_id=actor.organization_id, number=issue_number(db, actor, Vehicle, 'V')); db.add(row)
        row.type_id, row.plate, row.province = payload.type_id, payload.plate.upper(), payload.province.upper()
        row.active, row.data = payload.active, payload.model_dump(mode='json')
        if identity: changed(db, actor, row, 'vehicle.updated')
        else:
            db.flush(); audit(db, actor, 'vehicle.created', row.id, actor.organization_id)
        return jsonable_encoder(dump(row))
    return command(db, actor, key, 'vehicle:' + str(identity or 'new'), data.model_dump(mode='json'), run)


def driver_view(db, actor, row):
    on_duty = db.scalar(select(DutySession.id).where(
        DutySession.organization_id == actor.organization_id,
        DutySession.driver_id == row.id, DutySession.ended_at.is_(None))) is not None
    return jsonable_encoder({**dump(row), 'on_duty': on_duty})


def save_driver(db, actor, data, key, identity=None):
    password = None
    def run():
        nonlocal password
        company_lock(db, actor)
        payload = data.data if identity else data
        if payload.vehicle_id:
            vehicle = record(db, Vehicle, actor, payload.vehicle_id, True)
            if not vehicle.active: raise HTTPException(422, 'Choose an active vehicle.')
        if identity:
            row = record(db, Driver, actor, identity, True); version(row, data.version)
            if row.archived_at: raise HTTPException(409, 'Archived Driver cannot be edited.')
            if in_use(db, actor, 'driver_id', identity): raise HTTPException(409, 'Driver has an active Route; finish or cancel it before editing.')
            user = db.scalar(select(User).where(User.driver_id == identity, User.organization_id == actor.organization_id).with_for_update())
            if data.duty_status is not None:
                current_duty = db.scalar(select(DutySession.id).where(
                    DutySession.organization_id == actor.organization_id,
                    DutySession.driver_id == row.id, DutySession.ended_at.is_(None))) is not None
                if current_duty != data.expected_on_duty:
                    raise HTTPException(409, 'Driver duty changed since this form was opened. Reload and try again.')
        else:
            row = Driver(id=uuid4(), organization_id=actor.organization_id, number=issue_number(db, actor, Driver, 'D')); db.add(row)
            password = secrets.token_urlsafe(18)
            user = User(organization_id=actor.organization_id, driver_id=row.id, scope=str(actor.organization_id),
                role='DRIVER', password_hash=hash_password(password), login_id=payload.email)
        row.name, row.email, row.phone = payload.name, payload.email, payload.phone
        row.address = payload.address.model_dump(mode='json')
        row.service_city = ' '.join(payload.address.city.casefold().split())
        row.vehicle_id, row.active = payload.vehicle_id, payload.active
        row.data = payload.model_dump(mode='json', exclude={'name', 'email', 'phone', 'address', 'vehicle_id', 'active'})
        db.flush()
        if not identity: db.add(user)
        if user.login_id != payload.email or user.active != payload.active:
            user.login_id, user.active = payload.email, payload.active
            db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
            if not payload.active:
                from .execution import close_duty_on_logout
                close_duty_on_logout(db,user,actor)
        if identity and data.duty_status is not None:
            if data.duty_status == 'ON_DUTY' and not payload.active:
                raise HTTPException(409, 'Activate the driver account before setting On Duty.')
            duty = db.scalar(select(DutySession).where(
                DutySession.organization_id == actor.organization_id,
                DutySession.driver_id == row.id, DutySession.ended_at.is_(None)).with_for_update())
            if data.duty_status == 'ON_DUTY' and duty is None:
                duty = DutySession(organization_id=actor.organization_id, driver_id=row.id, started_at=now())
                db.add(duty); db.flush()
                audit(db, actor, 'duty.started_by_dispatcher', duty.id, actor.organization_id)
            elif data.duty_status == 'OFF_DUTY' and duty is not None:
                duty.ended_at = now()
                changed(db, actor, duty, 'duty.ended_by_dispatcher')
        if identity: changed(db, actor, row, 'driver.updated')
        else:
            db.flush(); audit(db, actor, 'driver.created', row.id, actor.organization_id)
        return driver_view(db, actor, row)
    result = command(db, actor, key, 'driver:' + str(identity or 'new'), data.model_dump(mode='json'), run)
    return {**result, 'initial_password': password}


def reset_driver_password(db, actor, identity, key):
    password = None
    def run():
        nonlocal password
        driver = record(db, Driver, actor, identity, True)
        account = db.scalar(select(User).where(User.organization_id == actor.organization_id, User.driver_id == driver.id).with_for_update())
        if not account: raise HTTPException(404, 'Driver account not found.')
        password = secrets.token_urlsafe(18)
        account.password_hash = hash_password(password)
        db.execute(delete(LoginSession).where(LoginSession.user_id == account.id))
        from .execution import close_duty_on_logout
        close_duty_on_logout(db,account,actor)
        audit(db, actor, 'driver.password_reset', driver.id, actor.organization_id)
        return {'driver_id':str(driver.id),'login_id':account.login_id}
    result = command(db,actor,key,'driver-reset:' + str(identity),{},run)
    return {**result,'initial_password':password}


def update_shipper_profile(db, actor, data, key):
    def run():
        row = customer = operational_shipper(db,actor,actor.shipper_id,True); version(row,data.version)
        row.warehouse = data.warehouse.model_dump(mode='json')
        customer.phone, customer.address = data.phone, data.warehouse.text
        if data.contact_name is not None:
            customer.name = data.contact_name.strip()
            customer.contact_name = customer.name
        changed(db,actor,row,'shipper.profile_updated')
        return shipper_view(db,actor,row)
    return command(db,actor,key,'shipper-profile',data.model_dump(mode='json'),run)
