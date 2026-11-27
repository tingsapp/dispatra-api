from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from app.schemas import Input

from app.operations.stop_reasons import ReasonCode


class Reply(Input):
    """Current drivers send `reasons` and photos from the shared report form; older apps send one `reason`."""
    version: int = Field(ge=1)
    reason: ReasonCode | None = None
    # The company's reason list plus NO_PROBLEM; its notes and photo rules are checked on reply.
    reasons: list[ReasonCode] = Field(default_factory=list, max_length=15)
    details: str = Field(default='', max_length=1000)
    estimated_ready_at: AwareDatetime | None = None
    photo_ids: list[UUID] = Field(default_factory=list, max_length=4)
    # Where the photos were taken, for a question that names no stop: a remaining stop of the question's Order.
    stop_id: UUID | None = None

    @model_validator(mode='after')
    def explain_other(self):
        if not self.reasons and self.reason: self.reasons = [self.reason]
        if not self.reasons: raise ValueError('Choose at least one reason.')
        if len(set(self.reasons)) != len(self.reasons) or len(set(self.photo_ids)) != len(self.photo_ids): raise ValueError('Duplicate reason or photo.')
        if 'NO_PROBLEM' in self.reasons and len(self.reasons) > 1: raise ValueError('Progress is normal cannot be combined with a problem.')
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
    value: str
    label: str
    stops: Literal['BOTH', 'PICKUP', 'DROPOFF'] = 'BOTH'
    notes_required: bool = False
    photo_required: bool = False


class QuestionView(BaseModel):
    id: UUID
    version: int
    order_id: UUID
    order_number: str
    prompt: str
    stop_id: UUID | None = None
    # What the question is about: the Order Watch observation kind, e.g. ARRIVAL_UNCONFIRMED offers Arrived.
    kind: str | None = None
    choices: list[Choice]
    status: Literal['OPEN','ANSWERED','CLOSED']
    reason: str | None
    reasons: list[str] = Field(default_factory=list)
    details: str
    estimated_ready_at: datetime | None
    created_at: datetime
    answered_at: datetime | None


class Acknowledge(Input):
    version: int = Field(ge=1)
