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


class AssistantReply(BaseModel):
    message: str
    references: list[AssistantReference]
    mode: Literal['AI', 'SEARCH']
    observed_at: datetime
    limited: bool
