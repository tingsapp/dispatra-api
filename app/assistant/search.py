"""Explicit tenant-filtered, bounded search projections; no raw operational JSON leaves this module."""
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import select, func, or_, exists
from app.operations.models import Order, OrderStop, Driver, Vehicle, Shipper, Settings, Route
from .schemas import AssistantReference

LIMIT = 8
STATUS_LABELS = {'NEW': 'New', 'ASSIGNED': 'Assigned', 'IN_PROGRESS': 'In progress', 'COMPLETED': 'Completed', 'CANCELLED': 'Cancelled'}


def company_zone(db, org):
    settings = db.scalar(select(Settings.data).where(Settings.organization_id == org)) or {}
    try: return ZoneInfo(settings.get('time_zone') or 'UTC')
    except (ZoneInfoNotFoundError, ValueError, TypeError): return ZoneInfo('UTC')


def pattern(query):
    return '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def overview(db, org):
    statuses = dict(db.execute(select(Order.status, func.count()).where(Order.organization_id == org).group_by(Order.status)).all())
    return {'time_zone': str(company_zone(db, org)), 'orders_by_status': statuses, 'total_orders': sum(statuses.values()),
        'registered_drivers': db.scalar(select(func.count()).select_from(Driver).where(Driver.organization_id == org, Driver.archived_at.is_(None))),
        'registered_vehicles': db.scalar(select(func.count()).select_from(Vehicle).where(Vehicle.organization_id == org, Vehicle.archived_at.is_(None)))}


