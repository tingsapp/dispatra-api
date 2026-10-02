from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from app.models import now
from app.services import audit
from .models import Issue, Order, OrderStop, Route, RouteStop
from .common import record, version, changed, command, dump


def report(db, actor, identity, data, key):
    def run():
        order = record(db, Order, actor, identity, True)
        if actor.role == 'DRIVER':
            route = record(db, Route, actor, order.route_id) if order.route_id else None
            if not route or route.driver_id != actor.driver_id: raise HTTPException(404, 'Order not found.')
        elif actor.role == 'SHIPPER' and order.shipper_id != actor.shipper_id: raise HTTPException(404, 'Order not found.')
        if order.status in {'INVOICED','CANCELLED'}: raise HTTPException(409, 'Order is closed.')
        if data.stop_id:
            stop = record(db, OrderStop, actor, data.stop_id)
            if stop.order_id != identity: raise HTTPException(404, 'Stop not found.')
        row = Issue(organization_id=actor.organization_id, order_id=identity, **data.model_dump())
        db.add(row); db.flush(); audit(db, actor, 'issue.reported', row.id, actor.organization_id)
        if order.status == 'COMPLETED':
            order.status = 'IN_PROGRESS'; order.completed_at = None; changed(db, actor, order, 'order.completion_held')
        return jsonable_encoder(dump(row))
    return command(db, actor, key, 'issue:' + str(identity), data.model_dump(mode='json'), run)


def resolve(db, actor, identity, data, key):
    def run():
        row = record(db, Issue, actor, identity, True); version(row, data.version)
        row.resolved, row.resolution = True, data.resolution
        changed(db, actor, row, 'issue.resolved')
        order = record(db, Order, actor, row.order_id, True)
        pending = db.scalar(select(RouteStop.id).join(OrderStop, OrderStop.id == RouteStop.stop_id).where(
            OrderStop.order_id == order.id, RouteStop.organization_id == actor.organization_id, RouteStop.status != 'COMPLETED'))
        unresolved = db.scalar(select(Issue.id).where(Issue.organization_id == actor.organization_id, Issue.order_id == order.id, Issue.resolved.is_(False)))
        if order.status == 'IN_PROGRESS' and order.route_id and not pending and not unresolved:
            order.status, order.completed_at = 'COMPLETED', now()
            changed(db, actor, order, 'order.completed')
        return jsonable_encoder(dump(row))
    return command(db, actor, key, 'issue-resolve:' + str(identity), data.model_dump(), run)
