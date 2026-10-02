from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4
from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, UniqueConstraint, CheckConstraint, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, declared_attr
from app.models import Base, now


class TenantRecord:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'), index=True)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    @declared_attr.directive
    def __table_args__(cls):
        return (UniqueConstraint('organization_id', 'id'),)


class Settings(TenantRecord, Base):
    __tablename__ = 'company_settings'
    __table_args__ = (UniqueConstraint('organization_id'),)
    data: Mapped[dict] = mapped_column(JSONB)


class Catalog(TenantRecord, Base):
    __tablename__ = 'catalog_entries'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'kind', 'code'))
    kind: Mapped[str] = mapped_column(String(20))
    code: Mapped[str] = mapped_column(String(60))
    active: Mapped[bool] = mapped_column(default=True)
    data: Mapped[dict] = mapped_column(JSONB)


class RateCard(TenantRecord, Base):
    __tablename__ = 'rate_cards'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'code'))
    code: Mapped[str] = mapped_column(String(60))
    active: Mapped[bool] = mapped_column(default=True)
    is_default: Mapped[bool] = mapped_column(default=False)
    data: Mapped[dict] = mapped_column(JSONB)


class Shipper(TenantRecord, Base):
    """Shipper identity, login profile and commercial defaults (merged with the former `customers` table in 0016).
    A NULL warehouse marks a legacy account-only record without an operational profile."""
    __tablename__ = 'shippers'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'number'),
        CheckConstraint("status IN ('ACTIVE','INACTIVE','ON_HOLD')", name='shippers_status_check'),
        ForeignKeyConstraint(['organization_id', 'rate_card_id'], ['rate_cards.organization_id', 'rate_cards.id']))
    number: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(160))
    contact_name: Mapped[str] = mapped_column(String(160), default='')
    email: Mapped[str] = mapped_column(String(254), default='')
    phone: Mapped[str] = mapped_column(String(50), default='')
    address: Mapped[str] = mapped_column(String(500), default='')
    status: Mapped[str] = mapped_column(String(20), default='ACTIVE')
    kind: Mapped[str] = mapped_column(String(20), default='BUSINESS')
    company_name: Mapped[str] = mapped_column(String(160), default='')
    warehouse: Mapped[dict | None] = mapped_column(JSONB)
    rate_card_id: Mapped[UUID | None]
    terms: Mapped[str] = mapped_column(String(20), default='NET30')
    discount: Mapped[dict] = mapped_column(JSONB, default=lambda: {'kind': 'NONE', 'value': '0'})
    instructions: Mapped[str] = mapped_column(Text, default='')
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Vehicle(TenantRecord, Base):
    __tablename__ = 'vehicles'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'number', name='vehicles_company_number'), UniqueConstraint('organization_id', 'plate', 'province'),
        ForeignKeyConstraint(['organization_id', 'type_id'], ['catalog_entries.organization_id', 'catalog_entries.id']))
    number: Mapped[str] = mapped_column(String(50))
    type_id: Mapped[UUID]
    plate: Mapped[str] = mapped_column(String(30))
    province: Mapped[str] = mapped_column(String(30))
    active: Mapped[bool] = mapped_column(default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSONB)


class Driver(TenantRecord, Base):
    __tablename__ = 'drivers'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'email'),
        UniqueConstraint('organization_id', 'number'),
        ForeignKeyConstraint(['organization_id', 'vehicle_id'], ['vehicles.organization_id', 'vehicles.id']))
    number: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(50))
    address: Mapped[dict] = mapped_column(JSONB)
    service_city: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    vehicle_id: Mapped[UUID | None]
    data: Mapped[dict] = mapped_column(JSONB)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    location_permission: Mapped[str] = mapped_column(String(20), default='UNKNOWN')


class Route(TenantRecord, Base):
    __tablename__ = 'routes'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),
        ForeignKeyConstraint(['organization_id', 'driver_id'], ['drivers.organization_id', 'drivers.id']),
        ForeignKeyConstraint(['organization_id', 'vehicle_id'], ['vehicles.organization_id', 'vehicles.id']),
        CheckConstraint("status IN ('PLANNED','IN_PROGRESS','COMPLETED','CANCELLED')"))
    driver_id: Mapped[UUID]
    vehicle_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(20), default='PLANNED')
    locked: Mapped[bool] = mapped_column(default=False)
    generation: Mapped[int] = mapped_column(default=1)
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plan: Mapped[dict] = mapped_column(JSONB)


class Order(TenantRecord, Base):
    __tablename__ = 'orders'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'number'),
        ForeignKeyConstraint(['organization_id', 'shipper_id'], ['shippers.organization_id', 'shippers.id']),
        ForeignKeyConstraint(['organization_id', 'billing_shipper_id'], ['shippers.organization_id', 'shippers.id']),
        ForeignKeyConstraint(['organization_id', 'service_id'], ['catalog_entries.organization_id', 'catalog_entries.id']),
        ForeignKeyConstraint(['organization_id', 'route_id'], ['routes.organization_id', 'routes.id']),
        CheckConstraint("status IN ('NEW','ASSIGNED','IN_PROGRESS','COMPLETED','INVOICED','CANCELLED')"))
    number: Mapped[str] = mapped_column(String(50))
    shipper_id: Mapped[UUID]
    billing_shipper_id: Mapped[UUID]
    service_id: Mapped[UUID]
    route_id: Mapped[UUID | None]
    source: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default='NEW')
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    facts: Mapped[dict] = mapped_column(JSONB)
    booking: Mapped[dict] = mapped_column(JSONB)
    pricing: Mapped[dict] = mapped_column(JSONB)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderStop(TenantRecord, Base):
    __tablename__ = 'order_stops'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'order_id', 'id'),
        ForeignKeyConstraint(['organization_id', 'order_id'], ['orders.organization_id', 'orders.id']))
    order_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(12))
    data: Mapped[dict] = mapped_column(JSONB)


