"""Explicit tenant-filtered, bounded search projections; no raw operational JSON leaves this module."""
import re
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import select, func, or_, and_, exists
from app.operations.models import Order, OrderStop, Driver, Vehicle, Shipper, Settings, Route, DutySession, Location, Issue, Catalog, RateCard
from .schemas import AssistantReference

LIMIT = 8
STATUS_LABELS = {'NEW': 'New', 'ASSIGNED': 'Assigned', 'IN_PROGRESS': 'In progress', 'COMPLETED': 'Completed', 'CANCELLED': 'Cancelled'}
DISPATCH_WORDS = re.compile(r'\b(who|driver|assign|take|availab|eligib|best|closest|nearest|recommend|free|why|can|fit|fits|match)\b', re.I)


def company_settings(db, org):
    return db.scalar(select(Settings.data).where(Settings.organization_id == org)) or {}


def settings_zone(settings):
    try: return ZoneInfo(settings.get('time_zone') or 'UTC')
    except (ZoneInfoNotFoundError, ValueError, TypeError): return ZoneInfo('UTC')


def company_zone(db, org):
    return settings_zone(company_settings(db, org))


def pattern(query):
    return '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def tokens(term):
    """Split multi-word queries into bounded tokens while keeping literal wildcard or single-term searches intact."""
    if not term or '%' in term or '_' in term: return [term] if term else []
    parts = [part for part in re.findall(r'[A-Za-z0-9@#._-]+', term) if len(part) >= 2]
    return parts[:4] if len(parts) >= 2 else [term]


def overview(db, org):
    from app.models import now
    from app.operations.monitor_day import day_bounds
    settings = company_settings(db, org)
    zone = settings_zone(settings)
    observed = now()
    start, end = day_bounds(observed, zone)
    statuses = dict(db.execute(select(Order.status, func.count()).where(Order.organization_id == org, Order.archived_at.is_(None)).group_by(Order.status)).all())
    today_statuses = dict(db.execute(select(Order.status, func.count()).where(
        Order.organization_id == org, Order.archived_at.is_(None),
        Order.scheduled_at >= start, Order.scheduled_at < end, Order.status != 'CANCELLED').group_by(Order.status)).all())
    on_duty = db.scalar(select(func.count(func.distinct(DutySession.driver_id))).where(
        DutySession.organization_id == org, DutySession.ended_at.is_(None))) or 0
    return {'time_zone': str(zone), 'today': observed.astimezone(zone).date().isoformat(),
        'dispatch_mode': 'AUTO' if settings.get('dispatch_mode') == 'AUTO' else 'MANUAL',
        'orders_by_status': statuses, 'total_orders': sum(statuses.values()),
        'today_orders_by_status': today_statuses, 'today_total_orders': sum(today_statuses.values()),
        'registered_drivers': db.scalar(select(func.count()).select_from(Driver).where(Driver.organization_id == org, Driver.archived_at.is_(None))),
        'on_duty_drivers': on_duty,
        'registered_vehicles': db.scalar(select(func.count()).select_from(Vehicle).where(Vehicle.organization_id == org, Vehicle.archived_at.is_(None)))}


def _order_match(org, token):
    clean = re.sub(r'^#+', '', token) or token
    like = pattern(clean)
    stops = exists(select(OrderStop.id).where(OrderStop.organization_id == org, OrderStop.order_id == Order.id,
        or_(OrderStop.data['address']['text'].astext.ilike(like, escape='\\'), OrderStop.data['contact_name'].astext.ilike(like, escape='\\'))))
    assigned_driver = exists(select(Route.id).join(Driver, (Driver.id == Route.driver_id) & (Driver.organization_id == org)).where(
        Route.organization_id == org, Route.id == Order.route_id, or_(Driver.name.ilike(like, escape='\\'), Driver.number.ilike(like, escape='\\'))))
    return or_(Order.number.ilike(like, escape='\\'), Order.booking['shipper']['name'].astext.ilike(like, escape='\\'),
        Order.facts['external_reference'].astext.ilike(like, escape='\\'), stops, assigned_driver)


def _driver_match(token):
    like = pattern(token)
    fields = [f.ilike(like, escape='\\') for f in (Driver.name, Driver.number, Driver.email, Driver.phone)]
    digits = re.sub(r'\D', '', token)
    if len(digits) >= 4: fields.append(func.regexp_replace(Driver.phone, '[^0-9]', '', 'g').contains(digits, autoescape=True))
    return or_(*fields)


