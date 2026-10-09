"""Versioned operational requests; canonical units are kg, cm, km and CAD."""
import base64
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, EmailStr, AwareDatetime, field_validator, model_validator
from app.schemas import Input, Name

Money = Annotated[Decimal, Field(ge=0, le=10000000, max_digits=16, decimal_places=4)]
Positive = Annotated[Decimal, Field(gt=0, le=10000000, allow_inf_nan=False)]
Percent = Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)]
Text = Annotated[str, Field(max_length=2000)]


class Address(Input):
    text: Annotated[str, Field(min_length=5, max_length=500)]
    city: Name
    province: Annotated[str, Field(min_length=2, max_length=30)]
    postal_code: Annotated[str, Field(pattern=r'^[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d$')]
    country: Literal['CA'] = 'CA'
    latitude: Annotated[float, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[float, Field(ge=-180, le=180)] | None = None

    @model_validator(mode='after')
    def coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError('Provide both coordinates or neither')
        return self


class SettingsData(Input):
    company_name: Name = 'Dispatra Logistics'
    contact_name: str = Field(default='', max_length=160)
    email: EmailStr | Literal[''] = ''
    phone: str = Field(default='', max_length=50)
    # Company correspondence can be manually entered; operational addresses stay structured.
    address: Address | Annotated[str, Field(max_length=500)] | None = None
    logo_url: str = Field(default='', max_length=280000)
    currency: Literal['CAD', 'USD'] = 'CAD'
    time_zone: str = 'America/Vancouver'
    weight_unit: Literal['lb', 'kg'] = 'lb'
    dimension_unit: Literal['in', 'cm'] = 'in'
    distance_unit: Literal['km', 'mi'] = 'km'
    tax_registration_number: str = Field(default='', max_length=100)
    gst_enabled: bool = True
    gst_percent: Percent = Decimal('5')
    provincial_enabled: bool = False
    provincial_percent: Percent = Decimal('0')
    fuel_enabled: bool = True
    fuel_percent: Percent = Decimal('28.5')
    maximum_active_orders: int = Field(default=3, ge=1, le=100)
    quote_validity_days: int = Field(default=14, ge=1, le=365)
    # AUTO: the Dispatch agent assigns priced, unassigned Orders inside its horizon; MANUAL: dispatchers assign, the agent only recommends.
    dispatch_mode: Literal['AUTO', 'MANUAL'] = 'MANUAL'
    # New Orders (and Order agent emails that name no service) start on this active SERVICE; a Shipper's own default wins.
    default_service_id: UUID | None = None

    @field_validator('logo_url')
    @classmethod
    def company_logo(cls, value):
        if not value:
            return value
        if value.startswith(('https://', 'http://')) and len(value) <= 2000:
            return value
        for prefix, signature in [('data:image/png;base64,', b'\x89PNG\r\n\x1a\n'), ('data:image/jpeg;base64,', b'\xff\xd8\xff')]:
            if value.startswith(prefix):
                try:
                    raw = base64.b64decode(value[len(prefix):], validate=True)
                except ValueError:
                    raise ValueError('Invalid logo image')
                if len(raw) <= 200 * 1024 and raw.startswith(signature):
                    return value
        raise ValueError('Use a PNG/JPEG logo up to 200 KB or an HTTP(S) image URL')

    @field_validator('time_zone')
    @classmethod
    def timezone_exists(cls, value):
        try: ZoneInfo(value)
        except (KeyError, ValueError): raise ValueError('Unknown time zone')
        return value


class Version(Input):
    version: int = Field(ge=1)


class SettingsUpdate(Version):
    data: SettingsData


class CatalogData(Input):
    name: Name
    description: Text = ''
    amount: Money = Decimal('0')
    taxable: bool = True
    fuel_eligible: bool = False
    exclusive_vehicle: bool = False
    payload_kg: Positive | None = None
    volume_m3: Positive | None = None
    length_cm: Positive | None = None
    width_cm: Positive | None = None
    height_cm: Positive | None = None
    pallet_capacity: int = Field(default=0, ge=0, le=100)
    equipment: list[Name] = Field(default_factory=list, max_length=30)
    required_equipment: list[Name] = Field(default_factory=list, max_length=30)
    required_crew: int = Field(default=1, ge=1, le=10)


class CatalogCreate(Input):
    kind: Literal['SERVICE', 'ACCESSORIAL', 'VEHICLE_TYPE']
    code: Annotated[str, Field(min_length=1, max_length=60)]
    data: CatalogData


class CatalogUpdate(Version):
    data: CatalogData


class Zone(Input):
    id: Name
    name: Name
    postal_codes: list[Annotated[str, Field(pattern=r'^[A-Za-z]\d[A-Za-z]( ?\d[A-Za-z]\d)?$')]] = Field(min_length=1)


class WeightBand(Input):
    from_kg: Decimal = Field(ge=0, allow_inf_nan=False)
    to_kg: Positive
    prices: dict[str, Money]

    @model_validator(mode='after')
    def range_valid(self):
        if self.to_kg <= self.from_kg: raise ValueError('Weight To must exceed From')
        return self


class RateData(Input):
    @model_validator(mode='before')
    @classmethod
    def legacy_settlement_flag(cls, data):
        # Retired invoice settlement metadata may still exist in saved Rate Cards.
        return {key:value for key,value in data.items() if key != 'settle_actual'} if isinstance(data, dict) else data

    name: Name
    method: Literal['BASE_PLUS_DISTANCE', 'FIXED', 'ZONE', 'HOURLY', 'IMPORTED']
    base_fee: Money = Decimal('20')
    included_km: Money = Decimal('5')
    per_km: Money = Decimal('1.5')
    fixed_amount: Money = Decimal('55')
    hourly_rate: Money = Decimal('85')
    minimum_minutes: int = Field(default=120, ge=0, le=10080)
    increment_minutes: int = Field(default=30, ge=1, le=1440)
    minimum_subtotal: Money = Decimal('0')
    dimensional_divisor: Positive = Decimal('5000')
    zones: list[Zone] = Field(default_factory=list, max_length=100)
    weight_bands: list[WeightBand] = Field(default_factory=list, max_length=200)
    apply_fuel: bool = True
    apply_service: bool = True
    apply_vehicle: bool = True
    apply_accessorials: bool = True

    @model_validator(mode='after')
    def bands_valid(self):
        ids = [z.id for z in self.zones]
        if len(set(ids)) != len(ids): raise ValueError('Duplicate zone identifiers')
        previous = None
        for band in sorted(self.weight_bands, key=lambda b: b.from_kg):
            if previous is not None and band.from_kg < previous: raise ValueError('Weight ranges overlap')
            if not set(band.prices).issubset(ids): raise ValueError('Price references an unknown zone')
            previous = band.to_kg
        return self


class RateCreate(Input):
    code: Annotated[str, Field(min_length=1, max_length=60)]
    is_default: bool = False
    data: RateData


class RateUpdate(Version):
    is_default: bool = False
    active: bool = True
    data: RateData


class Discount(Input):
    kind: Literal['NONE', 'PERCENT', 'FIXED'] = 'NONE'
    value: Money = Decimal('0')
    @model_validator(mode='after')
    def percentage(self):
        if self.kind == 'PERCENT' and self.value > 100: raise ValueError('Discount cannot exceed 100%')
        return self


class ShipperData(Input):
    name: Name
    kind: Literal['BUSINESS', 'INDIVIDUAL']
    company_name: str = Field(default='', max_length=160)
    email: EmailStr
    phone: Annotated[str, Field(max_length=50)] = ''
    warehouse: Address
    rate_card_id: UUID | None = None
    discount: Discount = Field(default_factory=Discount)
    instructions: Text = ''
    email_updates: bool = True
    @model_validator(mode='after')
    def business(self):
        if self.kind == 'BUSINESS' and not self.company_name: raise ValueError('Company name is required for Business')
        if self.kind == 'INDIVIDUAL' and self.company_name: raise ValueError('Individual does not have a Company name')
        self.email = str(self.email).lower()
        return self


class ShipperUpdate(Version):
    data: ShipperData
    status: Literal['ACTIVE', 'ON_HOLD', 'INACTIVE'] | None = None


class VehicleData(Input):
    name: Name
    unit_number: str = Field(default='', max_length=50)
    make_model: str = Field(default='', max_length=160)
    year: int | None = Field(default=None, ge=1886, le=2100)
    vin: str = Field(default='', max_length=50)
    availability: Literal['AVAILABLE', 'UNAVAILABLE'] = 'AVAILABLE'
    unavailable_reason: str = Field(default='', max_length=500)
    unavailable_from: AwareDatetime | None = None
    unavailable_until: AwareDatetime | None = None
    type_id: UUID
    plate: Annotated[str, Field(min_length=1, max_length=30)]
    province: Annotated[str, Field(min_length=2, max_length=30)]
    payload_kg: Positive
    volume_m3: Positive | None = None
    length_cm: Positive
    width_cm: Positive
    height_cm: Positive
    pallet_capacity: int = Field(default=0, ge=0, le=100)
    maximum_stops: int | None = Field(default=None, ge=1, le=500)
    equipment: list[Name] = Field(default_factory=list, max_length=30)
    description: Text = ''
    active: bool = True


    @model_validator(mode='after')
    def availability_valid(self):
        if self.availability == 'UNAVAILABLE' and not self.unavailable_reason: raise ValueError('Unavailable reason is required')
        if self.unavailable_from and self.unavailable_until and self.unavailable_until <= self.unavailable_from: raise ValueError('Unavailable interval is invalid')
        physical_volume = self.length_cm * self.width_cm * self.height_cm / Decimal(1000000)
        self.volume_m3 = min(self.volume_m3,physical_volume) if self.volume_m3 is not None else physical_volume
        self.unit_number = (self.unit_number.strip() or self.name.strip()).upper()
        if not self.unit_number or len(self.unit_number) > 50:
            raise ValueError('Unit number must contain 1–50 characters')
        return self


class VehicleUpdate(Version):
    data: VehicleData


class DriverData(Input):
    name: Name
    avatar_url: str = Field(default='', max_length=2000)
    email: EmailStr
    phone: Annotated[str, Field(min_length=3, max_length=50)]
    address: Address
    vehicle_id: UUID | None = None
    qualifications: list[Name] = Field(default_factory=list, max_length=30)
    crew_size: int = Field(default=1, ge=1, le=10)
    shift_start: AwareDatetime | None = None
    shift_end: AwareDatetime | None = None
    maximum_work_minutes: int = Field(default=480, ge=1, le=1440)
    active: bool = True
    @model_validator(mode='after')
    def validate_driver(self):
        self.email = str(self.email).lower()
        if (self.shift_start is None) != (self.shift_end is None): raise ValueError('Provide both shift boundaries')
        if self.shift_start and self.shift_end <= self.shift_start: raise ValueError('Shift end must follow start')
        return self


class DriverUpdate(Version):
    data: DriverData
    duty_status: Literal['ON_DUTY', 'OFF_DUTY'] | None = None
    expected_on_duty: bool | None = None
    @model_validator(mode='after')
    def validate_duty_change(self):
        if self.duty_status is not None and self.expected_on_duty is None:
            raise ValueError('Expected duty status is required when editing duty')
        return self


class StopInput(Input):
    id: UUID
    kind: Literal['PICKUP', 'DROPOFF']
    address: Address
    contact_name: str = Field(default='', max_length=160)
    phone: str = Field(default='', max_length=50)
    instructions: Text = ''
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    service_minutes: int = Field(default=10, ge=0, le=480)
    unattended_allowed: bool = False
    photo_required: bool = False
    @model_validator(mode='after')
    def window(self):
        if self.window_start and self.window_end and self.window_end < self.window_start:
            raise ValueError('Time window end precedes start')
        return self


class ItemInput(Input):
    id: UUID
    pickup_id: UUID
    delivery_id: UUID
    quantity: int = Field(ge=1, le=10000)
    weight_kg: Positive
    length_cm: Positive
    width_cm: Positive
    height_cm: Positive
    pallets: int = Field(default=0, ge=0, le=100)
    fragile: bool = False
    dangerous_goods: bool = False
    description: Text = ''


class AccessorialSelection(Input):
    id: UUID
    quantity: Positive = Decimal('1')


class Adjustment(Input):
    amount: Decimal = Field(ge=-10000000, le=10000000, max_digits=16, decimal_places=2)
    reason: Annotated[str, Field(min_length=3, max_length=500)]
    taxable: bool = True


class Booking(Input):
    shipper_id: UUID | None = None
    billing_shipper_id: UUID | None = None
    rate_card_id: UUID | None = None
    service_id: UUID
    vehicle_type_id: UUID | None = None
    preferred_driver_id: UUID | None = None
    scheduled_at: AwareDatetime
    stops: list[StopInput] = Field(min_length=2, max_length=50)
    items: list[ItemInput] = Field(min_length=1, max_length=200)
    accessorials: list[AccessorialSelection] = Field(default_factory=list, max_length=50)
    distance_km: Money | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=10080)
    adjustments: list[Adjustment] = Field(default_factory=list, max_length=20)
    internal_notes: Text = ''
    imported_total: Money | None = None
    imported_tax: Money | None = None
    external_reference: str = Field(default='', max_length=160)
    @model_validator(mode='after')
    def links(self):
        stops = {s.id: s for s in self.stops}
        if len(stops) != len(self.stops) or len({i.id for i in self.items}) != len(self.items):
            raise ValueError('Stop and item identifiers must be unique')
        used = set()
        for item in self.items:
            if item.pickup_id not in stops or stops[item.pickup_id].kind != 'PICKUP': raise ValueError('Invalid item pickup link')
            if item.delivery_id not in stops or stops[item.delivery_id].kind != 'DROPOFF': raise ValueError('Invalid item delivery link')
            used.update([item.pickup_id, item.delivery_id])
        if used != set(stops): raise ValueError('Every stop must be linked to cargo')
        if len({a.id for a in self.accessorials}) != len(self.accessorials): raise ValueError('Duplicate Accessorial')
        return self


