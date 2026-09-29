from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from app.schemas import Input, LoginID, Name

DisplayName = Annotated[str, Field(max_length=160)]
Version = Annotated[int, Field(ge=1)]


class CompanySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    slug: str
    name: str
    active: bool
    version: int
    created_at: datetime
    dispatcher_count: int
    active_dispatcher_count: int


class CompanyDetail(CompanySummary):
    updated_at: datetime
    shipper_account_count: int
    driver_account_count: int
    active_session_count: int


class CompanyUpdate(Input):
    version: Version
    name: Name


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


class SessionRevocation(BaseModel):
    revoked: int


class AuditEntry(BaseModel):
    id: UUID
    action: str
    entity_id: UUID
    actor_login_id: str
    actor_role: Literal['ADMIN', 'DISPATCHER', 'SHIPPER', 'DRIVER']
    created_at: datetime
