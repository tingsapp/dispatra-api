"""Owner-only, read-only view of current work and committed metadata handoffs."""
from datetime import timedelta
from sqlalchemy import and_, func, select
from app.models import Organization
from app.events.models import Event
from app.intake.models import EmailIntake
from app.operations.models import Order, Route, Settings
from app.dispatch.models import DispatchDecision
from .models import AgentLog, AgentRun, TechnicalIssue
from .machine_events import EVENT_SIGNALS, READ_SIGNALS

LIMIT = 40


def workload(db, observed):
    def groups(model, field):
        return dict(db.execute(select(field, func.count()).join(Organization, Organization.id == model.organization_id)
            .where(Organization.active.is_(True)).group_by(field)).all())
    emails, orders, routes = groups(EmailIntake, EmailIntake.status), groups(Order, Order.status), groups(Route, Route.status)
    auto = db.scalar(select(func.count()).select_from(Order).join(Organization, Organization.id == Order.organization_id)
        .join(Settings, Settings.organization_id == Order.organization_id).where(Organization.active.is_(True), Order.status == 'NEW', Settings.data['dispatch_mode'].astext == 'AUTO'))
    decisions = select(DispatchDecision).distinct(DispatchDecision.organization_id, DispatchDecision.order_id).order_by(
        DispatchDecision.organization_id, DispatchDecision.order_id, DispatchDecision.created_at.desc(), DispatchDecision.id.desc()).subquery()
    no_driver = db.scalar(select(func.count()).select_from(Order).join(Organization, Organization.id == Order.organization_id)
        .join(Settings, Settings.organization_id == Order.organization_id).join(decisions, and_(decisions.c.organization_id == Order.organization_id, decisions.c.order_id == Order.id,
            decisions.c.order_version == Order.version)).where(Organization.active.is_(True), Order.status == 'NEW',
            Settings.data['dispatch_mode'].astext == 'AUTO', decisions.c.mode == 'AUTO', decisions.c.status == 'NO_CANDIDATE'))
    completed = db.scalar(select(func.count()).select_from(Order).join(Organization, Organization.id == Order.organization_id)
        .where(Organization.active.is_(True), Order.status == 'COMPLETED', Order.completed_at >= observed - timedelta(hours=24)))
    return dict(emails_waiting=emails.get('RECEIVED', 0), emails_review=emails.get('NEEDS_REVIEW', 0) + emails.get('UNKNOWN_SENDER', 0), emails_failed=emails.get('FAILED', 0),
        orders_waiting=orders.get('NEW', 0), auto_waiting=auto, manual_waiting=orders.get('NEW', 0) - auto, no_driver=no_driver,
        orders_assigned=orders.get('ASSIGNED', 0), orders_in_progress=orders.get('IN_PROGRESS', 0), routes_in_progress=routes.get('IN_PROGRESS', 0), completed_24h=completed)