def collect(db, org, plan):
    term = plan.query.strip()
    like = pattern(term)
    zone = company_zone(db, org)
    references, counts = [], {}
    routes = None
    estimates = None
    def current_estimates():
        nonlocal routes, estimates
        if estimates is None:
            from app.order_watch.estimates import collect as collect_estimates
            from app.models import now
            routes = list(db.scalars(select(Route).where(Route.organization_id == org, Route.status == 'IN_PROGRESS')))
            estimates = collect_estimates(db, org, routes, now())
        return estimates

    def timing(estimate, stop_ids=None):
        predictions = [p for p in estimate['stops'] if stop_ids is None or p['stop_id'] in stop_ids] if estimate else []
        if not predictions: return ' · No current qualified traffic ETA is available.'
        text = ''.join(f' · Stop {p["stop_id"]} ETA {p["eta"]}; traffic delay {round(p["traffic_added_seconds"] / 60, 1)} min' for p in predictions[:3])
        text += f'; {estimate["source"]}, observed {estimate["observed_at"]}'
        if len(predictions) > 3: text += ' · Additional stop estimates omitted; open Monitor.'
        return text

    kinds = set(plan.kinds) or {'ORDER', 'DRIVER', 'VEHICLE', 'SHIPPER'}
    queries = []
    if 'ORDER' in kinds:
        q = select(Order).where(Order.organization_id == org)
        if plan.order_statuses: q = q.where(Order.status.in_(plan.order_statuses))
        if term:
            stops = exists(select(OrderStop.id).where(OrderStop.organization_id == org, OrderStop.order_id == Order.id,
                or_(OrderStop.data['address']['text'].astext.ilike(like, escape='\\'), OrderStop.data['contact_name'].astext.ilike(like, escape='\\'))))
            q = q.where(or_(Order.number.ilike(like, escape='\\'), Order.booking['shipper']['name'].astext.ilike(like, escape='\\'),
                Order.facts['external_reference'].astext.ilike(like, escape='\\'), stops))
        queries.append(('ORDER', Order, q))
    if 'DRIVER' in kinds:
        q = select(Driver).where(Driver.organization_id == org, Driver.archived_at.is_(None))
        if term:
            fields = [f.ilike(like, escape='\\') for f in (Driver.name, Driver.number, Driver.email, Driver.phone)]
            digits = re.sub(r'\D', '', term)
            if len(digits) >= 4: fields.append(func.regexp_replace(Driver.phone, '[^0-9]', '', 'g').contains(digits, autoescape=True))
            q = q.where(or_(*fields))
        queries.append(('DRIVER', Driver, q))
    if 'VEHICLE' in kinds:
        q = select(Vehicle).where(Vehicle.organization_id == org, Vehicle.archived_at.is_(None))
        if term: q = q.where(or_(Vehicle.number.ilike(like, escape='\\'), Vehicle.plate.ilike(like, escape='\\'), Vehicle.data['description'].astext.ilike(like, escape='\\')))
        queries.append(('VEHICLE', Vehicle, q))
    if 'SHIPPER' in kinds:
        q = select(Shipper).where(Shipper.organization_id == org, Shipper.archived_at.is_(None))
        if term: q = q.where(or_(*[f.ilike(like, escape='\\') for f in (Shipper.name, Shipper.number, Shipper.company_name, Shipper.address, Shipper.phone, Shipper.email)]))
        queries.append(('SHIPPER', Shipper, q))
    for kind, model, query in queries:
        counts[kind] = db.scalar(select(func.count()).select_from(query.subquery()))
        if not plan.include_records: continue
        for row in db.scalars(query.order_by(model.updated_at.desc(), model.id).limit(LIMIT)):
            identity = str(row.id)
            ref = AssistantReference(key=f'{kind}:{identity}', kind=kind, id=identity, label='', detail='')
            if kind == 'ORDER':
                ref.label, ref.order_number = row.number, row.number
                stops = db.scalars(select(OrderStop).where(OrderStop.organization_id == org, OrderStop.order_id == row.id).order_by(OrderStop.created_at, OrderStop.id)).all()
                addresses = [f'{"Pickup" if stop.kind == "PICKUP" else "Drop-off"}: {stop.data.get("address", {}).get("text", "")}' + (f' (Contact: {stop.data["contact_name"]})' if stop.data.get('contact_name') else '') for stop in stops]
                ref.detail = f'{STATUS_LABELS[row.status]} · {row.booking.get("shipper", {}).get("name", "")} · {row.scheduled_at.astimezone(zone).strftime("%b %d, %Y %H:%M %Z")} · ' + (' → '.join(addresses)[:1000] + (' [Additional stop details omitted; open the order.]' if len(' → '.join(addresses)) > 1000 else ''))
                if row.route_id and row.status == 'IN_PROGRESS':
                    ref.detail += timing(current_estimates().get(row.route_id), {str(stop.id) for stop in stops})
            elif kind == 'DRIVER':
                ref.label, ref.driver_id = row.name, identity
                ref.detail = f'{row.number} · {row.phone} · {row.email} · {"Profile active" if row.active else "Profile inactive"}'
                vehicle = db.scalar(select(Vehicle).where(Vehicle.organization_id == org, Vehicle.id == row.vehicle_id, Vehicle.archived_at.is_(None))) if row.vehicle_id else None
                ref.detail += f' · Vehicle: {vehicle.number} ({vehicle.plate})' if vehicle else ' · No registered vehicle'
                values = current_estimates()
                active_route = next((route for route in routes if route.driver_id == row.id), None)
                if active_route: ref.detail += timing(values.get(active_route.id))
            elif kind == 'VEHICLE':
                ref.label, ref.detail = row.number, f'{row.plate} · {"Profile active" if row.active else "Profile inactive"}'
                driver = db.execute(select(Driver.id, Driver.name).where(Driver.organization_id == org, Driver.vehicle_id == row.id, Driver.archived_at.is_(None)).order_by(Driver.id).limit(1)).first()
                ref.driver_id = str(driver.id) if driver else None
                ref.detail += f' · Attached driver: {driver.name}' if driver else ' · No attached driver'
            else:
                ref.label, ref.detail = row.name, f'{row.number} · {row.status} · {row.address} · {row.phone} · {row.email}'
            references.append(ref)
    return references, counts
