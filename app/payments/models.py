from uuid import UUID
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint, String
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base
from app.operations.models import TenantRecord


class StripeConnection(TenantRecord, Base):
    __tablename__ = 'stripe_connections'
    __table_args__ = (UniqueConstraint('organization_id', 'livemode'), UniqueConstraint('organization_id', 'id'), UniqueConstraint('account_id', 'livemode'))
    account_id: Mapped[str] = mapped_column(String(100))
    livemode: Mapped[bool]


class StripeCustomer(TenantRecord, Base):
    __tablename__ = 'stripe_customers'
    __table_args__ = (
        UniqueConstraint('organization_id', 'shipper_id', 'account_id', 'livemode'),
        UniqueConstraint('account_id', 'customer_id', 'livemode'),
        ForeignKeyConstraint(['organization_id', 'shipper_id'], ['shippers.organization_id', 'shippers.id']),
    )
    shipper_id: Mapped[UUID]
    account_id: Mapped[str] = mapped_column(String(100))
    livemode: Mapped[bool]
    customer_id: Mapped[str | None] = mapped_column(String(100))