class OrderItem(TenantRecord, Base):
    __tablename__ = 'order_items'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),
        ForeignKeyConstraint(['organization_id', 'order_id'], ['orders.organization_id', 'orders.id']),
        ForeignKeyConstraint(['organization_id', 'order_id', 'pickup_id'], ['order_stops.organization_id', 'order_stops.order_id', 'order_stops.id']),
        ForeignKeyConstraint(['organization_id', 'order_id', 'delivery_id'], ['order_stops.organization_id', 'order_stops.order_id', 'order_stops.id']))
    order_id: Mapped[UUID]
    pickup_id: Mapped[UUID]
    delivery_id: Mapped[UUID]
    quantity: Mapped[int]
    data: Mapped[dict] = mapped_column(JSONB)


class RouteStop(TenantRecord, Base):
    __tablename__ = 'route_stops'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('route_id', 'position'),
        UniqueConstraint('route_id', 'stop_id'),
        ForeignKeyConstraint(['organization_id', 'route_id'], ['routes.organization_id', 'routes.id']),
        ForeignKeyConstraint(['organization_id', 'stop_id'], ['order_stops.organization_id', 'order_stops.id']))
    route_id: Mapped[UUID]
    stop_id: Mapped[UUID]
    position: Mapped[int]
    status: Mapped[str] = mapped_column(String(20), default='PENDING')
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    movements: Mapped[dict] = mapped_column(JSONB, default=dict)


class Evidence(TenantRecord, Base):
    __tablename__ = 'delivery_evidence'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),
        ForeignKeyConstraint(['organization_id', 'stop_id'], ['order_stops.organization_id', 'order_stops.id']),
        ForeignKeyConstraint(['organization_id', 'driver_id'], ['drivers.organization_id', 'drivers.id']))
    stop_id: Mapped[UUID]
    driver_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(20))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    digest: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(80))
    # Bounded evidence bytes are stored as base64, protected by tenant/stop RLS.
    content: Mapped[str] = mapped_column(Text)


class Invoice(TenantRecord, Base):
    __tablename__ = 'invoices'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('order_id'), UniqueConstraint('organization_id', 'number'),
        ForeignKeyConstraint(['organization_id', 'order_id'], ['orders.organization_id', 'orders.id']))
    order_id: Mapped[UUID]
    number: Mapped[str] = mapped_column(String(50))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    tax: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2))


class DutySession(TenantRecord, Base):
    __tablename__ = 'duty_sessions'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),
        ForeignKeyConstraint(['organization_id', 'driver_id'], ['drivers.organization_id', 'drivers.id']))
    driver_id: Mapped[UUID]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Location(TenantRecord, Base):
    __tablename__ = 'driver_locations'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),
        ForeignKeyConstraint(['organization_id', 'driver_id'], ['drivers.organization_id', 'drivers.id']),
        ForeignKeyConstraint(['organization_id', 'duty_id'], ['duty_sessions.organization_id', 'duty_sessions.id']))
    driver_id: Mapped[UUID]
    duty_id: Mapped[UUID]
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSONB)


class Issue(TenantRecord, Base):
    __tablename__ = 'operation_issues'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),
        ForeignKeyConstraint(['organization_id', 'order_id'], ['orders.organization_id', 'orders.id']))
    order_id: Mapped[UUID]
    stop_id: Mapped[UUID | None]
    kind: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(default=False)
    resolution: Mapped[str] = mapped_column(Text, default='')


class Quote(TenantRecord, Base):
    __tablename__ = 'quotes'
    facts: Mapped[dict] = mapped_column(JSONB)
    pricing: Mapped[dict] = mapped_column(JSONB)


class PricingRevision(TenantRecord, Base):
    __tablename__ = 'pricing_revisions'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('order_id', 'order_version'),
        ForeignKeyConstraint(['organization_id', 'order_id'], ['orders.organization_id', 'orders.id']))
    order_id: Mapped[UUID]
    order_version: Mapped[int]
    snapshot: Mapped[dict] = mapped_column(JSONB)


class EmailDelivery(TenantRecord, Base):
    __tablename__ = 'email_deliveries'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),
        ForeignKeyConstraint(['organization_id', 'quote_id'], ['quotes.organization_id', 'quotes.id']),
        ForeignKeyConstraint(['organization_id', 'invoice_id'], ['invoices.organization_id', 'invoices.id']),
        CheckConstraint('(quote_id IS NULL) <> (invoice_id IS NULL)', name='email_delivery_source'),
        CheckConstraint("status IN ('PENDING','SENDING','SENT','FAILED','UNKNOWN')", name='email_delivery_status'))
    quote_id: Mapped[UUID | None]
    invoice_id: Mapped[UUID | None]
    requested_by: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    recipient: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default='PENDING')
    message_id: Mapped[str] = mapped_column(String(200), unique=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
