from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict


class SyncChange(BaseModel):
    """Identifiers only. Clients refetch the affected records through normal authorized reads."""
    id: UUID
    type: str
    actor_type: Literal['USER', 'SYSTEM', 'AGENT']
    entity: str
    entity_id: UUID
    entity_version: int | None
    order_id: UUID | None
    route_id: UUID | None
    created_at: datetime


class SyncView(BaseModel):
    cursor: str
    changes: list[SyncChange]
    unread: int
    reset: bool
    next_poll_ms: int


class NotificationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    version: int
    created_at: datetime
    kind: str
    severity: Literal['INFO', 'WARNING', 'CRITICAL']
    title: str
    body: str
    order_id: UUID | None
    route_id: UUID | None
    read_at: datetime | None


class ReadAllView(BaseModel):
    updated: int