def _vehicle_match(token):
    like = pattern(token)
    return or_(Vehicle.number.ilike(like, escape='\\'), Vehicle.plate.ilike(like, escape='\\'),
        Vehicle.data['name'].astext.ilike(like, escape='\\'), Vehicle.data['description'].astext.ilike(like, escape='\\'))


def _shipper_match(token):
    like = pattern(token)
    return or_(*[f.ilike(like, escape='\\') for f in (Shipper.name, Shipper.number, Shipper.company_name, Shipper.address, Shipper.phone, Shipper.email)])


def _price_text(order):
    pricing = order.pricing or {}
    if pricing.get('status') == 'PRICED' and pricing.get('total') is not None:
        return f'{pricing.get("currency", "")} {pricing["total"]}'.strip()
    reason = pricing.get('review_reason')
    return f'Pricing needs review ({reason})' if reason else 'Pricing needs review'


def _stop_text(stop, zone):
    kind = 'Pickup' if stop.kind == 'PICKUP' else 'Drop-off'
    text = f'{kind}: {stop.data.get("address", {}).get("text", "")}'
    if stop.data.get('contact_name'): text += f' (Contact: {stop.data["contact_name"]})'
    if stop.data.get('window_end'):
        try: text += f' (by {datetime.fromisoformat(stop.data["window_end"]).astimezone(zone).strftime("%b %-d, %-I:%M %p")})'
        except ValueError: pass
    return text


def stop_window_problem(order, zone, observed):
    """Plain-language explanation when an Order's pickup or drop-off deadline is already past or before pickup."""
    earliest = max(order.scheduled_at, observed)
    for stop in (order.facts or {}).get('stops', ()):
        if not stop.get('window_end'): continue
        try: end = datetime.fromisoformat(stop['window_end'])
        except ValueError: continue
        if end < earliest:
            kind = 'Pickup' if stop.get('kind') == 'PICKUP' else 'Drop-off'
            end_text = end.astimezone(zone).strftime('%b %-d, %-I:%M %p %Z')
            if end < order.scheduled_at:
                sched_text = order.scheduled_at.astimezone(zone).strftime('%b %-d, %-I:%M %p %Z')
                return f'{kind} deadline ({end_text}) is earlier than the scheduled pickup ({sched_text}). Update the {kind.lower()} window first.'
            return f'{kind} deadline ({end_text}) has already passed. Update the {kind.lower()} window first.'
    return None


def _order_detail(db, org, row, zone, current_estimates, timing):
    stops = db.scalars(select(OrderStop).where(OrderStop.organization_id == org, OrderStop.order_id == row.id).order_by(OrderStop.created_at, OrderStop.id)).all()
    addresses = [_stop_text(stop, zone) for stop in stops]
    assigned_text = 'Unassigned'
    if row.route_id:
        route = db.scalar(select(Route).where(Route.organization_id == org, Route.id == row.route_id))
        driver = db.scalar(select(Driver).where(Driver.organization_id == org, Driver.id == route.driver_id)) if route else None
        vehicle = db.scalar(select(Vehicle).where(Vehicle.organization_id == org, Vehicle.id == route.vehicle_id)) if route else None
        if driver:
            vlabel = f' ({vehicle.data.get("name") or vehicle.number})' if vehicle else ''
            assigned_text = f'Driver: {driver.name}{vlabel}'
    issues = db.scalars(select(Issue.kind).where(Issue.organization_id == org, Issue.order_id == row.id, Issue.resolved.is_(False)).limit(3)).all()
    issue_text = f' · Open issues: {", ".join(i.replace("_", " ").lower() for i in issues)}' if issues else ''
    joined = ' → '.join(addresses)
    stop_part = joined[:900] + (' [Additional stop details omitted; open the order.]' if len(joined) > 900 else '')
    detail = f'{STATUS_LABELS[row.status]} · {assigned_text} · {_price_text(row)} · {row.booking.get("shipper", {}).get("name", "")} · {row.scheduled_at.astimezone(zone).strftime("%b %d, %Y %H:%M %Z")} · {stop_part}{issue_text}'
    if row.route_id and row.status == 'IN_PROGRESS':
        detail += timing(current_estimates().get(row.route_id), {str(stop.id) for stop in stops})
    return detail


