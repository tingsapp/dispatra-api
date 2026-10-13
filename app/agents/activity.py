"""Dispatcher projection of committed company events and company-scoped agent steps."""
from contextlib import contextmanager
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import and_, or_, select, text
from app.database import context
from app.models import User, now
from app.events.models import Event
from app.operations.models import Order, Settings
from .models import AgentLog, AgentRun

# Only public operational milestones enter this feed. Messages, GPS, credentials,
# provider payloads, private audits and global/platform logs never enter it.
LABELS = {
    'order.created': 'Order created', 'order.updated': 'Order details updated',
    'order.assigned': 'Driver assigned', 'order.unassigned': 'Order awaiting assignment',
    'order.started': 'Order in progress', 'order.completed': 'Order completed',
    'order.cancelled': 'Order cancelled', 'order.completion_held': 'Completion needs review',
    'route.created': 'Route prepared', 'route.optimized': 'Route optimized',
    'route.started': 'Route started', 'route.released': 'Route released',
    'route.completed': 'Route completed', 'stop.arrived': 'Arrival recorded',
    'stop.completed': 'Stop completed', 'evidence.accepted': 'Delivery evidence accepted',
    'issue.reported': 'Operational issue reported', 'issue.resolved': 'Operational issue resolved',
    'intake.received': 'Email received', 'intake.order_created': 'Email converted to order',
    'intake.needs_review': 'Email order needs review', 'intake.unknown_sender': 'Email sender needs review',
    'intake.failed': 'Email processing failed', 'intake.discarded': 'Email draft discarded',
    'intake.retried': 'Email processing retry requested',
    'intake.shipper_linked': 'Email sender linked to Shipper',
    'mailbox.failed': 'Order mailbox needs attention', 'dispatch.approved': 'Dispatch suggestion approved',
    'dispatch.auto_assigned': 'Auto dispatch completed', 'dispatch.no_candidate': 'Assignment needs review',
    'watch.detected': 'Order risk detected', 'watch.escalated': 'Order risk escalated',
    'watch.question_created': 'Driver progress update requested', 'watch.driver_replied': 'Driver progress update received',
    'watch.update_requested': 'Driver progress follow-up requested', 'watch.resolved': 'Order risk closed',
    'watch.acknowledged': 'Order risk acknowledged',
}
WARNINGS = {'order.completion_held', 'issue.reported', 'intake.needs_review', 'intake.unknown_sender',
    'intake.failed', 'mailbox.failed', 'dispatch.no_candidate', 'watch.detected', 'watch.escalated'}


@contextmanager
def _agent_projection(db):
    # Existing owner-only RLS remains intact. This authenticated read use case
    # selects an explicit company projection and restores every context setting.
    db.flush()
    saved = db.execute(text("SELECT current_setting('app.organization_id',true), current_setting('app.platform',true), current_setting('app.shipper_id',true), current_setting('app.driver_id',true)")).one()
    with db.no_autoflush:
        context(db, platform=True)
        try:
            yield
        finally:
            if db.is_active:
                context(db, saved[0] or None, saved[1] == 'true', saved[2] or None, saved[3] or None)


def _older(query, model, source, anchor):
    if anchor is None:
        return query
    timestamp, anchor_source, identity = anchor
    same_time = model.created_at == timestamp
    if source < anchor_source:
        return query.where(model.created_at <= timestamp)
    if source > anchor_source:
        return query.where(model.created_at < timestamp)
    return query.where(or_(model.created_at < timestamp, and_(same_time, model.id < identity)))


def _event_agent(row):
    prefix = row['type'].split('.')[0]
    if prefix in {'intake', 'mailbox'}: return 'ORDER'
    if prefix == 'dispatch': return 'DISPATCH'
    if prefix == 'watch': return 'OPERATIONAL'
    if row['actor_type'] == 'AGENT' and row['type'] == 'order.created': return 'ORDER'
    if row['actor_type'] == 'AGENT' and row['type'] == 'order.assigned': return 'DISPATCH'
    return None


def page(db, user, before=None, limit=30):
    if user.role != 'DISPATCHER' or not user.organization_id:
        raise HTTPException(403, 'Dispatcher access required.')
    company_id = user.organization_id
    agents = select(AgentLog.id, AgentLog.created_at, AgentLog.level, AgentLog.message, AgentRun.agent).join(AgentRun, AgentRun.id == AgentLog.run_id).where(AgentLog.organization_id == company_id)
    events = select(Event.id, Event.created_at, Event.type, Event.actor_type, User.role.label('actor_role'), Order.id.label('order_id'), Order.number.label('order_number')).outerjoin(User, and_(User.id == Event.actor_id, User.organization_id == company_id)).outerjoin(Order, and_(Order.id == Event.order_id, Order.organization_id == company_id)).where(Event.organization_id == company_id, Event.dispatchers.is_(True), Event.type.in_(LABELS))
    anchor = None
    if before:
        try:
            source, identity = before.split(':', 1)
            identity = UUID(identity)
            if source not in {'AGENT', 'EVENT'}: raise ValueError()
        except (ValueError, AttributeError):
            raise HTTPException(422, 'Invalid activity cursor.')
        if source == 'AGENT':
            with _agent_projection(db):
                row = db.execute(agents.where(AgentLog.id == identity)).mappings().first()
        else:
            row = db.execute(events.where(Event.id == identity)).mappings().first()
        if row is None: raise HTTPException(404, 'Activity entry not found.')
        anchor = row['created_at'], source, identity
    with _agent_projection(db):
        agent_rows = db.execute(_older(agents, AgentLog, 'AGENT', anchor).order_by(AgentLog.created_at.desc(), AgentLog.id.desc()).limit(limit + 1)).mappings().all()
    event_rows = db.execute(_older(events, Event, 'EVENT', anchor).order_by(Event.created_at.desc(), Event.id.desc()).limit(limit + 1)).mappings().all()
    items = [dict(id=f"AGENT:{r['id']}", source='AGENT', agent=r['agent'], actor='AGENT', kind='agent.step', title=r['message'], level=r['level'], created_at=r['created_at'], order_id=None, order_number=None) for r in agent_rows]
    for row in event_rows:
        agent = _event_agent(row)
        actor = 'AGENT' if row['actor_type'] == 'AGENT' else 'SYSTEM' if row['actor_type'] == 'SYSTEM' else row['actor_role'] if row['actor_role'] in {'DRIVER', 'SHIPPER', 'DISPATCHER'} else 'UNKNOWN'
        items.append(dict(id=f"EVENT:{row['id']}", source='EVENT', agent=agent, actor=actor, kind=row['type'], title=LABELS[row['type']], level='WARNING' if row['type'] in WARNINGS else 'INFO', created_at=row['created_at'], order_id=row['order_id'], order_number=row['order_number']))
    items.sort(key=lambda item: (item['created_at'], item['id']), reverse=True)
    settings = db.scalar(select(Settings.data).where(Settings.organization_id == company_id)) or {}
    return dict(items=items[:limit], next_before=items[limit - 1]['id'] if len(items) > limit else None, observed_at=now(), time_zone=settings.get('time_zone'))