class Assignment(Version):
    driver_id: UUID
    vehicle_id: UUID
    route_id: UUID | None = None
    route_version: int | None = Field(default=None, ge=1)
    planned_at: AwareDatetime


class RouteCommand(Version):
    generation: int = Field(ge=1)


class ArrivalCommand(RouteCommand):
    captured_at: AwareDatetime


class StopCommand(RouteCommand):
    quantities: dict[UUID, int]
    recipient_name: str = Field(default='', max_length=160)
    unattended: bool = False
    safe_placement: bool = False
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=10)
    captured_at: AwareDatetime


class EvidenceInput(Input):
    kind: Literal['PHOTO', 'SIGNATURE']
    captured_at: AwareDatetime
    media_type: Literal['image/png', 'image/jpeg']
    content_base64: str = Field(min_length=8, max_length=2800000)
    @model_validator(mode='after')
    def image(self):
        try: content = base64.b64decode(self.content_base64, validate=True)
        except ValueError: raise ValueError('Invalid base64 evidence')
        if len(content) > 2_000_000: raise ValueError('Evidence exceeds 2 MB')
        if self.media_type == 'image/png' and not content.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('Invalid PNG')
        if self.media_type == 'image/jpeg' and not content.startswith(b'\xff\xd8\xff'): raise ValueError('Invalid JPEG')
        return self


