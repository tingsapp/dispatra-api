from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

Kind = Literal['ORDER', 'DRIVER', 'VEHICLE', 'SHIPPER']
Status = Literal['NEW', 'ASSIGNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED']


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


class AssistantReply(BaseModel):
    briefing: DailyBriefing | None = None
    message: str
    references: list[AssistantReference]
    mode: Literal['AI', 'SEARCH']
    observed_at: datetime
    limited: bool
