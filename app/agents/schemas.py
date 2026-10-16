from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from app.schemas import Input

AgentName = Literal['ORDER', 'DISPATCH', 'TECHNICAL', 'OPERATIONAL', 'CHAT']


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
    machine: "MachineTelemetry | None" = None


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


MachineNode = Literal['inputs', 'ORDER', 'data', 'DISPATCH', 'delivery', 'OPERATIONAL', 'TECHNICAL', 'CHAT']


class MachineAgentState(BaseModel):
    agent: AgentName
    state: Literal['PROCESSING', 'WAITING', 'ERROR', 'UNKNOWN']
    detail: str
    company_name: str | None
    heartbeat_at: datetime | None
    queued: int


class MachineWorkload(BaseModel):
    emails_waiting: int
    emails_review: int
    emails_failed: int
    orders_waiting: int
    auto_waiting: int
    manual_waiting: int
    no_driver: int
    orders_assigned: int
    orders_in_progress: int
    routes_in_progress: int
    completed_24h: int


class MachineActivity(BaseModel):
    id: str
    source: Literal['EVENT', 'AGENT']
    kind: str
    from_node: MachineNode | None
    to_node: MachineNode
    title: str
    level: Literal['INFO', 'WARNING', 'ERROR']
    company_name: str | None
    reference: str | None
    created_at: datetime


class MachineTelemetry(BaseModel):
    observed_at: datetime
    agents: list[MachineAgentState]
    workload: MachineWorkload
    activity: list[MachineActivity]
    activity_truncated: bool
    window_seconds: int = 86400


AgentMonitorView.model_rebuild()
