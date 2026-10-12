from datetime import datetime
from uuid import UUID
from typing import Annotated, Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.schemas import Input
from . import fields

Endpoint = Annotated[str, Field(min_length=1, max_length=500, pattern=r'^/[^\s#?]*$')]
Secret = Annotated[str, Field(min_length=8, max_length=2000)]
Record = Literal['driver', 'vehicle', 'order']


class TmsInput(Input):
    """`credential` is required the first time and `webhook_secret` the first time webhooks are used; omit either to keep the saved value."""
    enabled: bool = True
    base_url: Annotated[str, Field(min_length=9, max_length=2048)]
    auth_type: Literal['api_key', 'bearer'] = 'api_key'
    credential: Secret | None = None
    drivers_endpoint: Endpoint = '/drivers'
    vehicles_endpoint: Endpoint = '/vehicles'
    fleet_sync_minutes: Literal[15, 60, 360, 1440] = 60
    sync_method: Literal['webhook', 'polling'] = 'webhook'
    webhook_secret: Annotated[str, Field(min_length=32, max_length=200)] | None = None
    orders_endpoint: Endpoint = '/orders'
    poll_interval_minutes: Literal[1, 5, 15] = 5
    status_endpoint: Endpoint = '/orders/{id}/status'
    field_maps: dict[Record, dict[str, Annotated[str, Field(max_length=200)]]] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)

    @field_validator('base_url')
    @classmethod
    def https(cls, value: str):
        parts = urlsplit(value.strip())
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment or parts.query:
            raise ValueError('Enter a complete HTTPS URL without credentials, query or fragment.')
        return value.strip().rstrip('/')

    @field_validator('status_endpoint')
    @classmethod
    def order_specific(cls, value: str):
        if '{id}' not in value: raise ValueError('The status endpoint must contain {id} for the TMS order ID.')
        return value

    @model_validator(mode='after')
    def maps(self):
        self.field_maps = {record: {key: path.strip() for key, path in mapping.items() if path.strip()} for record, mapping in self.field_maps.items()}
        errors = [problem for record, mapping in self.field_maps.items() for problem in fields.problems(record, mapping)]
        if errors: raise ValueError('; '.join(errors[:5]))
        return self


class TmsView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    enabled: bool
    base_url: str
    auth_type: str
    drivers_endpoint: str
    vehicles_endpoint: str
    fleet_sync_minutes: int
    sync_method: str
    orders_endpoint: str
    poll_interval_minutes: int
    status_endpoint: str
    field_maps: dict[str, dict[str, str]]
    credential_configured: bool = True
    webhook_secret_configured: bool = False
    missing_fields: dict[str, list[str]] = Field(default_factory=dict)
    last_fleet_sync_at: datetime | None
    last_fleet_result: dict | None
    last_order_poll_at: datetime | None
    last_error: str | None
    last_status_at: datetime | None = None
    last_status_error: str | None = None
    version: int


class TmsCheckResult(BaseModel):
    ok: bool
    message: str


class SyncCounts(BaseModel):
    created: int
    updated: int
    unchanged: int
    skipped: int
    archived: int


class SyncIssue(BaseModel):
    kind: str
    id: str
    reason: str


class FleetSyncResult(BaseModel):
    drivers: SyncCounts | None
    vehicles: SyncCounts | None
    issues: list[SyncIssue]
    error: str | None


class InboxView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    external_id: str
    received_via: str
    received_at: datetime
    status: str
    missing: list[str]
    error_code: str | None
    order_id: UUID | None
    order_number: str | None = None
    version: int


class WebhookReceipt(BaseModel):
    accepted: int
    rejected: int


class ApiWebhookInput(Input):
    url: Annotated[str, Field(min_length=9, max_length=2048)]
    secret: Annotated[str, Field(min_length=32, max_length=200)] | None = None
    enabled: bool = True
    version: int | None = Field(default=None, ge=1)

    @field_validator('url')
    @classmethod
    def https(cls, value: str):
        parts = urlsplit(value.strip())
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError('Enter a complete HTTPS URL without credentials or a fragment.')
        return value.strip()


class ApiWebhookView(BaseModel):
    url: str
    enabled: bool
    secret_configured: bool
    last_delivery_at: datetime | None
    last_error: str | None
    version: int


class ApiKeyView(BaseModel):
    configured: bool
    prefix: str | None
    created_at: datetime | None
    last_used_at: datetime | None


class ApiKeyCreated(ApiKeyView):
    key: str