class DutyStart(Input):
    location_permission: Literal['GRANTED', 'DENIED', 'UNKNOWN'] | None = None


class DutyEnd(Version):
    ended_at: AwareDatetime


class Telemetry(Input):
    duty_id: UUID
    captured_at: AwareDatetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: Positive
    location_permission: Literal['GRANTED', 'DENIED', 'UNKNOWN']


class IssueInput(Input):
    stop_id: UUID | None = None
    kind: Literal['FAILED_PICKUP', 'FAILED_DELIVERY', 'SHORT_LOAD', 'WRONG_ADDRESS', 'UNSAFE', 'VEHICLE', 'OTHER']
    description: Annotated[str, Field(min_length=3, max_length=2000)]


class IssueResolve(Version):
    resolution: Annotated[str, Field(min_length=3, max_length=2000)]


class RecordView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    version: int
    created_at: datetime


class SettingsView(RecordView):
    data: SettingsData


class CatalogView(RecordView):
    kind: str
    code: str
    active: bool
    data: CatalogData


class RateView(RecordView):
    code: str
    active: bool
    is_default: bool
    data: RateData


class ChargeLine(BaseModel):
    group: str
    label: str
    amount: Decimal
    taxable: bool
    fuel_eligible: bool = False
    taxable_base: Decimal | None = None
    rate_percent: Decimal | None = None


