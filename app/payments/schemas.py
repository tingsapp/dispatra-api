from typing import Literal
from pydantic import BaseModel, ConfigDict


class SavedCard(BaseModel):
    id: str
    brand: str
    last4: str
    exp_month: int
    exp_year: int


class PaymentMethodsView(BaseModel):
    configured: bool
    test_mode: bool
    terms: str
    cards: list[SavedCard]


class CardSetupInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    consent: Literal[True]


class CardSetupView(BaseModel):
    url: str
