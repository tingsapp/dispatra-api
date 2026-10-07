from uuid import UUID
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base
from app.operations.models import TenantRecord

MODES = ('AUTO', 'MANUAL')
STATUSES = ('SUGGESTED', 'ASSIGNED', 'NO_CANDIDATE')


class DispatchDecision(TenantRecord, Base):
    """One evaluation of an Order version: every candidate with its feasibility, the ranking, who ranked it and the outcome."""
    __tablename__ = 'dispatch_decisions'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),
        ForeignKeyConstraint(['organization_id', 'order_id'], ['orders.organization_id', 'orders.id']),
        ForeignKeyConstraint(['organization_id', 'driver_id'], ['drivers.organization_id', 'drivers.id']),
        CheckConstraint(f"mode IN {MODES}", name='dispatch_decision_mode'),
        CheckConstraint(f"status IN {STATUSES}", name='dispatch_decision_status'),
        CheckConstraint("ranked_by IN ('AI','RULES')", name='dispatch_decision_ranked_by'))
    order_id: Mapped[UUID]
    order_version: Mapped[int]
    mode: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20))
    ranked_by: Mapped[str] = mapped_column(String(10), default='RULES')
    # Feasible candidates in ranked order, each with metrics, rule facts and the ranking reason.
    candidates: Mapped[list] = mapped_column(JSONB, default=list)
    # Drivers that failed a hard check, with the check's message.
    excluded: Mapped[list] = mapped_column(JSONB, default=list)
    summary: Mapped[str] = mapped_column(Text, default='')
    # The chosen candidate once assigned; NULL user means the agent assigned in AUTO mode.
    driver_id: Mapped[UUID | None]
    decided_by: Mapped[UUID | None]
    error_code: Mapped[str | None] = mapped_column(String(80))