class PriceView(BaseModel):
    status: Literal['PRICED', 'NEEDS_ATTENTION'] = 'PRICED'
    review_reason: str | None = None
    currency: str = 'CAD'
    method: str | None = None
    rate_card_id: UUID | None = None
    rate_card_version: int | None = None
    lines: list[ChargeLine] = Field(default_factory=list)
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    expires_at: datetime | None = None
    stage: Literal['ESTIMATE', 'FINAL']
    context: dict = Field(default_factory=dict)


class OrderView(RecordView):
    number: str
    shipper_id: UUID
    billing_shipper_id: UUID
    service_id: UUID
    route_id: UUID | None
    source: str
    status: str
    scheduled_at: datetime
    completed_at: datetime | None
    facts: Booking
    booking: dict
    pricing: PriceView


class DriverNotificationView(RecordView):
    title: str
    body: str
    order_id: UUID | None
    route_id: UUID | None
    read_at: datetime | None


class QuoteSend(Version):
    recipient: EmailStr


class EmailDeliveryView(RecordView):
    quote_id: UUID | None
    notification_id: UUID | None = None
    recipient: EmailStr
    status: Literal['PENDING', 'SENDING', 'SENT', 'FAILED', 'UNKNOWN']
    sent_at: datetime | None
    error_code: str | None


