from fastapi import HTTPException
from datetime import timedelta
from sqlalchemy import select, text, or_, and_
from app.models import now
from app.services import audit, once
from .models import AgentRun, TechnicalIssue
from .schemas import AgentRunView
from .queries import issue_view
from .classification import issue_agent


def request_check(db, owner, key):
    def run():
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('technical-scan-request', 0))"))
        row = db.scalar(select(AgentRun).where(AgentRun.agent == 'TECHNICAL', or_(AgentRun.status == 'QUEUED',
            and_(AgentRun.status == 'RUNNING', AgentRun.heartbeat_at >= now() - timedelta(minutes=5)))).order_by(AgentRun.created_at.desc()))
        if row is None:
            row = AgentRun(agent='TECHNICAL', status='QUEUED', step='Waiting for the Technical agent', progress=0)
            db.add(row); db.flush()
            audit(db, owner, 'platform_owner.technical_scan_requested', row.id, None)
        return AgentRunView.model_validate(row).model_dump(mode='json')
    return once(db, owner, key, 'technical-scan', {}, run)


def decide(db, owner, identity, version, approve, key):
    def run():
        row = db.scalar(select(TechnicalIssue).where(TechnicalIssue.id == identity).with_for_update())
        if row is None: raise HTTPException(404, 'Technical report not found.')
        if row.version != version: raise HTTPException(409, 'This report changed. Reload before continuing.')
        if approve:
            if row.status != 'AWAITING_APPROVAL' or row.action not in {'RETRY_ORDER_AGENT', 'RETRY_MAILBOX'}:
                raise HTTPException(409, 'This report has no repair awaiting approval.')
            row.status, row.approved_by = 'APPROVED', owner.id
        else:
            if row.status not in {'NEEDS_ADMIN', 'AWAITING_APPROVAL', 'FAILED'}:
                raise HTTPException(409, 'This report cannot be dismissed while a repair is queued or running.')
            row.status = 'REJECTED'
        row.version += 1; row.updated_at = now()
        audit(db, owner, 'platform_owner.technical_repair_approved' if approve else 'platform_owner.technical_report_dismissed', row.id, row.organization_id)
        db.flush()
        return issue_view(db, row)
    result = once(db, owner, key, f'technical-issue:{identity}:{approve}', {'version': version}, run)
    # Add the read-only projection to pre-flowchart replay results without repeating a decision.
    if 'agent' not in result:
        agent = db.scalar(select(issue_agent()).where(TechnicalIssue.id == identity))
        if agent is None: raise HTTPException(404, 'Technical report not found.')
        result = {**result, 'agent': agent}
    return result
