"""Fresh, tenant-filtered ETA projection shared by Monitor and the read-only assistant."""
from sqlalchemy import select
from pydantic import ValidationError
from app.operations.models import RouteStop, DutySession
from app.operations.schemas import TrafficEstimateView
from .models import RouteEstimate


def qualified(estimate, route, duty, visit, current):
    if not estimate or not route or route.status != 'IN_PROGRESS' or not duty or duty.ended_at is not None: return False
    if not visit or visit.arrived_at is not None or estimate.next_stop_id != visit.stop_id: return False
    if estimate.generation != route.generation or estimate.route_version != route.version: return False
    if estimate.captured_at < duty.started_at or estimate.data.get('duty_id') != str(duty.id): return False
    return (0 <= (current - estimate.captured_at).total_seconds() <= 120
        and 0 <= (current - estimate.observed_at).total_seconds() <= 180)


def collect(db, organization_id, routes, current):
    ids = [r.id for r in routes if r.status == 'IN_PROGRESS']
    if not ids: return {}
    visits = {}
    for visit in db.scalars(select(RouteStop).where(RouteStop.organization_id == organization_id,
            RouteStop.route_id.in_(ids), RouteStop.status != 'COMPLETED').order_by(RouteStop.position)):
        visits.setdefault(visit.route_id, visit)
    duties = {d.driver_id: d for d in db.scalars(select(DutySession).where(
        DutySession.organization_id == organization_id, DutySession.ended_at.is_(None)))}
    estimates = {e.route_id: e for e in db.scalars(select(RouteEstimate).where(
        RouteEstimate.organization_id == organization_id, RouteEstimate.route_id.in_(ids)))}
    result = {}
    for route in routes:
        estimate = estimates.get(route.id)
        if not qualified(estimate, route, duties.get(route.driver_id), visits.get(route.id), current): continue
        try:
            value = TrafficEstimateView.model_validate({**estimate.data,
                'observed_at': estimate.observed_at, 'captured_at': estimate.captured_at})
            result[route.id] = value.model_dump(mode='json')
        except ValidationError: continue
    return result
