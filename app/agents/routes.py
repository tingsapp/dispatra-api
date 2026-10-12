from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from app.security import rate_limit
from app.platform.schemas import VersionCommand
from . import commands, diagnostics, queries
from .schemas import AgentName, AgentMonitorView, AgentLogPage, AgentRunView, TechnicalIssueView, TechnicalIssuePage, ClientDiagnostic, DiagnosticAccepted

router = APIRouter(prefix='/api/v1/platform/agents', tags=['Platform agents'])
Owner = Annotated[User, Depends(auth.platform)]


@router.get('/monitor', response_model=AgentMonitorView)
def monitor(db: DB, owner: Owner):
    return queries.snapshot(db)


@router.get('/logs', response_model=AgentLogPage)
def logs(db: DB, owner: Owner, agent: AgentName | None = None, level: Literal['INFO', 'WARNING', 'ERROR'] | None = None,
         before: UUID | None = None, limit: int = Query(50, ge=1, le=100)):
    return queries.logs(db, agent, level, before, limit)


@router.post('/technical/check', response_model=AgentRunView, status_code=202)
def check_platform(db: DB, owner: Owner, key: OperationKey):
    return commands.request_check(db, owner, key)


@router.get('/issues', response_model=TechnicalIssuePage)
def reports(db: DB, owner: Owner, status: Literal['OPEN', 'ALL'] = 'OPEN', before: UUID | None = None, limit: int = Query(30, ge=1, le=100), agent: AgentName | None = None):
    return queries.issues(db, status, before, limit, agent)


@router.post('/issues/{identity}/approve', response_model=TechnicalIssueView, status_code=202)
def approve_repair(identity: UUID, data: VersionCommand, db: DB, owner: Owner, key: OperationKey):
    return commands.decide(db, owner, identity, data.version, True, key)


@router.post('/issues/{identity}/reject', response_model=TechnicalIssueView)
def reject_repair(identity: UUID, data: VersionCommand, db: DB, owner: Owner, key: OperationKey):
    return commands.decide(db, owner, identity, data.version, False, key)


client_router = APIRouter(prefix='/api/v1/diagnostics', tags=['Platform diagnostics'])


@client_router.post('/runtime', response_model=DiagnosticAccepted, status_code=202)
def report_runtime(data: ClientDiagnostic, user: Annotated[User, Depends(auth.authenticated)]):
    rate_limit(f'runtime-diagnostic:{user.id}', 5)
    if data.source == 'DRIVER' and user.role != 'DRIVER':
        from fastapi import HTTPException
        raise HTTPException(403, 'Driver access required.')
    diagnostics.record(data.source, data.kind, user.organization_id)
    return DiagnosticAccepted()
