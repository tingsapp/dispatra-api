from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, SecretStr

class CredentialField(BaseModel):
    key: str
    label: str
    secret: bool

class ConnectorView(BaseModel):
    id: str
    name: str
    initials: str
    description: str
    auth: Literal['oauth', 'bearer', 'api_key', 'session', 'client_credentials', 'request_setup']
    ready: bool
    status: Literal['ACTIVATION_REQUIRED','NOT_CONNECTED','CONNECTING','CONNECTED','SYNCING','RECONNECT_REQUIRED','SYNC_FAILED','DISCONNECTED','PAUSED']
    paused: bool = False
    account: str | None = None
    version: int | None = None
    last_sync_at: datetime | None = None
    message: str | None = None
    imported: int = 0
    review_count: int = 0
    credential_fields: list[CredentialField] = Field(default_factory=list)

class ConnectInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int | None = Field(default=None, ge=1)
    credentials: dict[str, SecretStr] = Field(default_factory=dict, max_length=4)

class ConnectorAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)

class ConnectResult(BaseModel):
    connector: ConnectorView
    authorization_url: str | None = None

class ReviewItem(BaseModel):
    kind: str
    id: str
    reference: str
    reasons: list[str]
    created_at: datetime

class ProviderView(BaseModel):
    id: str
    name: str
    initials: str
    connected_dispatchers: int