class OrderUpdate(Version):
    booking: Booking


class ShipperView(RecordView):
    name: str
    number: str
    status: str
    kind: str
    company_name: str
    email: str
    phone: str
    warehouse: Address
    rate_card_id: UUID | None
    rate_card_name: str | None = None
    discount: Discount
    instructions: str
    email_updates: bool = True
    archived_at: datetime | None = None
    initial_password: str | None = None


class VehicleView(RecordView):
    number: str
    archived_at: datetime | None = None
    type_id: UUID
    plate: str
    province: str
    active: bool
    data: VehicleData


class DriverView(RecordView):
    archived_at: datetime | None = None
    number: str
    name: str
    email: str
    phone: str
    address: Address
    service_city: str
    active: bool
    vehicle_id: UUID | None
    data: dict
    last_seen_at: datetime | None
    location_permission: str
    on_duty: bool
    initial_password: str | None = None


class RouteVisitView(RecordView):
    route_id: UUID
    stop_id: UUID
    position: int
    status: str
    planned_at: datetime
    arrived_at: datetime | None
    completed_at: datetime | None
    movements: dict
    stop: StopInput


class RouteView(RecordView):
    driver_id: UUID
    vehicle_id: UUID
    status: str
    locked: bool
    generation: int
    planned_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    plan: dict
    stops: list[RouteVisitView]
    items: list[ItemInput] | None = None
    vehicle: dict | None = None


class AssignmentView(BaseModel):
    order_id: UUID
    order_version: int
    route: RouteView


class QuoteView(RecordView):
    facts: Booking
    pricing: PriceView


class IssueView(RecordView):
    order_id: UUID
    stop_id: UUID | None
    kind: str
    description: str
    resolved: bool
    resolution: str


class DutyView(RecordView):
    driver_id: UUID
    started_at: datetime
    ended_at: datetime | None


class ShipperProfileUpdate(Version):
    contact_name: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    phone: Annotated[str, Field(max_length=50)] = ''
    warehouse: Address

class DriverProfileView(BaseModel):
    id: UUID
    version: int
    number: str
    name: str
    email: str
    phone: str
    address: Address
    service_city: str
    vehicle_id: UUID | None
    duty: DutyView | None
    location_permission: str
    vehicle_name: str | None = None


class DriverProfileUpdate(Version):
    phone: Annotated[str, Field(min_length=3, max_length=50)]


class DriverOrderStop(BaseModel):
    id: UUID
    kind: str
    address: Address
    contact_name: str
    phone: str
    instructions: str
    window_start: datetime | None
    window_end: datetime | None
    unattended_allowed: bool
    photo_required: bool


class DriverShipperContact(BaseModel):
    name: str
    company_name: str
    phone: str
    email: EmailStr | Literal['']


class DriverOrderView(BaseModel):
    """Driver projection of an assigned Order: no price, payer, billing or private notes."""
    id: UUID
    number: str
    status: str
    scheduled_at: datetime
    completed_at: datetime | None
    route_id: UUID
    route_status: str
    service_name: str
    shipper: DriverShipperContact
    stops: list[DriverOrderStop]
    items: list[ItemInput]


class DriverActivityView(BaseModel):
    completed_orders: int
    app_connectivity: str
    app_last_seen: datetime | None
    gps_captured: datetime | None
    location_permission: str
    current_route: UUID | None


