from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base, now


class SupportTicket(Base):
    __tablename__ = 'support_tickets'
    __table_args__ = (
        CheckConstraint("delivery_status IN ('PENDING','SENDING','SENT','FAILED','UNKNOWN')"),
        CheckConstraint('attempts >= 0'),
        Index('support_tickets_pending', 'available_at',
              postgresql_where=text("delivery_status IN ('PENDING','SENDING')")),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_hash: Mapped[str] = mapped_column(String(64), unique=True)
    request_id: Mapped[UUID]
    request_hash: Mapped[str] = mapped_column(String(64))
    number: Mapped[str] = mapped_column(String(24), unique=True)
    email: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(160))
    details: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    delivery_status: Mapped[str] = mapped_column(String(16), default='PENDING')
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[UUID | None]
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
