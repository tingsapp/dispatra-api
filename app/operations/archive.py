"""Reversible directory retirement while preserving Order history."""
from fastapi import HTTPException
from sqlalchemy import delete, select
from app.models import LoginSession, User, now
from .common import changed, command, company_lock, record, version, operational_shipper
from .models import Driver, Order, RateCard, Route, Shipper, Vehicle


def archive_rate(db, actor, identity, data, key):
    def run():
        company_lock(db, actor)
        row = record(db, RateCard, actor, identity, True)
        version(row, data.version)
        if not row.active:
            raise HTTPException(409, 'Rate Card is already archived.')
        if row.is_default:
            raise HTTPException(409, 'Select another default Rate Card before archiving this one.')
        attached = db.scalar(select(Shipper.id).where(
            Shipper.organization_id == actor.organization_id, Shipper.rate_card_id == identity,
            Shipper.status == 'ACTIVE'))
        if attached:
            raise HTTPException(409, 'Move active Shippers to another Rate Card first.')
        row.active = False
        changed(db, actor, row, 'rate.archived')
        return {'id': str(row.id), 'version': row.version}
    return command(db, actor, key, 'rate-archive:' + str(identity), data.model_dump(), run)


def archive_shipper(db, actor, identity, data, key):
    def run():
        company_lock(db, actor)
        row = operational_shipper(db, actor, identity, True)
        version(row, data.version)
        if row.archived_at is not None:
            raise HTTPException(409, 'Shipper is already archived.')
        open_order = db.scalar(select(Order.id).where(
            Order.organization_id == actor.organization_id, ((Order.shipper_id == identity) | (Order.billing_shipper_id == identity)),
            Order.status.notin_(['COMPLETED', 'CANCELLED'])))
        if open_order:
            raise HTTPException(409, 'Finish or cancel the Shipper’s open Orders first.')
        account = db.scalar(select(User).where(User.organization_id == actor.organization_id,
            User.shipper_id == identity).with_for_update())
        if not account:
            raise HTTPException(409, 'Shipper account is missing.')
        row.archived_at = now()
        row.status = 'INACTIVE'
        account.active = False
        db.execute(delete(LoginSession).where(LoginSession.user_id == account.id))
        changed(db, actor, row, 'shipper.archived')
        return {'id': str(row.id), 'version': row.version}
    return command(db, actor, key, 'shipper-archive:' + str(identity), data.model_dump(), run)


def active_route(db, actor, field, identity):
    return db.scalar(select(Route.id).where(Route.organization_id == actor.organization_id,
        getattr(Route, field) == identity, Route.status.in_(['PLANNED', 'IN_PROGRESS'])))


def archive_driver(db, actor, identity, data, key):
    def run():
        company_lock(db, actor)
        row = record(db, Driver, actor, identity, True)
        version(row, data.version)
        if row.archived_at is not None:
            raise HTTPException(409, 'Driver is already archived.')
        if active_route(db, actor, 'driver_id', identity):
            raise HTTPException(409, 'Finish or release the Driver’s active Route first.')
        account = db.scalar(select(User).where(User.organization_id == actor.organization_id,
            User.driver_id == identity).with_for_update())
        if not account:
            raise HTTPException(409, 'Driver account is missing.')
        from .execution import close_duty_on_logout
        close_duty_on_logout(db, account, actor)
        row.active = False
        row.archived_at = now()
        account.active = False
        db.execute(delete(LoginSession).where(LoginSession.user_id == account.id))
        changed(db, actor, row, 'driver.archived')
        return {'id': str(row.id), 'version': row.version}
    return command(db, actor, key, 'driver-archive:' + str(identity), data.model_dump(), run)


def archive_vehicle(db, actor, identity, data, key):
    def run():
        company_lock(db, actor)
        row = record(db, Vehicle, actor, identity, True)
        version(row, data.version)
        if row.archived_at is not None:
            raise HTTPException(409, 'Vehicle is already archived.')
        if active_route(db, actor, 'vehicle_id', identity):
            raise HTTPException(409, 'Finish or release the Vehicle’s active Route first.')
        linked = db.scalar(select(Driver.id).where(Driver.organization_id == actor.organization_id,
            Driver.vehicle_id == identity, Driver.active.is_(True)))
        if linked:
            raise HTTPException(409, 'Unlink the Vehicle from its active Driver first.')
        row.active = False
        row.archived_at = now()
        changed(db, actor, row, 'vehicle.archived')
        return {'id': str(row.id), 'version': row.version}
    return command(db, actor, key, 'vehicle-archive:' + str(identity), data.model_dump(), run)
