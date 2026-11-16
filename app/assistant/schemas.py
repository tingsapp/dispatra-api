from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Kind = Literal['ORDER', 'DRIVER', 'VEHICLE', 'SHIPPER']
Status = Literal['NEW', 'ASSIGNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED']
# Company records chat may change; `records.OPERATIONS` lists the allowed entity/operation pairs.
Entity = Literal['NONE', 'ORDER', 'SHIPPER', 'DRIVER', 'VEHICLE', 'ISSUE', 'SERVICE', 'ACCESSORIAL', 'VEHICLE_TYPE', 'PACKAGE_TYPE',
    'RATE_CARD', 'SETTINGS', 'NOTIFICATION', 'ALERT', 'REQUEST', 'QUOTE', 'TMS', 'MAILBOX', 'CONNECTOR']
Operation = Literal['NONE', 'CREATE', 'UPDATE', 'DELETE', 'CANCEL', 'COMPLETE', 'UNASSIGN', 'RESOLVE', 'RESOLVE_ALL', 'READ_ALL',
    'DELETE_ALL', 'CLEAR_ALL', 'DISCARD', 'DISCARD_ALL', 'RETRY', 'LINK_SHIPPER', 'CREATE_ORDER', 'SEND', 'RESET_PASSWORD', 'SYNC',
    'DISCONNECT', 'RESTORE']


class NoFields(BaseModel):
    """Request body of a chat change that needs no values, such as marking every notification read."""
    model_config = ConfigDict(extra='forbid')


class HiddenWarning(BaseModel):
    """One computed Needs Attention row to hide while its Order stays at `order_version`."""
    model_config = ConfigDict(extra='forbid')
    order_id: UUID
    key: str = Field(min_length=1, max_length=80)
    order_version: int = Field(ge=1)
    severity: Literal['WARNING', 'CRITICAL'] | None = None


class IssueRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: UUID
    version: int = Field(ge=1)


class AlertClear(BaseModel):
    """Clear Needs Attention: resolve reported issues and hide computed warnings, each re-checked on confirmation."""
    model_config = ConfigDict(extra='forbid')
    issues: list[IssueRef] = Field(default_factory=list, max_length=200)
    resolution: str = Field(min_length=3, max_length=2000)
    hide: list[HiddenWarning] = Field(default_factory=list, max_length=500)

    @model_validator(mode='after')
    def not_empty(self):
        if not self.hide and not self.issues: raise ValueError('Nothing to clear.')
        return self


class RequestBatch(BaseModel):
    """Incoming requests to discard: e-mails with their versions and TMS orders waiting for review."""
    model_config = ConfigDict(extra='forbid')
    emails: list[IssueRef] = Field(default_factory=list, max_length=200)
    tms: list[UUID] = Field(default_factory=list, max_length=200)

    @model_validator(mode='after')
    def not_empty(self):
        if not self.tms and not self.emails: raise ValueError('Nothing to discard.')
        return self


class ConnectorChange(BaseModel):
    """A connector sync or disconnect; the connector is named by its provider."""
    model_config = ConfigDict(extra='forbid')
    provider: str = Field(min_length=2, max_length=40)
    version: int = Field(ge=1)


class AssistantTurn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['user', 'assistant']
    content: str = Field(min_length=1, max_length=4000)


class AssistantQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=2000)
    history: list[AssistantTurn] = Field(default_factory=list, max_length=10)

    @field_validator('message')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('Please enter a question.')
        return value.strip()

    @field_validator('history')
    @classmethod
    def bounded_history(cls, value):
        if sum(len(turn.content) for turn in value) > 12000: raise ValueError('Conversation context is too long.')
        return value


class WorkspaceAssistantQuestion(AssistantQuestion):
    kind: Literal['CHAT', 'BRIEFING'] = 'CHAT'


class SearchPlan(BaseModel):
    include_records: bool
    query: str = Field(max_length=120)
    kinds: list[Kind] = Field(max_length=4)
    order_statuses: list[Status] = Field(max_length=5)
    # ASSIGN only when the dispatcher's latest message asks to give an Order to a driver; the server resolves and checks both.
    # CHANGE asks for a workspace change listed in `records.OPERATIONS` (or REQUEST); the server drafts and checks it.
    # No defaults: provider structured outputs require every field.
    action: Literal['NONE', 'ASSIGN', 'CHANGE']
    order_number: str = Field(max_length=60)
    driver: str = Field(max_length=120)
    entity: Entity
    operation: Operation
    # Existing record as written (order number, name, plate or code); for a new Order, its Shipper.
    target: str = Field(max_length=160)
    # Everything the dispatcher asked to set or change, restated from the conversation in plain words.
    request: str = Field(max_length=4000)


class DraftedRecord(BaseModel):
    """AI draft of a workspace request body; the server resolves names and addresses and validates it."""
    record_json: str = Field(max_length=60000)
    missing: list[str] = Field(max_length=20)


class GroundedAnswer(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    references: list[str] = Field(max_length=32)


class AssistantReference(BaseModel):
    key: str
    kind: Kind
    id: str
    label: str
    detail: str
    order_number: str | None = None
    driver_id: str | None = None


class BriefingCommentary(BaseModel):
    message: str = Field(min_length=1, max_length=180)


class DriverAssignments(BaseModel):
    driver_name: str
    orders: int


class DailyBriefing(BaseModel):
    day: str
    time_zone: str
    evening: bool
    received: int
    unassigned: int
    assigned: int
    in_progress: int
    completed: int
    open_incidents: int
    dispatch_mode: Literal['AUTO', 'MANUAL'] = 'MANUAL'
    # Today's orders that have a driver (assigned, in progress or completed), most orders first.
    assigned_by_driver: list[DriverAssignments] = Field(default_factory=list)


class BriefingTotals(BaseModel):
    """Deterministic daily totals the open briefing card refetches after workspace changes."""
    briefing: DailyBriefing
    observed_at: datetime


class AssistantAction(BaseModel):
    """A checked assignment the dispatcher confirms; the shared assignment command re-validates it."""
    kind: Literal['ASSIGN']
    order_id: UUID
    order_version: int
    order_number: str
    driver_id: UUID
    driver_name: str
    vehicle_name: str
    current_driver_name: str | None = None


class AssistantActionCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_id: UUID
    order_version: int = Field(ge=1)
    driver_id: UUID


class AssistantActionResult(BaseModel):
    message: str
    order_id: UUID
    order_version: int


class ChangeLine(BaseModel):
    field: str
    before: str | None = None
    after: str | None = None


class AssistantChange(BaseModel):
    """A drafted record change the dispatcher confirms. `body` is exactly the workspace request for `operation`."""
    operation: str
    title: str
    subject: str
    target_id: UUID | None = None
    destructive: bool = False
    lines: list[ChangeLine]
    body: dict


class AssistantChangeCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: str = Field(max_length=40)
    target_id: UUID | None = None
    body: dict


class AssistantChangeResult(BaseModel):
    message: str
    id: UUID | None = None
    version: int | None = None
    # Shown once on the confirmation card for a new driver or shipper; never part of chat text or history.
    initial_password: str | None = None


class AssistantReply(BaseModel):
    briefing: DailyBriefing | None = None
    action: AssistantAction | None = None
    change: AssistantChange | None = None
    message: str
    references: list[AssistantReference]
    mode: Literal['AI', 'SEARCH']
    observed_at: datetime
    limited: bool