def _driver_detail(db, org, row, observed, on_duty_ids, driver_routes, current_estimates, active_routes, timing):
    on_duty = row.id in on_duty_ids
    duty_text = 'On duty' if on_duty else 'Off duty'
    gps_text = ''
    if on_duty:
        point = db.scalar(select(Location.captured_at).join(DutySession, DutySession.id == Location.duty_id).where(
            Location.organization_id == org, Location.driver_id == row.id, DutySession.ended_at.is_(None))
            .order_by(Location.captured_at.desc()).limit(1))
        gps_text = ' · GPS fresh' if point and (observed - point) < timedelta(minutes=2) else (' · GPS stale' if point else ' · No live GPS')
    route = driver_routes.get(row.id)
    if route:
        route_orders = db.scalars(select(Order.number).where(Order.organization_id == org, Order.route_id == route.id,
            Order.archived_at.is_(None), Order.status.in_(['ASSIGNED', 'IN_PROGRESS'])).order_by(Order.number).limit(6)).all()
        state = 'On the road' if route.status == 'IN_PROGRESS' else ('Planned route (locked)' if route.locked else 'Planned route')
        route_text = f' · {state}: {", ".join(route_orders) if route_orders else "no open orders"}'
    else:
        route_text = ' · No active route'
    detail = f'{row.number} · {duty_text}{gps_text}{route_text} · {row.phone} · {row.email} · {"Profile active" if row.active else "Profile inactive"}'
    vehicle = db.scalar(select(Vehicle).where(Vehicle.organization_id == org, Vehicle.id == row.vehicle_id, Vehicle.archived_at.is_(None))) if row.vehicle_id else None
    detail += f' · Vehicle: {vehicle.number} ({vehicle.plate})' if vehicle else ' · No registered vehicle'
    values = current_estimates()
    active_route = next((r for r in active_routes() if r.driver_id == row.id), None)
    if active_route: detail += timing(values.get(active_route.id))
    return detail


def collect(db, org, plan):
    from app.models import now
    term = plan.query.strip()
    parts = tokens(term)
    zone = company_zone(db, org)
    observed = now()
    references, counts = [], {}
    routes = None
    estimates = None
    def current_estimates():
        nonlocal routes, estimates
        if estimates is None:
            from app.order_watch.estimates import collect as collect_estimates
            routes = list(db.scalars(select(Route).where(Route.organization_id == org, Route.status == 'IN_PROGRESS')))
            estimates = collect_estimates(db, org, routes, observed)
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
        q = select(Order).where(Order.organization_id == org, Order.archived_at.is_(None))
        if plan.order_statuses: q = q.where(Order.status.in_(plan.order_statuses))
        if parts: q = q.where(and_(*[_order_match(org, part) for part in parts]))
        queries.append(('ORDER', Order, q))
    if 'DRIVER' in kinds:
        q = select(Driver).where(Driver.organization_id == org, Driver.archived_at.is_(None))
        if parts: q = q.where(and_(*[_driver_match(part) for part in parts]))
        queries.append(('DRIVER', Driver, q))
    if 'VEHICLE' in kinds:
        q = select(Vehicle).where(Vehicle.organization_id == org, Vehicle.archived_at.is_(None))
        if parts: q = q.where(and_(*[_vehicle_match(part) for part in parts]))
        queries.append(('VEHICLE', Vehicle, q))
    if 'SHIPPER' in kinds:
        q = select(Shipper).where(Shipper.organization_id == org, Shipper.archived_at.is_(None))
        if parts: q = q.where(and_(*[_shipper_match(part) for part in parts]))
        queries.append(('SHIPPER', Shipper, q))
    on_duty_ids, driver_routes = set(), {}
    for kind, model, query in queries:
        counts[kind] = db.scalar(select(func.count()).select_from(query.subquery()))
        if not plan.include_records: continue
        rows = list(db.scalars(query.order_by(model.updated_at.desc(), model.id).limit(LIMIT)))
        if kind == 'DRIVER' and rows:
            ids = [row.id for row in rows]
            on_duty_ids = set(db.scalars(select(DutySession.driver_id).where(
                DutySession.organization_id == org, DutySession.driver_id.in_(ids), DutySession.ended_at.is_(None))))
            active = list(db.scalars(select(Route).where(Route.organization_id == org, Route.driver_id.in_(ids), Route.status.in_(['PLANNED', 'IN_PROGRESS']))))
            driver_routes = {r.driver_id: r for r in active}
        for row in rows:
            identity = str(row.id)
            ref = AssistantReference(key=f'{kind}:{identity}', kind=kind, id=identity, label='', detail='')
            if kind == 'ORDER':
                ref.label, ref.order_number = row.number, row.number
                ref.detail = _order_detail(db, org, row, zone, current_estimates, timing)
            elif kind == 'DRIVER':
                ref.label, ref.driver_id = row.name, identity
                ref.detail = _driver_detail(db, org, row, observed, on_duty_ids, driver_routes, current_estimates, lambda: routes or [], timing)
            elif kind == 'VEHICLE':
                avail = (row.data or {}).get('availability')
                avail_text = f' · {avail.capitalize()}' if isinstance(avail, str) and avail else ''
                parking = ((row.data or {}).get('parking_address') or {}).get('city') or ((row.data or {}).get('parking_address') or {}).get('text')
                park_text = f' · Parks: {parking[:60]}' if parking else ''
                ref.label, ref.detail = row.number, f'{row.plate} · {"Profile active" if row.active else "Profile inactive"}{avail_text}{park_text}'
                driver = db.execute(select(Driver.id, Driver.name).where(Driver.organization_id == org, Driver.vehicle_id == row.id, Driver.archived_at.is_(None)).order_by(Driver.id).limit(1)).first()
                ref.driver_id = str(driver.id) if driver else None
                ref.detail += f' · Attached driver: {driver.name}' if driver else ' · No attached driver'
            else:
                ref.label, ref.detail = row.name, f'{row.number} · {row.status} · {row.address} · {row.phone} · {row.email}'
            references.append(ref)
    return references, counts


