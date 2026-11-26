from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from app.schemas import Input

from app.operations.stop_reasons import Reason, choices

# The same Dispatra reason list as stop reports, plus "Progress is normal".
CHOICES = choices(report=False)


class Reply(Input):
    """Current drivers send `reasons` and photos from the shared report form; older apps send one `reason`."""
    version: int = Field(ge=1)
    reason: Reason | None = None
    reasons: list[Reason] = Field(default_factory=list, max_length=15)
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
        if 'OTHER' in self.reasons and len(self.details.strip()) < 3:
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
    stop_id: UUID | None = None
    choices: list[Choice]
    status: Literal['OPEN','ANSWERED','CLOSED']
    reason: Reason | None
    reasons: list[Reason] = Field(default_factory=list)
    details: str
    estimated_ready_at: datetime | None
    created_at: datetime
    answered_at: datetime | None


class Acknowledge(Input):
    version: int = Field(ge=1)
