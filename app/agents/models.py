from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base, now


class AgentRun(Base):
    __tablename__ = 'agent_runs'
    __table_args__ = (CheckConstraint("agent IN ('ORDER','DISPATCH','TECHNICAL','OPERATIONAL')"),
        CheckConstraint("status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')"),
        CheckConstraint('progress BETWEEN 0 AND 100'), Index('ix_agent_runs_recent', 'agent', 'created_at'))
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('organizations.id'))
    agent: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default='RUNNING')
    step: Mapped[str] = mapped_column(String(300), default='Starting')
    progress: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentLog(Base):
    __tablename__ = 'agent_logs'
    __table_args__ = (CheckConstraint("level IN ('INFO','WARNING','ERROR')"), Index('ix_agent_logs_recent', 'created_at', 'id'),
        Index('ix_agent_logs_company_recent', 'organization_id', 'created_at', 'id'))
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey('agent_runs.id'))
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('organizations.id'))
    level: Mapped[str] = mapped_column(String(12), default='INFO')
    message: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TechnicalIssue(Base):
    __tablename__ = 'technical_issues'
    __table_args__ = (UniqueConstraint('fingerprint'),
        CheckConstraint("status IN ('NEEDS_ADMIN','AWAITING_APPROVAL','APPROVED','APPLYING','FIXED','FAILED','REJECTED','SUPERSEDED')"),
        CheckConstraint("severity IN ('WARNING','CRITICAL')"),
        CheckConstraint("action IS NULL OR action IN ('RETRY_ORDER_AGENT','RETRY_MAILBOX')"),
        Index('ix_technical_issues_recent', 'status', 'created_at'))
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('organizations.id'))
    fingerprint: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(12), default='WARNING')
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(24))
    action: Mapped[str | None] = mapped_column(String(24))
    # Internal, versioned repair targets never appear in owner responses.
    target_id: Mapped[UUID | None]
    target_version: Mapped[int | None]
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey('users.id'))
    automatic: Mapped[bool] = mapped_column(default=False)
    fixed_count: Mapped[int] = mapped_column(default=0)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RuntimeDiagnostic(Base):
    __tablename__ = 'runtime_diagnostics'
    __table_args__ = (UniqueConstraint('organization_id', 'source', 'kind', 'bucket', name='runtime_diagnostic_window', postgresql_nulls_not_distinct=True),
        CheckConstraint("source IN ('API','WEB','DRIVER')"),
        CheckConstraint("kind IN ('HTTP_5XX','RUNTIME_ERROR','UNHANDLED_REJECTION')"))
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('organizations.id'))
    source: Mapped[str] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(24))
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occurrences: Mapped[int] = mapped_column(default=1)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