def _selected_label(db, org, ui_context):
    """Verify optional Monitor selection inside the company and return a compact non-secret summary."""
    if db is None or ui_context is None: return None
    info = {}
    if getattr(ui_context, 'selected_order_id', None):
        row = db.scalar(select(Order).where(Order.organization_id == org, Order.id == ui_context.selected_order_id, Order.archived_at.is_(None)))
        if row: info['selected_order'] = {'order_number': row.number, 'status': STATUS_LABELS.get(row.status, row.status)}
    if getattr(ui_context, 'selected_driver_id', None):
        row = db.scalar(select(Driver).where(Driver.organization_id == org, Driver.id == ui_context.selected_driver_id, Driver.archived_at.is_(None)))
        if row: info['selected_driver'] = {'driver_name': row.name, 'driver_number': row.number}
    return info or None


def selected_hint(db, org, ui_context):
    info = _selected_label(db, org, ui_context)
    if not info: return None
    parts = []
    if 'selected_order' in info: parts.append(f'selected order {info["selected_order"]["order_number"]} ({info["selected_order"]["status"]})')
    if 'selected_driver' in info: parts.append(f'selected driver {info["selected_driver"]["driver_name"]}')
    return 'Currently open on Monitor (hint only): ' + ', '.join(parts)


def _candidate_snapshot(db, actor, refs, question_text, ui_context):
    from types import SimpleNamespace
    from app.models import now
    from . import actions
    if not DISPATCH_WORDS.search(question_text or ''): return None
    order_refs = [ref for ref in refs if ref.kind == 'ORDER']
    target_id = UUID(order_refs[0].id) if len(order_refs) == 1 else getattr(ui_context, 'selected_order_id', None)
    if not target_id: return None
    order = db.scalar(select(Order).where(Order.organization_id == actor.organization_id, Order.id == target_id, Order.archived_at.is_(None)))
    if order is None or order.status != 'NEW': return None
    from app.dispatch.candidates import blocker, evaluate
    block = blocker(order)
    if block:
        return {'order_number': order.number, 'blocker': actions.friendly(block, SimpleNamespace(name='The driver')), 'eligible': [], 'excluded': []}
    cands, excl = evaluate(db, actor, order, road=False)
    window_issue = stop_window_problem(order, company_zone(db, actor.organization_id), now()) if not cands else None
    return {
        'order_number': order.number, 'blocker': window_issue,
        'eligible': [{'driver_name': c['driver_name'], 'vehicle_name': c['vehicle_name'], 'extra_minutes': c['metrics']['extra_minutes'],
            'deadhead_km': c['metrics']['deadhead_km'], 'live_insertion': c['metrics']['live_insertion'], 'summary': c['facts'][:3]} for c in cands[:5]],
        'excluded': [{'driver_name': e['driver_name'], 'reason': actions.friendly(e['reason'], SimpleNamespace(name=e['driver_name']))} for e in excl[:8]],
    }


