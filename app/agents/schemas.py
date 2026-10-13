from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from app.schemas import Input

AgentName = Literal['ORDER', 'DISPATCH', 'TECHNICAL', 'OPERATIONAL']


class AgentRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    agent: AgentName
    status: Literal['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED']
    step: str
    progress: int
    created_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None


class AgentStatus(BaseModel):
    agent: AgentName
    run: AgentRunView | None
    stale: bool
    queued: int
    issue_count: int


class AgentLogView(BaseModel):
    id: UUID
    run_id: UUID
    agent: AgentName
    organization_id: UUID | None
    company_name: str | None
    level: Literal['INFO', 'WARNING', 'ERROR']
    message: str
    created_at: datetime


class AgentLogPage(BaseModel):
    items: list[AgentLogView]
    next_before: UUID | None


class TechnicalIssueView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    version: int
    organization_id: UUID | None
    company_name: str | None
    agent: AgentName
    kind: str
    severity: Literal['WARNING', 'CRITICAL']
    title: str
    description: str
    status: Literal['NEEDS_ADMIN', 'AWAITING_APPROVAL', 'APPROVED', 'APPLYING', 'FIXED', 'FAILED', 'REJECTED', 'SUPERSEDED']
    action: Literal['RETRY_ORDER_AGENT', 'RETRY_MAILBOX'] | None
    automatic: bool
    fixed_count: int
    created_at: datetime
    updated_at: datetime


class AgentMonitorView(BaseModel):
    observed_at: datetime
    agents: list[AgentStatus]
    running: int
    awaiting_approval: int
    needs_attention: int
    automatic_fixes_24h: int
    errors_24h: int
    issues: list[TechnicalIssueView]


class TechnicalIssuePage(BaseModel):
    items: list[TechnicalIssueView]
    next_before: UUID | None


class ClientDiagnostic(Input):
    source: Literal['WEB', 'DRIVER']
    kind: Literal['RUNTIME_ERROR', 'UNHANDLED_REJECTION']


class DiagnosticAccepted(BaseModel):
    accepted: bool = True


class CompanyActivityEntry(BaseModel):
    id: str
    source: Literal['AGENT', 'EVENT']
    agent: AgentName | None
    actor: Literal['AGENT', 'DISPATCHER', 'SHIPPER', 'DRIVER', 'SYSTEM', 'UNKNOWN']
    kind: str
    title: str
    level: Literal['INFO', 'WARNING', 'ERROR']
    created_at: datetime
    order_id: UUID | None
    order_number: str | None


class CompanyActivityPage(BaseModel):
    items: list[CompanyActivityEntry]
    next_before: str | None
    observed_at: datetime
    time_zone: str | None
