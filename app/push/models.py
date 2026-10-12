"""Driver push records (migration 0034). Devices die with their login session."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base, now


class PushDevice(Base):
    __tablename__ = 'push_devices'
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'))
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    driver_id: Mapped[UUID]
    session_hash: Mapped[str] = mapped_column(String(64), ForeignKey('login_sessions.token_hash', ondelete='CASCADE'))
    platform: Mapped[str] = mapped_column(String(10))
    token: Mapped[str] = mapped_column(String(4096))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PushMessage(Base):
    __tablename__ = 'push_messages'
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'))
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    driver_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(String(500))
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
