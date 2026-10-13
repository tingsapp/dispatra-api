from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from app.schemas import Input

Reason = Literal['ITEMS_NOT_READY','WAITING_FOR_STAFF','ACCESS_BLOCKED','HANDLING_IN_PROGRESS','VEHICLE_PROBLEM','TRAFFIC','RECIPIENT_UNAVAILABLE','NEED_HELPER','DAMAGED_ITEM','UNSAFE','OTHER','NO_PROBLEM']
CHOICES = [dict(value=value,label=label) for value,label in [
    ('ITEMS_NOT_READY','Items not ready'), ('WAITING_FOR_STAFF','Waiting for staff'),
    ('ACCESS_BLOCKED','Access blocked'), ('HANDLING_IN_PROGRESS','Loading or unloading'),
    ('VEHICLE_PROBLEM','Vehicle problem'), ('TRAFFIC','Traffic delay'), ('RECIPIENT_UNAVAILABLE','Recipient unavailable'),
    ('NEED_HELPER','Need handling assistance'), ('DAMAGED_ITEM','Item damaged'), ('UNSAFE','Unsafe situation'), ('OTHER','Another issue'), ('NO_PROBLEM','Progress is normal')]]


class Reply(Input):
    version: int = Field(ge=1)
    reason: Reason
    details: str = Field(default='', max_length=1000)
    estimated_ready_at: AwareDatetime | None = None

    @model_validator(mode='after')
    def explain_other(self):
        if self.reason == 'OTHER' and len(self.details.strip()) < 3:
            raise ValueError('Describe the issue when choosing Another issue.')
        return self


class Evidence(BaseModel):
    label: str
    value: str
    source: str
    at: datetime | None = None


class IncidentView(BaseModel):
    id: UUID
    version: int
    organization_id: UUID
    company_name: str | None
    order_id: UUID
    order_number: str
    stop_id: UUID | None
    kind: str
    title: str
    description: str
    severity: Literal['WARNING','CRITICAL']
    certainty: Literal['OBSERVED','PREDICTED','REPORTED','NEEDS_CONFIRMATION']
    status: Literal['OPEN','ACKNOWLEDGED','RESOLVED','SUPERSEDED']
    evidence: list[Evidence]
    suggestions: list[str]
    response_due_at: datetime | None
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    resolution: str
    driver_reply: str | None
    created_at: datetime
    updated_at: datetime


class IncidentPage(BaseModel):
    items: list[IncidentView]
    next_before: UUID | None


class Choice(BaseModel):
    value: Reason
    label: str


class QuestionView(BaseModel):
    id: UUID
    version: int
    order_id: UUID
    order_number: str
    prompt: str
    choices: list[Choice]
    status: Literal['OPEN','ANSWERED','CLOSED']
    reason: Reason | None
    details: str
    estimated_ready_at: datetime | None
    created_at: datetime
    answered_at: datetime | None


class Acknowledge(Input):
    version: int = Field(ge=1)
