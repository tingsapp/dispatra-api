"""Dispatch agent use cases.

A suggestion ranks the feasible candidates for a dispatcher to approve. In AUTO mode the worker assigns the best
candidate through `operations.dispatch.assign` as a system dispatcher audited as `AGENT`; the command re-checks every
hard rule with road travel and the next candidate is tried when it refuses. Worker AI calls run between short
transactions, and any provider failure falls back to the rule ranking instead of blocking dispatch.
"""
import os
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import context, engine
from app.events import publish
from app.models import now
from app.services import audit
from app.operations import dispatch
from app.operations.common import command, company_lock, record, settings
from app.operations.models import Order, Route, Settings
from app.operations.schemas import Assignment
from . import rank
from .candidates import blocker, evaluate
from .models import DispatchDecision
from .schemas import DecisionView

SYSTEM = SimpleNamespace(id=None, role=None)
HORIZON = timedelta(hours=float(os.environ.get('DISPATCH_AGENT_HORIZON_HOURS') or '12'))
RETRY = timedelta(minutes=5)
REUSE = timedelta(minutes=2)
ATTEMPTS = 3
RULES_SUMMARY = 'Ranked by rules: extra driving time, current workload, vehicle match and the Shipper\'s requested driver.'


class AgentDispatcher:
    """System principal for AUTO mode: company dispatcher permissions, no user account, audited as `AGENT`."""
    actor_type, role, id, shipper_id, driver_id = 'AGENT', 'DISPATCHER', None, None, None

    def __init__(self, organization_id): self.organization_id = organization_id


def brief(order):
    """What the ranking model may see: no addresses, contacts, prices or free-text notes."""
    stops, items = order.facts['stops'], order.facts['items']
    return {'scheduled_pickup': order.scheduled_at.isoformat(), 'pickups': sum(s['kind'] == 'PICKUP' for s in stops),
        'deliveries': sum(s['kind'] == 'DROPOFF' for s in stops),
        'delivery_deadlines': [s['window_end'] for s in stops if s['kind'] == 'DROPOFF' and s.get('window_end')],
        'pieces': sum(int(i['quantity']) for i in items), 'total_weight_kg': round(sum(float(i['weight_kg']) * int(i['quantity']) for i in items), 1),
        'dangerous_goods': any(i.get('dangerous_goods') for i in items), 'fragile': any(i.get('fragile') for i in items),
        'service': ((order.pricing.get('context') or {}).get('service') or {}).get('name')}


def ranked(order_brief, candidates):
    """Return (ordered candidates, ranked_by, summary, error_code)."""
    if not candidates: return [], 'RULES', '', None
    try:
        ordered, summary = rank.rank(order_brief, candidates)
        return ordered, 'AI', summary, None
    except rank.Unavailable as error:
        return [{**c, 'reason': c['facts'][0]} for c in candidates], 'RULES', RULES_SUMMARY, error.code


def latest(db, organization_id, order_id):
    return db.scalar(select(DispatchDecision).where(DispatchDecision.organization_id == organization_id, DispatchDecision.order_id == order_id)
        .order_by(DispatchDecision.created_at.desc(), DispatchDecision.id.desc()).limit(1))


def save(db, organization_id, order_id, order_version, mode, status, ordered, excluded, ranked_by, summary, error_code, driver_id=None, decided_by=None):
    row = DispatchDecision(organization_id=organization_id, order_id=order_id, order_version=order_version, mode=mode, status=status,
        ranked_by=ranked_by, candidates=ordered, excluded=excluded, summary=summary, error_code=error_code, driver_id=driver_id, decided_by=decided_by, version=1)
    db.add(row)
    db.flush()
    return row


def view(row):
    return DecisionView.model_validate(row).model_dump(mode='json')


def suggest(db, user, order_id, refresh=False):
    order = record(db, Order, user, order_id)
    reason = blocker(order)
    if reason: raise HTTPException(409, reason)
    previous = latest(db, user.organization_id, order.id)
    if not refresh and previous and previous.order_version == order.version and previous.status == 'SUGGESTED' and now() - previous.created_at < REUSE:
        return view(previous)
    candidates, excluded = evaluate(db, user, order)
    ordered, ranked_by, summary, error = ranked(brief(order), candidates)
    row = save(db, user.organization_id, order.id, order.version, 'MANUAL', 'SUGGESTED' if ordered else 'NO_CANDIDATE',
        ordered, excluded, ranked_by, summary, error, decided_by=user.id)
    return view(row)


def history(db, user, order_id, limit=20):
    record(db, Order, user, order_id)
    rows = db.scalars(select(DispatchDecision).where(DispatchDecision.organization_id == user.organization_id, DispatchDecision.order_id == order_id)
        .order_by(DispatchDecision.created_at.desc(), DispatchDecision.id.desc()).limit(limit))
    return [view(row) for row in rows]


def assignment(db, actor, order, candidate):
    """The assignment command body for a stored candidate, against the Route as it is now."""
    route = record(db, Route, actor, candidate['route_id']) if candidate['route_id'] else None
    return Assignment(version=order.version, driver_id=candidate['driver_id'], vehicle_id=candidate['vehicle_id'],
        route_id=route.id if route else None, route_version=route.version if route else None,
        planned_at=route.planned_at if route else candidate['planned_at'])


