"""Driver-owned duty, telemetry, evidence and ordered stop execution."""
import base64
import hashlib
from datetime import timedelta
from uuid import UUID
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from app.models import now
from app.services import audit
from .models import Driver, Route, RouteStop, Order, OrderStop, DutySession, Location, Evidence, Issue
from .common import record, version, changed, command, company_lock, dump
from .dispatch import route_view


def own_route(db, actor, identity, lock=False):
    route = record(db, Route, actor, identity, lock)
    if actor.role != 'DRIVER' or route.driver_id != actor.driver_id: raise HTTPException(404, 'Route not found.')
    return route


def route_revision(route, data):
    version(route, data.version)
    if route.generation != data.generation: raise HTTPException(409, 'Assignment generation changed. Sync the Route.')


def start_duty(db, actor, data, key):
    def run():
        driver = record(db, Driver, actor, actor.driver_id, True)
        current = db.scalar(select(DutySession).where(DutySession.organization_id == actor.organization_id,
            DutySession.driver_id == actor.driver_id, DutySession.ended_at.is_(None)))
        if current: raise HTTPException(409, 'Driver is already On Duty.')
        if data.location_permission != 'GRANTED': raise HTTPException(409, 'Location permission is required to start duty.')
        row = DutySession(organization_id=actor.organization_id, driver_id=actor.driver_id, started_at=now())
        driver.last_seen_at, driver.location_permission = now(), data.location_permission
        db.add(row); db.flush(); audit(db, actor, 'duty.started', row.id, actor.organization_id)
        return jsonable_encoder(dump(row))
    return command(db, actor, key, 'duty-start', data.model_dump(), run)


def end_duty(db, actor, identity, data, key):
    def run():
        record(db, Driver, actor, actor.driver_id, True)
        row = record(db, DutySession, actor, identity, True); version(row, data.version)
        if row.driver_id != actor.driver_id: raise HTTPException(404, 'Duty session not found.')
        if row.ended_at: raise HTTPException(409, 'Duty session is already closed.')
        if data.ended_at < row.started_at or data.ended_at > now(): raise HTTPException(422, 'Invalid local End Duty time.')
        row.ended_at = data.ended_at
        changed(db, actor, row, 'duty.ended')
        return jsonable_encoder(dump(row))
    return command(db, actor, key, 'duty-end:' + str(identity), data.model_dump(mode='json'), run)


def telemetry(db, actor, data, key):
    def run():
        driver = record(db, Driver, actor, actor.driver_id, True)
        duty = record(db, DutySession, actor, data.duty_id, True)
        if duty.driver_id != actor.driver_id: raise HTTPException(404, 'Duty session not found.')
        if data.location_permission != 'GRANTED': raise HTTPException(422, 'Coordinates require granted location permission.')
        if data.captured_at < duty.started_at or data.captured_at > (duty.ended_at or now()):
            raise HTTPException(409, 'Location was captured outside the driver duty session.')
        row = Location(organization_id=actor.organization_id, driver_id=actor.driver_id, duty_id=duty.id,
            captured_at=data.captured_at, data=data.model_dump(mode='json'))
        db.add(row); driver.last_seen_at, driver.location_permission = now(), data.location_permission
        db.flush()
        return {'id': str(row.id), 'captured_at': row.captured_at.isoformat()}
    return command(db, actor, key, 'telemetry', data.model_dump(mode='json'), run)


def start_route(db, actor, identity, data, key):
    def run():
        company_lock(db, actor)
        route = own_route(db, actor, identity, True); route_revision(route, data)
        if route.status != 'PLANNED': raise HTTPException(409, 'Route must be planned before starting.')
        duty = db.scalar(select(DutySession).where(DutySession.organization_id == actor.organization_id,
            DutySession.driver_id == actor.driver_id, DutySession.ended_at.is_(None)))
        if not duty: raise HTTPException(409, 'Go On Duty before starting a Route.')
        from .models import Vehicle
        from .dispatch import validate_candidate
        driver = record(db, Driver, actor, actor.driver_id)
        vehicle = record(db, Vehicle, actor, route.vehicle_id)
        orders = list(db.scalars(select(Order).where(Order.organization_id == actor.organization_id, Order.route_id == route.id)))
        actual_start = max(now(), route.planned_at)
        shift_end = validate_candidate(db, actor, orders, driver, vehicle, actual_start)
        delay = actual_start - route.planned_at
        stops = {s['id']:s for order in orders for s in order.facts['stops']}
        for visit in route.plan['stops']:
            from datetime import datetime
            stop = stops[visit['stop_id']]
            arrival = datetime.fromisoformat(visit['planned_at']) + delay
            if stop.get('window_end') and arrival > datetime.fromisoformat(stop['window_end']):
                raise HTTPException(409,'Route is too late for a delivery window; request dispatcher replanning.')
            if shift_end and arrival + timedelta(minutes=stop['service_minutes']) > shift_end:
                raise HTTPException(409,'Route would exceed the driver shift.')
        route.status, route.locked, route.started_at = 'IN_PROGRESS', True, now()
        for order in orders:
            order.status = 'IN_PROGRESS'; changed(db, actor, order, 'order.started')
        changed(db, actor, route, 'route.started')
        return route_view(db, actor, route)
    return command(db, actor, key, 'route-start:' + str(identity), data.model_dump(), run)