def agent_states(db, agents, work, observed):
    ids = [item['run'].id for item in agents if item['run']]
    latest = dict(db.execute(select(AgentLog.run_id, Organization.name).outerjoin(Organization, Organization.id == AgentLog.organization_id)
        .where(AgentLog.run_id.in_(ids)).distinct(AgentLog.run_id)
        .order_by(AgentLog.run_id, AgentLog.created_at.desc(), AgentLog.id.desc())).all()) if ids else {}
    errors = dict(db.execute(select(AgentLog.run_id, AgentLog.message).where(AgentLog.run_id.in_(ids), AgentLog.level == 'ERROR',
        AgentLog.message != 'Run completed with errors').distinct(AgentLog.run_id).order_by(AgentLog.run_id, AgentLog.created_at.desc(), AgentLog.id.desc())).all()) if ids else {}
    approvals = db.scalar(select(func.count()).select_from(TechnicalIssue).where(TechnicalIssue.status == 'AWAITING_APPROVAL'))
    result = []
    for item in agents:
        agent, run, queued = item['agent'], item['run'], item['queued']
        waiting = {
            'ORDER': f"{queued} emails waiting for the Order agent." if queued else 'Waiting for new booking emails.',
            'DISPATCH': f"{work['auto_waiting']} automatic orders waiting; {work['no_driver']} have no eligible driver in their latest evaluation." if work['auto_waiting'] else 'Waiting for automatic dispatch work.',
            'OPERATIONAL': 'Waiting for the worker to pick up the order review.' if queued else 'Waiting for the next order review.',
            'TECHNICAL': f'{approvals} repairs await admin approval.' if approvals else 'Waiting for the next platform check.',
            'CHAT': 'Waiting for a workspace question.',
        }[agent]
        if item['stale']: state, detail = 'ERROR', 'Progress is overdue. The last heartbeat is no longer current.'
        elif run and run.status == 'RUNNING': state, detail = 'PROCESSING', run.step
        elif run and run.status == 'FAILED': state, detail = 'ERROR', errors.get(run.id, run.step)
        elif run and run.status == 'QUEUED': state, detail = 'WAITING', waiting
        elif not run and not queued: state, detail = 'UNKNOWN', 'No agent run has been recorded.'
        elif run and agent != 'CHAT' and run.heartbeat_at < observed - timedelta(minutes=5):
            state, detail = ('WAITING', waiting + ' No recent worker heartbeat has been recorded.') if queued else ('UNKNOWN', 'No recent worker progress. Worker availability is unknown.')
        else: state, detail = 'WAITING', waiting + (' No worker run has been recorded.' if not run else '')
        result.append(dict(agent=agent, state=state, detail=detail, company_name=latest.get(run.id) if run and state == 'PROCESSING' else None,
            heartbeat_at=run.heartbeat_at if run and run.status != 'QUEUED' else None, queued=queued))
    return result


def activity(db, observed):
    since = observed - timedelta(days=1)
    events = db.execute(select(Event, Organization.name, Order.number, Order.source).join(Organization, Organization.id == Event.organization_id)
        .outerjoin(Order, and_(Order.id == Event.order_id, Order.organization_id == Event.organization_id))
        .where(Event.created_at >= since, Event.created_at <= observed, Event.dispatchers.is_(True), Event.type.in_(EVENT_SIGNALS))
        .order_by(Event.created_at.desc(), Event.id.desc()).limit(LIMIT + 1)).all()
    logs = db.execute(select(AgentLog, AgentRun.agent, Organization.name).join(AgentRun, AgentRun.id == AgentLog.run_id)
        .outerjoin(Organization, Organization.id == AgentLog.organization_id).where(AgentLog.created_at >= since, AgentLog.created_at <= observed)
        .order_by(AgentLog.created_at.desc(), AgentLog.id.desc()).limit(LIMIT + 1)).all()
    items = []
    for row, company, reference, source in events:
        start, end, title, level = EVENT_SIGNALS[row.type]
        if row.type == 'order.created' and source == 'EMAIL': start = 'ORDER'
        items.append(dict(id=f'EVENT:{row.id}', source='EVENT', kind=row.type, from_node=start, to_node=end, title=title,
            level=level, company_name=company, reference=reference, created_at=row.created_at))
    for row, agent, company in logs:
        start, end = READ_SIGNALS.get((agent, row.message), (None, agent))
        items.append(dict(id=f'AGENT:{row.id}', source='AGENT', kind='agent.step', from_node=start, to_node=end,
            title=row.message, level=row.level, company_name=company, reference=None, created_at=row.created_at))
    items.sort(key=lambda item: (item['created_at'], item['id']), reverse=True)
    return items[:LIMIT], len(items) > LIMIT


def snapshot(db, agents, observed):
    work = workload(db, observed)
    entries, truncated = activity(db, observed)
    return dict(observed_at=observed, agents=agent_states(db, agents, work, observed), workload=work, activity=entries, activity_truncated=truncated, window_seconds=86400)