def approve(db, user, identity, data, key):
    """The dispatcher accepts one recommended candidate; the shared assignment command re-checks and saves it."""
    def run():
        row = record(db, DispatchDecision, user, identity, True)
        if row.version != data.version: raise HTTPException(409, 'Recommendation changed. Reload before continuing.')
        if row.status != 'SUGGESTED': raise HTTPException(409, 'This recommendation is no longer open.')
        candidate = next((c for c in row.candidates if c['driver_id'] == str(data.driver_id)), None)
        if candidate is None: raise HTTPException(422, 'Choose one of the recommended drivers.')
        order = record(db, Order, user, row.order_id)
        if order.version != row.order_version: raise HTTPException(409, 'The Order changed since this recommendation. Ask for a new one.')
        result = dispatch.assign(db, user, order.id, assignment(db, user, order, candidate), f'dispatch-{row.id}')
        row.status, row.driver_id, row.decided_by, row.version = 'ASSIGNED', data.driver_id, user.id, row.version + 1
        db.flush()
        audit(db, user, 'dispatch.approved', row.id, user.organization_id)
        return result
    return command(db, user, key, 'dispatch-approve', {'id': str(identity), **data.model_dump(mode='json')}, run)


def no_driver(db, organization_id, orders):
    """Needs Attention for unassigned Orders whose latest AUTO evaluation of the current version found no driver."""
    waiting = {o.id: o for o in orders if o.status == 'NEW'}
    if not waiting: return {}
    rows = db.scalars(select(DispatchDecision).where(DispatchDecision.organization_id == organization_id, DispatchDecision.order_id.in_(waiting))
        .order_by(DispatchDecision.order_id, DispatchDecision.created_at.desc(), DispatchDecision.id.desc()).distinct(DispatchDecision.order_id))
    result = {}
    for row in rows:
        if row.status != 'NO_CANDIDATE' or row.mode != 'AUTO' or row.order_version != waiting[row.order_id].version: continue
        reasons = Counter(item['reason'] for item in row.excluded)
        result[row.order_id] = f'Auto dispatch found no driver: {reasons.most_common(1)[0][0]}' if reasons else 'Auto dispatch found no active driver.'
    return result


# AUTO mode worker -----------------------------------------------------------------------------------------------

def auto_companies():
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        return db.scalars(select(Settings.organization_id).where(Settings.data['dispatch_mode'].astext == 'AUTO')).all()


def due(organization_id, limit=20):
    """Priced, unassigned Orders inside the horizon whose current version was not evaluated in the last few minutes."""
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        orders = db.scalars(select(Order).where(Order.organization_id == organization_id, Order.status == 'NEW',
            Order.pricing['status'].astext == 'PRICED', Order.scheduled_at <= now() + HORIZON).order_by(Order.scheduled_at).limit(limit * 3)).all()
        result = []
        for order in orders:
            previous = latest(db, organization_id, order.id)
            if previous and previous.order_version == order.version and (previous.status == 'ASSIGNED' or now() - previous.created_at < RETRY): continue
            result.append(order.id)
        return result[:limit]


def dispatch_order(organization_id, order_id):
    """Evaluate, rank and assign one Order. Returns the decision status, or None when nothing was recorded."""
    agent = AgentDispatcher(organization_id)
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        if settings(db, agent)[1].dispatch_mode != 'AUTO': return None
        order = db.scalar(select(Order).where(Order.organization_id == organization_id, Order.id == order_id))
        if order is None or blocker(order): return None
        previous = latest(db, organization_id, order.id)
        repeat = previous is not None and previous.order_version == order.version and previous.status == 'NO_CANDIDATE'
        version, candidates, excluded = order.version, *evaluate(db, agent, order)
        order_brief = brief(order)
    ordered, ranked_by, summary, error = ranked(order_brief, candidates)
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        # Ranking runs outside the transaction. Serialize with settings writes so
        # switching to MANUAL takes effect before an automatic assignment saves.
        company_lock(db, agent)
        if settings(db, agent)[1].dispatch_mode != 'AUTO': return None
        order = db.scalar(select(Order).where(Order.organization_id == organization_id, Order.id == order_id))
        if order is None or order.status != 'NEW' or order.version != version: return None
        refused = []
        for candidate in ordered[:ATTEMPTS]:
            queued = dict(db.info.get(publish.PENDING, {}))
            try:
                with db.begin_nested():
                    dispatch.assign(db, agent, order.id, assignment(db, agent, order, candidate), f'auto-{order.id}-{version}-{candidate["driver_id"]}')
            except HTTPException as failure:
                # A refused attempt leaves no events behind; the next candidate is tried.
                db.info[publish.PENDING] = queued
                refused.append({k: candidate[k] for k in ('driver_id', 'driver_name', 'driver_number')} | {'reason': str(failure.detail)})
                continue
            row = save(db, organization_id, order_id, version, 'AUTO', 'ASSIGNED', ordered, excluded + refused, ranked_by, summary, error, candidate['driver_id'])
            publish.record(db, SYSTEM, 'dispatch.auto_assigned', row.id, organization_id, 'AGENT')
            return 'ASSIGNED'
        tried = {item['driver_id'] for item in refused}
        row = save(db, organization_id, order_id, version, 'AUTO', 'NO_CANDIDATE', [c for c in ordered if c['driver_id'] not in tried],
            excluded + refused, ranked_by, summary, error)
        # Retries re-evaluate every few minutes; dispatchers hear about it once per Order version.
        if not repeat: publish.record(db, SYSTEM, 'dispatch.no_candidate', row.id, organization_id, 'AGENT')
        return 'NO_CANDIDATE'


def dispatch_pending(organization_id, limit=20):
    results = Counter()
    for order_id in due(organization_id, limit): results[dispatch_order(organization_id, order_id)] += 1
    return results