def upload_evidence(db, actor, stop_id, data, key):
    def run():
        stop = record(db, OrderStop, actor, stop_id)
        order = record(db, Order, actor, stop.order_id)
        if not order.route_id: raise HTTPException(409, 'Order is not assigned.')
        route = own_route(db, actor, order.route_id, True)
        if route.status != 'IN_PROGRESS' or stop.kind != 'DROPOFF': raise HTTPException(409, 'Evidence requires an executing delivery visit.')
        visit = db.scalar(select(RouteStop).where(RouteStop.route_id == route.id,RouteStop.stop_id == stop.id))
        if not visit or visit.arrived_at is None: raise HTTPException(409,'Record arrival before delivery evidence.')
        if data.captured_at < visit.arrived_at or data.captured_at > now(): raise HTTPException(422, 'Evidence capture time is outside this visit.')
        content = base64.b64decode(data.content_base64)
        row = Evidence(organization_id=actor.organization_id, stop_id=stop.id, driver_id=actor.driver_id,
            kind=data.kind, captured_at=data.captured_at, media_type=data.media_type,
            content=data.content_base64, digest=hashlib.sha256(content).hexdigest())
        db.add(row); db.flush(); audit(db, actor, 'evidence.accepted', row.id, actor.organization_id)
        return {'id': str(row.id), 'kind': row.kind, 'digest': row.digest}
    # Fingerprint content without persisting raw media a second time in the command result.
    return command(db, actor, key, 'evidence:' + str(stop_id), data.model_dump(mode='json'), run)


def validate_pod(db, actor, stop, route, data):
    evidence = [record(db, Evidence, actor, identity) for identity in data.evidence_ids]
    if any(e.stop_id != stop.id or e.driver_id != actor.driver_id or e.captured_at < route.started_at for e in evidence):
        raise HTTPException(422, 'Evidence must belong to this delivery and assignment.')
    kinds = {e.kind for e in evidence}
    if data.unattended:
        if not stop.data['unattended_allowed'] or not data.safe_placement or 'PHOTO' not in kinds:
            raise HTTPException(409, 'Unattended delivery requires permission, safe placement and a photo.')
    elif not data.recipient_name or 'SIGNATURE' not in kinds:
        raise HTTPException(409, 'Recipient name and signature are required.')
    if stop.data['photo_required'] and 'PHOTO' not in kinds: raise HTTPException(409, 'A delivery photo is required.')