class MonitorDriver(BaseModel):
    id: UUID
    name: str
    vehicle_id: UUID | None
    on_duty: bool
    location: Telemetry | None
    gps_status: str
    app_last_seen: datetime | None
    location_permission: str


class MonitorOrder(BaseModel):
    id: UUID
    number: str
    status: str
    route_id: UUID | None
    pricing_status: str
    review_reason: str | None
    attention_flags: list[str] = Field(default_factory=list)


class MonitorRoute(BaseModel):
    id: UUID
    driver_id: UUID
    vehicle_id: UUID
    status: str
    plan: dict


class MonitorIssue(BaseModel):
    id: UUID | None = None
    order_id: UUID
    kind: str
    description: str


class MonitorView(BaseModel):
    drivers: list[MonitorDriver]
    orders: list[MonitorOrder]
    routes: list[MonitorRoute]
    needs_attention: list[MonitorIssue]


class DailyMetric(BaseModel):
    date: str
    orders: int
    completed: int
    on_time: int = 0
    late: int = 0
    not_measured: int = 0


class AnalyticsRow(BaseModel):
    id: UUID
    number: str
    scheduled_at: datetime
    status: str
    source: str
    shipper_id: UUID
    driver_id: UUID | None
    shipper_name: str = ''
    driver_name: str = ''
    driver_number: str = ''
    vehicle_unit: str = ''
    service_name: str = ''
    delivered_at: datetime | None = None
    delivery_outcome: Literal['ON_TIME', 'LATE', 'NOT_MEASURED'] = 'NOT_MEASURED'
    pod_verified: bool = False
    open_issues: int = 0


class AnalyticsView(BaseModel):
    pod_verified_orders: int
    on_time_orders: int
    sla_known_orders: int
    on_time_percent: float | None
    orders: int
    statuses: dict[str, int]
    completed_orders: int
    open_issues: int
    source_counts: dict[str, int]
    daily: list[DailyMetric]
    rows: list[AnalyticsRow]


class ProofEvidence(BaseModel):
    id: UUID
    kind: str
    captured_at: datetime


class DeliveryProofView(BaseModel):
    stop_id: UUID
    address: Address
    completed_at: datetime
    recipient_name: str
    unattended: bool
    completed_by_dispatcher: bool = False
    evidence: list[ProofEvidence]


class TrackingStop(BaseModel):
    id: UUID
    kind: str
    address: Address
    window_start: datetime | None
    window_end: datetime | None
    planned_at: datetime | None
    eta: datetime | None
    arrived_at: datetime | None
    completed_at: datetime | None
    status: str


class TrackingEvent(BaseModel):
    kind: str
    label: str
    at: datetime


class TrackingDriver(BaseModel):
    first_name: str
    vehicle_type: str | None


class TrackingLocation(BaseModel):
    latitude: float
    longitude: float
    accuracy_m: float | None
    captured_at: datetime


class TrackingView(BaseModel):
    """Order tracking for its Shipper or a dispatcher; `location` is present only while `live` and fresh."""
    order_id: UUID
    status: str
    stage: Literal['BOOKED', 'ASSIGNED', 'TO_PICKUP', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', 'DELIVERED', 'CANCELLED']
    dedicated: bool
    driver: TrackingDriver | None
    stops: list[TrackingStop]
    stops_before_next: int | None
    eta: datetime | None
    delay_minutes: int
    late: bool
    live: bool
    location: TrackingLocation | None
    location_stale: bool
    events: list[TrackingEvent]
    open_issue: bool
    updated_at: datetime


class RoadPathView(BaseModel):
    """Driving path through an Order's stops as [latitude, longitude] points, for map display only."""
    points: list[tuple[float, float]]


class BookingDriverView(BaseModel):
    """Shipper-safe driver choice: identity and display name only."""
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str


class BookingPreferences(BaseModel):
    currency: Literal['CAD', 'USD']
    time_zone: str
    weight_unit: Literal['lb', 'kg']
    dimension_unit: Literal['in', 'cm']
    distance_unit: Literal['km', 'mi']
    # Company tax and fuel terms that apply to the booking actor's own Orders; the API still prices every Order.
    gst_enabled: bool
    gst_percent: Percent
    provincial_enabled: bool
    provincial_percent: Percent
    fuel_enabled: bool
    fuel_percent: Percent
    default_service_id: UUID | None = None
