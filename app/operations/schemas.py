"""Versioned operational requests; canonical units are kg, cm, km and CAD."""
import base64
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, EmailStr, AwareDatetime, StrictBool, field_validator, model_validator
from app import regions
from app.schemas import Input, Name, Password

Money = Annotated[Decimal, Field(ge=0, le=10000000, max_digits=16, decimal_places=4)]
Positive = Annotated[Decimal, Field(gt=0, le=10000000, allow_inf_nan=False)]
Percent = Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)]
Text = Annotated[str, Field(max_length=2000)]
# Full truckload, less than truckload, small parcel.
LoadSize = Literal['FTL', 'LTL', 'SMALL_PARCEL']


Fsa = Annotated[str, BeforeValidator(regions.area), Field(pattern=regions.AREA_PATTERN)]


def validate_logo(value: str) -> str:
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


from .stop_reasons import Reason, ReasonCode, Responsibility, Charge, Stops, BY_VALUE, CUSTOM_PREFIX

class Address(Input):
    text: Annotated[str, Field(min_length=5, max_length=500)]
    city: Name
    province: Annotated[str, Field(min_length=2, max_length=30)]
    postal_code: Annotated[str, Field(pattern=regions.POSTAL_PATTERN)]
    country: Literal['CA', 'US'] = 'CA'
    latitude: Annotated[float, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[float, Field(ge=-180, le=180)] | None = None

    @model_validator(mode='before')
    @classmethod
    def infer_country(cls, data):
        """Older clients omit country; a US ZIP code then identifies a US address."""
        if isinstance(data, dict) and not data.get('country') and regions.postal(data.get('postal_code'), 'US'):
            return {**data, 'country': 'US'}
        return data

    @model_validator(mode='after')
    def coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError('Provide both coordinates or neither')
        if not regions.postal(self.postal_code, self.country):
            raise ValueError('Use a Canadian postal code such as V6A 1A1' if self.country == 'CA' else 'Use a US ZIP code such as 98101')
        if self.country == 'US' and self.province.strip().upper() not in regions.STATES:
            raise ValueError('Use the two-letter US state code, such as WA')
        return self


class DispatchZone(Input):
    name: Name
    postal_codes: list[Fsa] = Field(min_length=1, max_length=200)


WEEKDAYS = ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')
ClockTime = Annotated[str, Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')]


class WorkDay(Input):
    """One weekday of company work hours, in the company time zone; same-day hours only, 23:59 meaning end of day."""
    day: Literal['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']
    open: StrictBool
    start: ClockTime = '08:00'
    end: ClockTime = '17:00'

    @model_validator(mode='after')
    def ordered(self):
        if self.open and self.end <= self.start: raise ValueError('Work hours must end after they start')
        return self


class Holiday(Input):
    date: date
    name: Name


def default_work_hours():
    # Companies that have not saved hours book and auto-dispatch pickups 06:00–19:00, every day.
    return [WorkDay(day=day, open=True, start='06:00', end='19:00') for day in WEEKDAYS]


class ReasonPolicy(Input):
    """One driver reason in a company's complete list: a built-in (code = its value, base ignored) or a CUSTOM_ reason on a built-in base."""
    code: ReasonCode
    base: Reason | None = None
    label: Annotated[str, Field(min_length=2, max_length=60)]
    stops: Stops = 'BOTH'
    responsibility: Responsibility
    charge: Charge

    @model_validator(mode='after')
    def known_base(self):
        self.label = self.label.strip()
        if self.code == 'NO_PROBLEM': raise ValueError('Progress is normal is not a stop reason')
        if self.code in BY_VALUE: self.base = None
        elif not self.code.startswith(CUSTOM_PREFIX): raise ValueError('Custom reason codes start with CUSTOM_')
        elif self.base is None or self.base == 'NO_PROBLEM': raise ValueError('Choose what a custom reason means')
        return self


def default_reason_policies():
    """Dispatra's built-in reasons with their defaults, so every company sees and edits a complete list."""
    from .stop_reasons import DEFAULT
    return [ReasonPolicy(code=r.code, label=r.label, stops=r.stops, responsibility=r.responsibility, charge=r.charge) for r in DEFAULT.rows.values()]


class SettingsData(Input):
    company_name: Name = 'Dispatra Logistics'
    tms_workspace: StrictBool = False
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
    # Extra time: the first N minutes at each stop are free, then each started N minutes costs the amount; 0 disables.
    extra_time_minutes: int = Field(default=30, ge=5, le=480)
    extra_time_amount: Money = Decimal('50')
    # The company's complete driver reason list (see stop_reasons); a reason left out is deleted. Order is fixed on read.
    stop_reasons: list[ReasonPolicy] = Field(default_factory=default_reason_policies, min_length=1, max_length=60)
    # Planned time at each stop by load size; bookings that omit a stop's service minutes use it (routing, Hourly prices, Order Watch).
    stop_minutes_small_parcel: int = Field(default=10, ge=0, le=480)
    stop_minutes_ltl: int = Field(default=20, ge=0, le=480)
    stop_minutes_ftl: int = Field(default=45, ge=0, le=480)
    # Legacy, not enforced: kept so saved settings and older clients still validate. Vehicle Maximum stops limits Routes.
    maximum_active_orders: int = Field(default=3, ge=1, le=100)
    quote_validity_days: int = Field(default=14, ge=1, le=365)
    # AUTO: the Dispatch agent assigns priced, unassigned Orders inside its horizon; MANUAL: dispatchers assign, the agent only recommends.
    dispatch_mode: Literal['AUTO', 'MANUAL'] = 'MANUAL'
    dispatch_zones: list[DispatchZone] = Field(default_factory=list, max_length=100)
    # Company operating calendar: Shippers book pickups inside it and AUTO dispatch holds pickups outside it.
    work_hours: list[WorkDay] = Field(default_factory=default_work_hours, min_length=7, max_length=7)
    holidays: list[Holiday] = Field(default_factory=list, max_length=100)
    # Manual new-order forms start on this active SERVICE; email intake resolves service from requested facts.
    default_service_id: UUID | None = None
    # Email intake falls back to this active PACKAGE_TYPE when it cannot infer an item's type.
    default_package_type_id: UUID | None = None

    @model_validator(mode='after')
    def dispatch_zone_unique(self):
        names = [z.name.casefold() for z in self.dispatch_zones]
        if len(set(names)) != len(names): raise ValueError('Dispatch zone names must be unique')
        prefixes = [(p.replace(' ', '').upper(), i) for i,z in enumerate(self.dispatch_zones) for p in z.postal_codes]
        if any(i != j and (a.startswith(b) or b.startswith(a)) for a,i in prefixes for b,j in prefixes):
            raise ValueError('Dispatch zones must not have overlapping postal prefixes')
        return self

    @model_validator(mode='before')
    @classmethod
    def earlier_reason_fields(cls, data):
        # Saved by the first Stop reasons page (2026-11-26): switched-off reasons were deleted ones; per-reason notes,
        # photo and active flags and the review amount are gone.
        if not isinstance(data, dict): return data
        data = {k: v for k, v in data.items() if k != 'extra_time_review_above'}
        rows = data.get('stop_reasons')
        if isinstance(rows, list) and any(isinstance(row, dict) and 'active' in row for row in rows):
            keep = [{k: v for k, v in row.items() if k not in ('active', 'notes_required', 'photo_required')}
                for row in data['stop_reasons'] if isinstance(row, dict) and row.get('active') is not False]
            data['stop_reasons'] = keep or None
            if data['stop_reasons'] is None: data.pop('stop_reasons')
        return data

    @model_validator(mode='after')
    def reason_list(self):
        codes = [r.code for r in self.stop_reasons]
        if len(set(codes)) != len(codes): raise ValueError('Each reason can be listed once')
        labels = [r.label.casefold() for r in self.stop_reasons]
        if len(set(labels)) != len(labels): raise ValueError('Reason names must be unique')
        from .stop_reasons import place
        self.stop_reasons.sort(key=place)
        return self

    @model_validator(mode='after')
    def operating_calendar(self):
        if tuple(d.day for d in self.work_hours) != WEEKDAYS: raise ValueError('Work hours need each weekday once, Monday to Sunday')
        dates = [h.date for h in self.holidays]
        if len(set(dates)) != len(dates): raise ValueError('Each holiday date can be added once')
        self.holidays.sort(key=lambda h: h.date)
        return self

    @field_validator('logo_url')
    @classmethod
    def company_logo(cls, value):
        return validate_logo(value)

    @field_validator('time_zone')
    @classmethod
    def timezone_exists(cls, value):
        try: ZoneInfo(value)
        except (KeyError, ValueError): raise ValueError('Unknown time zone')
        return value


class Version(Input):
    version: int = Field(ge=1)


class DirectoryPasswordChange(Version):
    new_password: Password


class DirectoryPasswordChanged(BaseModel):
    id: UUID
    version: int


class SettingsUpdate(Version):
    data: SettingsData


class TruckRoutingProfile(Input):
    """Exterior envelope and conservative loaded weight, separate from cargo capacity."""
    length_m: Annotated[Decimal, Field(gt=0, le=50, allow_inf_nan=False)]
    width_m: Annotated[Decimal, Field(gt=0, le=10, allow_inf_nan=False)]
    height_m: Annotated[Decimal, Field(gt=0, le=10, allow_inf_nan=False)]
    gross_weight_kg: int = Field(strict=True, gt=0, le=200000)
    axle_weight_kg: int | None = Field(default=None, strict=True, gt=0, le=100000)

    @model_validator(mode='after')
    def axle_valid(self):
        if self.axle_weight_kg and self.axle_weight_kg > self.gross_weight_kg:
            raise ValueError('Axle weight cannot exceed loaded gross weight')
        return self


class CatalogData(Input):
    name: Name
    description: Text = ''
    amount: Money = Decimal('0')
    taxable: bool = True
    fuel_eligible: bool = False
    exclusive_vehicle: bool = False
    road_profile: TruckRoutingProfile | None = Field(default=None, description='Required when creating or saving a Vehicle Type. Nullable only for legacy reads and other catalogue kinds.')
    payload_kg: Positive | None = None
    volume_m3: Positive | None = None
    length_cm: Positive | None = None
    width_cm: Positive | None = None
    height_cm: Positive | None = None
    default_weight_kg: Positive | None = Field(default=None, description='Package Type only: per-piece weight that prefills a new package; its length, width and height prefill the dimensions.')
    pallet_capacity: int = Field(default=0, ge=0, le=100)
    equipment: list[Name] = Field(default_factory=list, max_length=30)
    required_equipment: list[Name] = Field(default_factory=list, max_length=30)
    required_crew: int = Field(default=1, ge=1, le=10)
    load_sizes: list[Literal['FTL', 'LTL']] = Field(default_factory=lambda: ['FTL', 'LTL'], min_length=1, max_length=2,
        description='Vehicle Type only: the load sizes an order may require it for. Records saved before this field read as both.')

    @model_validator(mode='after')
    def road_weight_valid(self):
        if self.road_profile and self.payload_kg is not None and self.road_profile.gross_weight_kg < self.payload_kg:
            raise ValueError('Loaded gross weight must include at least the vehicle type payload capacity')
        if len(set(self.load_sizes)) != len(self.load_sizes): raise ValueError('Duplicate load size')
        return self


class CatalogCreate(Input):
    kind: Literal['SERVICE', 'ACCESSORIAL', 'VEHICLE_TYPE', 'PACKAGE_TYPE']
    code: Annotated[str, Field(min_length=1, max_length=60)]
    data: CatalogData


class CatalogUpdate(Version):
    data: CatalogData


class Zone(Input):
    id: Name
    name: Name
    postal_codes: list[Fsa] = Field(min_length=1)


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
    load_size: LoadSize = Field(default='LTL', description='The one load size this card prices. Cards saved before this field read as LTL.')

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
    # With `rate_card_ids` (in the dispatcher's order) this is the default card among them; alone it is a legacy single card.
    rate_card_id: UUID | None = None
    rate_card_ids: list[UUID] | None = Field(default=None, max_length=20, description='Attached Rate Cards, at most one per load size.')
    assigned_driver_id: UUID | None = None
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
    # Nullable for historical records; registration requires this in save_vehicle.
    parking_address: Address | None = None
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
    road_profile: TruckRoutingProfile | None = None
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
        if self.road_profile and self.road_profile.gross_weight_kg < self.payload_kg:
            raise ValueError('Loaded gross weight must include at least the vehicle payload capacity')
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
    load_sizes: list[LoadSize] = Field(default_factory=lambda: ['FTL', 'LTL', 'SMALL_PARCEL'], min_length=1, max_length=3,
        description='Load sizes the driver handles; an Order of another size cannot be assigned to them. Drivers saved before this field handle all three.')
    shift_start: AwareDatetime | None = None
    shift_end: AwareDatetime | None = None
    maximum_work_minutes: int = Field(default=480, ge=1, le=1440)
    active: bool = True
    @model_validator(mode='after')
    def validate_driver(self):
        self.email = str(self.email).lower()
        if (self.shift_start is None) != (self.shift_end is None): raise ValueError('Provide both shift boundaries')
        if len(set(self.load_sizes)) != len(self.load_sizes): raise ValueError('Duplicate load size')
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
    service_minutes: int = Field(default=10, ge=0, le=480, description='Planned minutes at the stop. Omit to use the company planned stop time for the load size.')
    unattended_allowed: bool = False
    photo_required: bool = False
    liftgate: bool = Field(default=False, description='A power liftgate is needed at this stop; a pickup adds the active LIFTGATE and a drop-off the LIFTGATE_DROPOFF Accessorial, each once per Order with its vehicle equipment.')
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
    # Weight and sizes are optional only on a full truckload, which takes the vehicle type's payload and cargo space.
    weight_kg: Positive | None = None
    length_cm: Positive | None = None
    width_cm: Positive | None = None
    height_cm: Positive | None = None
    pallets: int = Field(default=0, ge=0, le=100)
    fragile: bool = False
    dangerous_goods: bool = False
    description: Text = ''
    package_type_id: UUID | None = Field(default=None, description='Company Package Type (Envelope, Parcel, Pallet, Oversized…). Describes the item; it does not change price.')


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
    # Older bookings have no load size; the API suggests one when it is omitted.
    load_size: LoadSize | None = None
    service_id: UUID | None = None
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

    @model_validator(mode='after')
    def load_fields(self):
        """FTL needs a vehicle type and no service level, package weights or sizes; LTL and Small parcel need all of them."""
        if self.load_size == 'FTL':
            if self.vehicle_type_id is None: raise ValueError('Choose a vehicle type for a full truckload')
            return self
        if self.service_id is None: raise ValueError('Choose a service level')
        if any(i.weight_kg is None for i in self.items): raise ValueError('Enter the weight of every package')
        if any(None in (i.length_cm, i.width_cm, i.height_cm) for i in self.items): raise ValueError('Enter length, width and height for every package')
        return self


class Assignment(Version):
    allow_live: StrictBool = False
    driver_id: UUID
    vehicle_id: UUID
    route_id: UUID | None = None
    route_version: int | None = Field(default=None, ge=1)
    planned_at: AwareDatetime


class RouteCommand(Version):
    generation: int = Field(ge=1)


class RouteStartCommand(RouteCommand):
    supports_live_updates: StrictBool = False


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
    """A stop report. Current drivers send `reasons` (the company's list) and notes; the kind and description follow.
    Older clients send a kind and description only."""
    stop_id: UUID | None = None
    kind: Literal['FAILED_PICKUP', 'FAILED_DELIVERY', 'SHORT_LOAD', 'WRONG_ADDRESS', 'UNSAFE', 'VEHICLE', 'OTHER'] | None = None
    description: Annotated[str, Field(max_length=2000)] = ''
    # Codes from the company's reason list (GET /driver/report-reasons); the company's rules are checked when filed.
    reasons: list[ReasonCode] = Field(default_factory=list, max_length=15)
    photo_ids: list[UUID] = Field(default_factory=list, max_length=4)

    @model_validator(mode='after')
    def report(self):
        if len(set(self.reasons)) != len(self.reasons) or len(set(self.photo_ids)) != len(self.photo_ids): raise ValueError('Duplicate reason or photo')
        if 'NO_PROBLEM' in self.reasons: raise ValueError('Choose what went wrong at the stop')
        if not self.reasons:
            if self.kind is None or len(self.description.strip()) < 3: raise ValueError('Choose at least one reason')
            return self
        return self


class IssueResolve(Version):
    resolution: Annotated[str, Field(min_length=3, max_length=2000)]


class IssueVersion(Version):
    id: UUID


class IssueBatch(Input):
    """Several open issues closed with one note; each keeps its own version check."""
    issues: Annotated[list[IssueVersion], Field(min_length=1, max_length=200)]
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


class ExtraTimeStop(BaseModel):
    stop_id: UUID
    stop: str
    minutes_over: int
    amount: Decimal
    reasons: list[str] = Field(default_factory=list)
    responsibility: Literal['SHIPPER', 'CARRIER', 'UNDECIDED']
    # CHARGED automatically; EXCLUDED for a carrier or never-charged reason; REVIEW waits for a dispatcher (a reason
    # set to review); WAIVED or CHARGED once a dispatcher decides.
    status: Literal['CHARGED', 'EXCLUDED', 'REVIEW', 'WAIVED']
    decided_by_dispatcher: bool = False


class ExtraTimeView(BaseModel):
    minutes_free: int
    amount: Decimal
    stops: list[ExtraTimeStop]


class ExtraTimeDecision(Version):
    stop_id: UUID
    charge: StrictBool


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
    extra_time: ExtraTimeView | None = None


class OrderView(RecordView):
    number: str
    shipper_id: UUID
    billing_shipper_id: UUID
    service_id: UUID | None
    route_id: UUID | None
    source: str
    status: str
    scheduled_at: datetime
    completed_at: datetime | None
    archived_at: datetime | None = None
    facts: Booking
    booking: dict
    pricing: PriceView


class DriverNotificationView(RecordView):
    conversation_id: UUID | None = None
    title: str
    body: str
    order_id: UUID | None
    route_id: UUID | None
    read_at: datetime | None


class QuoteSend(Version):
    recipient: EmailStr


class EmailDeliveryView(RecordView):
    quote_id: UUID | None
    intake_id: UUID | None = None
    notification_id: UUID | None = None
    recipient: EmailStr
    status: Literal['PENDING', 'SENDING', 'SENT', 'FAILED', 'UNKNOWN']
    sent_at: datetime | None
    error_code: str | None


class OrderUpdate(Version):
    booking: Booking


class ShipperView(RecordView):
    logo_url: str = ''
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
    rate_card_ids: list[UUID] = Field(default_factory=list)
    rate_card_names: list[str] = Field(default_factory=list)
    rate_card_load_sizes: list[LoadSize] = Field(default_factory=list, description='Load size of each attached card, in the same order; the first is the default card\'s, which new Orders start with.')
    assigned_driver_id: UUID | None = None
    assigned_driver_name: str | None = None
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


class DriverMapOrigin(BaseModel):
    latitude: float
    longitude: float
    source: Literal['GPS', 'PARKING']


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


class DriverRouteView(RouteView):
    map_origin: DriverMapOrigin | None = None


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
    reasons: list[str] = Field(default_factory=list)
    photo_ids: list[UUID] = Field(default_factory=list)
    holds_completion: bool = True


class ReasonChoice(BaseModel):
    """A reason the driver can choose, with the company's rules for it."""
    value: str
    label: str
    stops: Literal['BOTH', 'PICKUP', 'DROPOFF'] = 'BOTH'
    notes_required: bool = False
    photo_required: bool = False


class DutyView(RecordView):
    driver_id: UUID
    started_at: datetime
    ended_at: datetime | None


class ShipperProfileUpdate(Version):
    logo_url: str = Field(default='', max_length=280000)

    @field_validator('logo_url')
    @classmethod
    def shipper_logo(cls, value):
        return validate_logo(value)

    contact_name: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    phone: Annotated[str, Field(max_length=50)] = ''
    warehouse: Address

class DriverProfileView(BaseModel):
    time_zone: str
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
    liftgate: bool = False


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
    load_size: LoadSize | None = None
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


class TrafficStopEstimate(BaseModel):
    stop_id: UUID
    eta: AwareDatetime
    traffic_added_seconds: int = Field(ge=0)
    road_distance_metres: int = Field(ge=0)


class TrafficEstimateView(BaseModel):
    source: str = Field(max_length=100)
    observed_at: AwareDatetime
    captured_at: AwareDatetime
    truck_profile_applied: bool = False
    stops: list[TrafficStopEstimate] = Field(max_length=25)


class MonitorRoute(BaseModel):
    id: UUID
    driver_id: UUID
    vehicle_id: UUID
    status: str
    plan: dict
    estimate: TrafficEstimateView | None = None


class MonitorIssue(BaseModel):
    # `id` and `version` are set only for reported Issues, which a dispatcher resolves with POST /issues/{id}/resolve.
    id: UUID | None = None
    version: int | None = None
    order_id: UUID
    kind: str
    description: str
    # When the condition was recorded; the list is sorted newest first.
    at: datetime
    # Driver report photos (GET /evidence/{id}); only reported Issues have them.
    photo_ids: list[UUID] = Field(default_factory=list)


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
    load_size: LoadSize | None = None
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


class ProgressUpdateView(BaseModel):
    """One entry of an Order's driver progress feed for dispatch: a progress question with the driver's answer, or a stop report.
    Photos are internal operations evidence; shippers never see them."""
    id: UUID
    kind: Literal['QUESTION', 'REPORT']
    at: datetime
    stop_kind: Literal['PICKUP', 'DROPOFF'] | None
    address: str
    prompt: str
    # QUESTION: OPEN, ANSWERED or CLOSED; REPORT: OPEN or RESOLVED.
    status: Literal['OPEN', 'ANSWERED', 'CLOSED', 'RESOLVED']
    reasons: list[str]
    responsibility: Literal['SHIPPER', 'CARRIER', 'UNDECIDED'] | None
    notes: str
    photo_ids: list[UUID]
    replied_at: datetime | None = None
    estimated_ready_at: datetime | None = None
    resolution: str = ''


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
    name: str = ''
    avatar_url: str = ''
    phone: str = ''


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
    """Order road path or dispatcher route approach as [latitude, longitude] points, for map display only."""
    points: list[tuple[float, float]]
    preview: bool = Field(default=False, description='Unassigned map preview only; not truck feasibility or dispatch evidence.')
    provider: Literal['GOOGLE', 'TOMTOM', 'OPENROUTESERVICE'] = 'GOOGLE'
    truck_profile_applied: bool = False
    attribution: str = ''


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
    # Shippers book pickups only inside these hours.
    work_hours: list[WorkDay]
    holidays: list[Holiday]
    default_service_id: UUID | None = None
    # Prefills new packages (Small parcel, and LTL when the company has no Pallet type).
    default_package_type_id: UUID | None = None