def complete_stop(db, actor, identity, visit_id, data, key):
    def run():
        company_lock(db, actor)
        route = own_route(db, actor, identity, True); route_revision(route, data)
        if route.status != 'IN_PROGRESS': raise HTTPException(409, 'Route is not executing.')
        visit = record(db, RouteStop, actor, visit_id, True)
        if visit.route_id != identity: raise HTTPException(404, 'Visit not found.')
        pending = db.scalar(select(RouteStop).where(RouteStop.organization_id == actor.organization_id,
            RouteStop.route_id == identity, RouteStop.status == 'PENDING').order_by(RouteStop.position).limit(1))
        if not pending or pending.id != visit.id: raise HTTPException(409, 'Complete the current visit before moving to the next.')
        if visit.arrived_at is None: raise HTTPException(409,'Record arrival before confirming cargo.')
        if data.captured_at < visit.arrived_at or data.captured_at > now(): raise HTTPException(422, 'Invalid action capture time.')
        previous = db.scalar(select(RouteStop).where(RouteStop.route_id == identity, RouteStop.status == 'COMPLETED').order_by(RouteStop.position.desc()).limit(1))
        if previous and data.captured_at < previous.completed_at: raise HTTPException(409, 'Action capture time precedes its dependency.')
        stop = record(db, OrderStop, actor, visit.stop_id)
        order = record(db, Order, actor, stop.order_id, True)
        link = 'pickup_id' if stop.kind == 'PICKUP' else 'delivery_id'
        expected = {UUID(i['id']): i['quantity'] for i in order.facts['items'] if i[link] == str(stop.id)}
        if data.quantities != expected: raise HTTPException(409, 'Confirm all linked quantities; report short or failed loads as an issue.')
        if stop.kind == 'DROPOFF': validate_pod(db, actor, stop, route, data)
        visit.status, visit.completed_at = 'COMPLETED', data.captured_at
        visit.movements = data.model_dump(mode='json')
        changed(db, actor, visit, 'stop.completed')
        unfinished = db.scalar(select(RouteStop.id).join(OrderStop, OrderStop.id == RouteStop.stop_id).where(
            RouteStop.organization_id == actor.organization_id, RouteStop.route_id == identity,
            OrderStop.order_id == order.id, RouteStop.status != 'COMPLETED'))
        if not unfinished:
            unresolved = db.scalar(select(Issue.id).where(Issue.organization_id == actor.organization_id, Issue.order_id == order.id, Issue.resolved.is_(False)))
            if not unresolved:
                order.status, order.completed_at = 'COMPLETED', data.captured_at
                changed(db, actor, order, 'order.completed')
        changed(db, actor, route, 'route.progress')
        return route_view(db, actor, route)
    return command(db, actor, key, 'stop:' + str(visit_id), data.model_dump(mode='json'), run)


def finish_route(db, actor, identity, data, key):
    def run():
        route = own_route(db, actor, identity, True); route_revision(route, data)
        if route.status != 'IN_PROGRESS': raise HTTPException(409, 'Route is not executing.')
        remaining = db.scalar(select(Order.id).where(Order.organization_id == actor.organization_id,
            Order.route_id == route.id, Order.status.not_in(['COMPLETED','INVOICED'])))
        if remaining: raise HTTPException(409, 'All Orders must have verified completion and resolved issues.')
        route.status, route.completed_at = 'COMPLETED', now()
        changed(db, actor, route, 'route.completed')
        return route_view(db, actor, route)
    return command(db, actor, key, 'route-finish:' + str(identity), data.model_dump(), run)


def close_duty_on_logout(db, user, actor=None):
    if not user.driver_id: return
    driver = record(db,Driver,user,user.driver_id,True)
    duty = db.scalar(select(DutySession).where(DutySession.organization_id == user.organization_id,
        DutySession.driver_id == user.driver_id,DutySession.ended_at.is_(None)).with_for_update())
    if duty:
        duty.ended_at = now()
        changed(db,actor or user,duty,'duty.session_ended')


def arrive_stop(db, actor, identity, visit_id, data, key):
    def run():
        route = own_route(db,actor,identity,True); route_revision(route,data)
        if route.status != 'IN_PROGRESS': raise HTTPException(409,'Route is not executing.')
        visit = record(db,RouteStop,actor,visit_id,True)
        if visit.route_id != identity: raise HTTPException(404,'Visit not found.')
        current = db.scalar(select(RouteStop).where(RouteStop.organization_id == actor.organization_id,RouteStop.route_id == identity,
            RouteStop.status == 'PENDING').order_by(RouteStop.position).limit(1))
        if not current or current.id != visit.id: raise HTTPException(409,'Arrive at the current visit first.')
        if visit.arrived_at: raise HTTPException(409,'Arrival is already recorded.')
        if data.captured_at < route.started_at or data.captured_at > now(): raise HTTPException(422,'Invalid arrival capture time.')
        previous = db.scalar(select(RouteStop).where(RouteStop.organization_id == actor.organization_id,RouteStop.route_id == identity,
            RouteStop.status == 'COMPLETED').order_by(RouteStop.position.desc()).limit(1))
        if previous and data.captured_at < previous.completed_at: raise HTTPException(409,'Arrival precedes the previous visit.')
        visit.arrived_at = data.captured_at
        changed(db,actor,visit,'stop.arrived');changed(db,actor,route,'route.progress')
        return route_view(db,actor,route)
    return command(db,actor,key,'arrive:' + str(visit_id),data.model_dump(mode='json'),run)
