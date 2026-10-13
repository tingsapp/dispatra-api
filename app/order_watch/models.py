"""Operational observations and driver clarification, separate from technical reports."""
from datetime import datetime
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base
from app.operations.models import TenantRecord


class Incident(TenantRecord, Base):
    __tablename__ = 'order_watch_incidents'
    __table_args__ = (
        UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'fingerprint'),
        ForeignKeyConstraint(['organization_id','order_id'], ['orders.organization_id','orders.id']),
        ForeignKeyConstraint(['organization_id','stop_id'], ['order_stops.organization_id','order_stops.id']),
        ForeignKeyConstraint(['organization_id','source_issue_id'], ['operation_issues.organization_id','operation_issues.id']),
        CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','RESOLVED','SUPERSEDED')"),
        CheckConstraint("certainty IN ('OBSERVED','PREDICTED','REPORTED','NEEDS_CONFIRMATION')"),
        CheckConstraint("severity IN ('WARNING','CRITICAL')"),
        Index('ix_order_watch_open', 'organization_id', 'status', 'created_at'),
    )
    order_id: Mapped[UUID]
    stop_id: Mapped[UUID | None]
    source_issue_id: Mapped[UUID | None]
    fingerprint: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(1000))
    severity: Mapped[str] = mapped_column(String(12))
    certainty: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(20), default='OPEN')
    evidence: Mapped[list] = mapped_column(JSONB)
    suggestions: Mapped[list] = mapped_column(JSONB)
    response_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str] = mapped_column(String(500), default='')


class Question(TenantRecord, Base):
    __tablename__ = 'order_watch_questions'
    __table_args__ = (
        UniqueConstraint('organization_id','id'), UniqueConstraint('incident_id'),
        ForeignKeyConstraint(['organization_id','incident_id'], ['order_watch_incidents.organization_id','order_watch_incidents.id']),
        ForeignKeyConstraint(['organization_id','order_id'], ['orders.organization_id','orders.id']),
        ForeignKeyConstraint(['organization_id','route_id'], ['routes.organization_id','routes.id']),
        ForeignKeyConstraint(['organization_id','driver_id'], ['drivers.organization_id','drivers.id']),
        CheckConstraint("status IN ('OPEN','ANSWERED','CLOSED')"),
    )
    incident_id: Mapped[UUID]
    order_id: Mapped[UUID]
    route_id: Mapped[UUID]
    driver_id: Mapped[UUID]
    generation: Mapped[int]
    prompt: Mapped[str] = mapped_column(String(600))
    status: Mapped[str] = mapped_column(String(16), default='OPEN')
    reason: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[str] = mapped_column(String(1000), default='')
    estimated_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RouteEstimate(TenantRecord, Base):
    __tablename__ = 'order_watch_estimates'
    __table_args__ = (UniqueConstraint('organization_id','id'), UniqueConstraint('route_id'),
        ForeignKeyConstraint(['organization_id','route_id'], ['routes.organization_id','routes.id']))
    route_id: Mapped[UUID]
    generation: Mapped[int]
    route_version: Mapped[int]
    next_stop_id: Mapped[UUID]
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSONB)
