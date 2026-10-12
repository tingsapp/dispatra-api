from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from app.schemas import Input, LoginID, Name, Password, CompanyPhone, CompanyAddress

DisplayName = Annotated[str, Field(max_length=160)]
Version = Annotated[int, Field(ge=1)]


class CompanySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    slug: str
    name: str
    logo_url: str
    active: bool
    version: int
    created_at: datetime
    dispatcher_count: int
    active_dispatcher_count: int


class CompanyDetail(CompanySummary):
    updated_at: datetime
    phone: str
    address: str
    settings_version: int | None
    shipper_account_count: int
    driver_account_count: int
    active_session_count: int
    shipper_count: int
    driver_count: int
    order_count: int


class CompanyUpdate(Input):
    version: Version
    name: Name
    phone: CompanyPhone | None = None
    address: CompanyAddress | None = None
    settings_version: Version | None = None


class VersionCommand(Input):
    version: Version


class DispatcherView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    login_id: str
    display_name: str
    active: bool
    version: int
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None
    active_session_count: int


class DispatcherCredentialView(DispatcherView):
    # Present only on the response that generated it; replays and later reads return null.
    initial_password: str | None = None


class DispatcherCreate(Input):
    login_id: LoginID
    display_name: DisplayName = ''


class DispatcherUpdate(Input):
    version: Version
    login_id: LoginID
    display_name: DisplayName = ''


class OverviewDispatcherUpdate(DispatcherUpdate):
    id: UUID
    new_password: Password | None = None


class CompanyOverviewUpdate(CompanyUpdate):
    active: bool
    dispatcher: OverviewDispatcherUpdate | None = None


class CompanyOverviewView(BaseModel):
    company: CompanyDetail
    dispatcher: DispatcherView | None


class SessionRevocation(BaseModel):
    revoked: int


class AuditEntry(BaseModel):
    id: UUID
    action: str
    entity_id: UUID
    actor_login_id: str
    actor_role: Literal['ADMIN', 'DISPATCHER', 'SHIPPER', 'DRIVER']
    created_at: datetime


class PlatformNotificationView(BaseModel):
    id: UUID
    version: int
    created_at: datetime
    kind: str
    severity: Literal['INFO', 'WARNING', 'CRITICAL']
    title: str
    body: str
    read_at: datetime | None
    organization_id: UUID
    company_name: str
    target: Literal['company', 'monitor'] = 'company'


class PlatformInbox(BaseModel):
    items: list[PlatformNotificationView]
    unread: int
