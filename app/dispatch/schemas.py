from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from app.schemas import Input


class CandidateMetrics(BaseModel):
    extra_minutes: float = Field(description='Estimated extra fleet driving: trip to pickup plus the Order, or the increase of a planned Route')
    deadhead_km: float | None = Field(description='Straight-line distance from the driver to the first pickup')
    position: Literal['GPS', 'HOME'] | None
    route_orders: int = Field(description='Orders already on the planned Route this Order would join')
    slack_minutes: int | None = Field(description='Minutes between planned delivery and the earliest delivery deadline')
    preferred: bool
    exact_vehicle_type: bool


class Candidate(BaseModel):
    driver_id: UUID
    driver_name: str
    driver_number: str
    vehicle_id: UUID
    vehicle_name: str
    route_id: UUID | None
    planned_at: datetime
    first_arrival: datetime
    metrics: CandidateMetrics
    facts: list[str]
    score: float
    reason: str


class Exclusion(BaseModel):
    driver_id: UUID
    driver_name: str
    driver_number: str
    reason: str


class DecisionView(BaseModel):
    """One Dispatch agent evaluation. Travel is the built-in estimate; assignment re-checks with road travel."""
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    version: int
    order_id: UUID
    order_version: int
    mode: Literal['AUTO', 'MANUAL']
    status: Literal['SUGGESTED', 'ASSIGNED', 'NO_CANDIDATE']
    ranked_by: Literal['AI', 'RULES']
    summary: str
    candidates: list[Candidate] = Field(description='Feasible drivers, best first')
    excluded: list[Exclusion] = Field(description='Drivers that failed a hard check, with the reason')
    driver_id: UUID | None
    decided_by: UUID | None = Field(description='Dispatcher who asked or approved; null when the agent assigned in AUTO mode')
    error_code: str | None = Field(description='Why AI ranking was unavailable and rules were used')
    created_at: datetime


class DecisionApproval(Input):
    version: int = Field(ge=1)
    driver_id: UUID
