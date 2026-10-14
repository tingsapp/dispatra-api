from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import func, select, tuple_
from app.models import Organization, now
from app.intake.models import EmailIntake
from app.operations.models import Order, Settings
from .models import AgentRun, AgentLog, TechnicalIssue
from .schemas import AgentLogView, AgentRunView, TechnicalIssueView
from .classification import issue_agent
from app.order_watch.models import Incident
from app.order_watch.commands import OPEN as OPERATIONAL_OPEN

NAMES = ('ORDER', 'DISPATCH', 'TECHNICAL', 'OPERATIONAL', 'CHAT')
OPEN = ('NEEDS_ADMIN', 'AWAITING_APPROVAL', 'APPROVED', 'APPLYING', 'FAILED')
STALE_AFTER = timedelta(minutes=5)


def issue_view(db, row, name=None, agent=None):
    if name is None and row.organization_id:
        name = db.scalar(select(Organization.name).where(Organization.id == row.organization_id))
    if agent is None: agent = db.scalar(select(issue_agent()).where(TechnicalIssue.id == row.id))
    return TechnicalIssueView.model_validate({**{key: getattr(row, key) for key in TechnicalIssueView.model_fields if key not in ('company_name', 'agent')}, 'company_name': name, 'agent': agent}).model_dump(mode='json')


def snapshot(db):
    observed = now()
    latest = db.scalars(select(AgentRun).distinct(AgentRun.agent).order_by(AgentRun.agent, AgentRun.created_at.desc(), AgentRun.id.desc())).all()
    runs = {row.agent: row for row in latest}
    active_chat = db.scalar(select(AgentRun).where(AgentRun.agent == 'CHAT', AgentRun.status == 'RUNNING', AgentRun.heartbeat_at >= observed - STALE_AFTER).order_by(AgentRun.created_at.desc()).limit(1))
    if active_chat: runs['CHAT'] = active_chat
    queued = dict(db.execute(select(AgentRun.agent, func.count()).where(AgentRun.status == 'QUEUED').group_by(AgentRun.agent)).all())
    classification = issue_agent()
    issue_counts = dict(db.execute(select(classification, func.count()).where(TechnicalIssue.status.in_(OPEN)).group_by(classification)).all())
    operational_count = db.scalar(select(func.count()).select_from(Incident).where(Incident.status.in_(OPERATIONAL_OPEN)))
    issue_counts['OPERATIONAL'] = operational_count
    # Queue sizes are operational counts only; no email, Order or driver detail leaves this endpoint.
    queued['ORDER'] = db.scalar(select(func.count()).select_from(EmailIntake).join(Organization, Organization.id == EmailIntake.organization_id)
        .where(EmailIntake.status == 'RECEIVED', Organization.active.is_(True)))
    queued['DISPATCH'] = db.scalar(select(func.count()).select_from(Order).join(Settings, Settings.organization_id == Order.organization_id)
        .join(Organization, Organization.id == Order.organization_id).where(Order.status == 'NEW', Organization.active.is_(True), Settings.data['dispatch_mode'].astext == 'AUTO'))
    agents = [{'agent': name, 'run': AgentRunView.model_validate(runs[name]) if name in runs else None,
        'stale': name in runs and runs[name].status == 'RUNNING' and runs[name].heartbeat_at < observed - STALE_AFTER,
        'queued': queued.get(name, 0), 'issue_count': issue_counts.get(name, 0)} for name in NAMES]
    rows = db.execute(select(TechnicalIssue, Organization.name, issue_agent()).outerjoin(Organization, Organization.id == TechnicalIssue.organization_id)
        .where(TechnicalIssue.status.in_(OPEN)).order_by(TechnicalIssue.created_at.desc(), TechnicalIssue.id.desc()).limit(100))
    return {'observed_at': observed, 'agents': agents,
        'running': sum(bool(item['run'] and item['run'].status == 'RUNNING' and not item['stale']) for item in agents),
        'awaiting_approval': db.scalar(select(func.count()).select_from(TechnicalIssue).where(TechnicalIssue.status == 'AWAITING_APPROVAL')),
        'needs_attention': operational_count + db.scalar(select(func.count()).select_from(TechnicalIssue).where(TechnicalIssue.status.in_(['NEEDS_ADMIN', 'AWAITING_APPROVAL', 'FAILED']))),
        'automatic_fixes_24h': db.scalar(select(func.coalesce(func.sum(TechnicalIssue.fixed_count), 0)).where(TechnicalIssue.automatic.is_(True), TechnicalIssue.status == 'FIXED', TechnicalIssue.created_at >= observed - timedelta(hours=24))),
        'errors_24h': db.scalar(select(func.count()).select_from(AgentLog).where(AgentLog.level == 'ERROR', AgentLog.created_at >= observed - timedelta(hours=24))),
        'issues': [issue_view(db, row, name, agent) for row, name, agent in rows]}


def logs(db, agent=None, level=None, before=None, limit=50):
    query = select(AgentLog, AgentRun.agent, Organization.name).join(AgentRun, AgentRun.id == AgentLog.run_id).outerjoin(Organization, Organization.id == AgentLog.organization_id)
    if agent: query = query.where(AgentRun.agent == agent)
    if level: query = query.where(AgentLog.level == level)
    if before:
        anchor = db.execute(query.where(AgentLog.id == before)).first()
        if anchor is None: raise HTTPException(404, 'Log entry not found.')
        query = query.where(tuple_(AgentLog.created_at, AgentLog.id) < (anchor[0].created_at, anchor[0].id))
    rows = db.execute(query.order_by(AgentLog.created_at.desc(), AgentLog.id.desc()).limit(limit + 1)).all()
    items = [AgentLogView(id=row.id, run_id=row.run_id, agent=name, organization_id=row.organization_id, company_name=company,
        level=row.level, message=row.message, created_at=row.created_at) for row, name, company in rows[:limit]]
    return {'items': items, 'next_before': items[-1].id if len(rows) > limit else None}


def issues(db, status='OPEN', before=None, limit=30, agent=None):
    query = select(TechnicalIssue, Organization.name, issue_agent()).outerjoin(Organization, Organization.id == TechnicalIssue.organization_id)
    if status == 'OPEN': query = query.where(TechnicalIssue.status.in_(OPEN))
    if agent: query = query.where(issue_agent() == agent)
    if before:
        anchor = db.execute(query.where(TechnicalIssue.id == before)).first()
        if anchor is None: raise HTTPException(404, 'Technical report not found.')
        query = query.where(tuple_(TechnicalIssue.created_at, TechnicalIssue.id) < (anchor[0].created_at, anchor[0].id))
    rows = db.execute(query.order_by(TechnicalIssue.created_at.desc(), TechnicalIssue.id.desc()).limit(limit + 1)).all()
    return {'items': [issue_view(db, row, name, agent) for row, name, agent in rows[:limit]], 'next_before': rows[limit - 1][0].id if len(rows) > limit else None}
