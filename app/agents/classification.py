"""One authoritative agent assignment for existing and future technical report projections."""
from sqlalchemy import and_, case
from .models import TechnicalIssue


def issue_agent():
    return case(
        (TechnicalIssue.kind.in_(['ORDER_PROCESSING', 'MAILBOX_CONNECTION']), 'ORDER'),
        (and_(TechnicalIssue.kind == 'WORKER_PROGRESS', TechnicalIssue.fingerprint.like('worker:ORDER:%')), 'ORDER'),
        (and_(TechnicalIssue.kind == 'WORKER_PROGRESS', TechnicalIssue.fingerprint.like('worker:DISPATCH:%')), 'DISPATCH'),
        else_='TECHNICAL',
    )
