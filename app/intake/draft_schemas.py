"""Partial shipment facts use null for unknowns; final Orders still require Booking."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, Field, AwareDatetime
from app.schemas import Input
from app.operations.schemas import StopInput, ItemInput, AccessorialSelection, Adjustment, Money, Positive, Text
from .schemas import IntakeVersion


class DraftAddress(Input):
    text: str = Field(default='', max_length=500)
    city: str = Field(default='', max_length=160)
    province: str = Field(default='', max_length=30)
    postal_code: str = Field(default='', max_length=20)
    country: Literal['CA'] = 'CA'
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class DraftStop(StopInput):
    address: DraftAddress | None = None


class DraftItem(ItemInput):
    pickup_id: UUID | None = None
    delivery_id: UUID | None = None
    quantity: Annotated[int, Field(ge=1, le=10000)] | None = None
    weight_kg: Positive | None = None
    length_cm: Positive | None = None
    width_cm: Positive | None = None
    height_cm: Positive | None = None


class DraftShipment(Input):
    service_id: UUID | None = None
    vehicle_type_id: UUID | None = None
    preferred_driver_id: UUID | None = None
    scheduled_at: AwareDatetime | None = None
    stops: list[DraftStop] = Field(default_factory=list, max_length=50)
    items: list[DraftItem] = Field(default_factory=list, max_length=200)
    accessorials: list[AccessorialSelection] = Field(default_factory=list, max_length=50)
    external_reference: str = Field(default='', max_length=160)


class DraftBooking(DraftShipment):
    shipper_id: UUID | None = None
    billing_shipper_id: UUID | None = None
    rate_card_id: UUID | None = None
    distance_km: Money | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=10080)
    adjustments: list[Adjustment] = Field(default_factory=list, max_length=20)
    internal_notes: Text = ''
    imported_total: Money | None = None
    imported_tax: Money | None = None


class SaveEmailDraft(IntakeVersion):
    draft: DraftBooking


class SubmitShipperDraft(IntakeVersion):
    draft: DraftShipment


class ShipperDraftView(BaseModel):
    id: UUID
    version: int
    draft: DraftShipment
    submitted_at: datetime | None