def workspace_context(db, actor, refs, question_text, observed, ui_context=None):
    """Bounded operational snapshot: today's totals, Needs Attention, waiting requests, config and dispatch candidates."""
    from app.operations.reporting import open_attention
    from app.connectors.models import Connection
    from app.connectors.catalog import BY_ID
    from . import records
    org = actor.organization_id
    briefing = daily_briefing(db, org, observed)
    open_orders, attention = open_attention(db, org, observed)
    issues = records.open_issues(db, org)[:10]
    issue_nums = dict(db.execute(select(Order.id, Order.number).where(Order.organization_id == org,
        Order.id.in_({i.order_id for i in issues}))).all()) if issues else {}
    alerts = [{'order_number': issue_nums.get(i.order_id, ''), 'kind': i.kind.replace('_', ' ').lower(), 'description': i.description[:140]} for i in issues]
    for order_id, rows in attention.items():
        for row in rows:
            if row['hidden'] or len(alerts) >= 15: continue
            alerts.append({'order_number': open_orders[order_id].number, 'kind': row['key'].lower(), 'description': row['description'][:140]})
    emails, tms_rows = records.open_requests(db, org, limit=8)
    requests = ([{'kind': 'email', 'sender': r.from_name or r.from_address, 'subject': (r.subject or '(no subject)')[:80], 'status': r.status.replace('_', ' ').lower()} for r in emails]
        + [{'kind': 'tms', 'external_id': r.external_id, 'status': r.status.replace('_', ' ').lower()} for r in tms_rows])
    catalog = db.execute(select(Catalog.kind, Catalog.code, Catalog.data['name'].astext).where(
        Catalog.organization_id == org, Catalog.active.is_(True))).all()
    cards = db.scalars(select(RateCard.data['name'].astext).where(RateCard.organization_id == org, RateCard.active.is_(True))).all()
    conns = db.execute(select(Connection.provider, Connection.status).where(Connection.organization_id == org)).all()
    snapshot = {
        'today': briefing.model_dump(),
        'needs_attention': {'total_shown': len(alerts), 'items': alerts, 'waiting_requests_count': records.waiting_requests(db, org), 'waiting_requests': requests[:8]},
        'config': {
            'dispatch_mode': briefing.dispatch_mode,
            'services': [name or code for k, code, name in catalog if k == 'SERVICE'],
            'accessorials': [name or code for k, code, name in catalog if k == 'ACCESSORIAL'],
            'vehicle_types': [name or code for k, code, name in catalog if k == 'VEHICLE_TYPE'],
            'package_types': [name or code for k, code, name in catalog if k == 'PACKAGE_TYPE'],
            'rate_cards': list(cards),
            'connectors': [{'name': BY_ID[p].name if p in BY_ID else p, 'status': s.lower()} for p, s in conns],
            'tms_configured': records.company_record(db, org, 'TMS') is not None,
            'mailbox_configured': records.company_record(db, org, 'MAILBOX') is not None,
        },
    }
    if sel := _selected_label(db, org, ui_context): snapshot['selected_on_monitor'] = sel
    if cand := _candidate_snapshot(db, actor, refs, question_text, ui_context): snapshot['dispatch_candidates'] = cand
    return snapshot


def daily_briefing(db, org, observed):
    from app.operations.monitor_day import day_bounds
    from app.order_watch.models import Incident
    from .schemas import DailyBriefing, DriverAssignments
    settings = company_settings(db, org)
    zone = settings_zone(settings)
    local = observed.astimezone(zone)
    start, end = day_bounds(observed, zone)
    # Today's totals include only pickups scheduled for the company-local day.
    picked_today = (Order.scheduled_at >= start) & (Order.scheduled_at < end)
    today = (Order.organization_id == org, Order.archived_at.is_(None), picked_today)
    counts = dict(db.execute(select(Order.status, func.count()).where(*today,
        Order.status != 'CANCELLED').group_by(Order.status)).all())
    incidents = db.scalar(select(func.count()).select_from(Incident)
        .join(Order, (Order.organization_id == org) & (Order.id == Incident.order_id))
        .where(*today, Order.status != 'CANCELLED', Incident.organization_id == org,
            Incident.status.in_(['OPEN', 'ACKNOWLEDGED'])))
    orders = func.count(Order.id)
    drivers = db.execute(select(Driver.name, orders)
        .join(Route, (Route.organization_id == org) & (Route.id == Order.route_id))
        .join(Driver, (Driver.organization_id == org) & (Driver.id == Route.driver_id))
        .where(*today, Order.status.in_(['ASSIGNED', 'IN_PROGRESS', 'COMPLETED']))
        .group_by(Driver.id, Driver.name).order_by(orders.desc(), Driver.name)).all()
    return DailyBriefing(day=local.date().isoformat(), time_zone=str(zone), evening=local.hour >= 18,
        received=sum(counts.values()), unassigned=counts.get('NEW', 0), assigned=counts.get('ASSIGNED', 0),
        in_progress=counts.get('IN_PROGRESS', 0), completed=counts.get('COMPLETED', 0), open_incidents=incidents,
        dispatch_mode='AUTO' if settings.get('dispatch_mode') == 'AUTO' else 'MANUAL',
        assigned_by_driver=[DriverAssignments(driver_name=name, orders=count) for name, count in drivers])
