from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from app.schemas import Input
from app.operations.schemas import Booking

Host = Annotated[str, Field(min_length=3, max_length=253, pattern=r'^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$')]
Password = Annotated[str, Field(min_length=1, max_length=500)]
Status = Literal['RECEIVED', 'ORDER_CREATED', 'NEEDS_REVIEW', 'UNKNOWN_SENDER', 'NOT_AN_ORDER', 'FAILED', 'DISCARDED']


class MailboxCheck(Input):
    host: Host
    port: int = Field(default=993, ge=1, le=65535)
    username: Annotated[str, Field(min_length=3, max_length=254)]
    password: Password | None = None
    folder: Annotated[str, Field(min_length=1, max_length=120)] = 'INBOX'
    smtp_host: Host | None = None
    smtp_port: int = Field(default=465, ge=1, le=65535)

    def outgoing(self):
        """The SMTP server; without one, the IMAP host with `imap.` swapped for `smtp.` (Gmail, Outlook, most hosts)."""
        return self.smtp_host or (('smtp.' + self.host[5:]) if self.host.lower().startswith('imap.') else self.host)


class MailboxInput(MailboxCheck):
    """`password` is required the first time; omit it later to keep the saved one."""
    enabled: bool = True
    version: int | None = Field(default=None, ge=1)


class MailboxView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    host: str
    port: int
    username: str
    folder: str
    smtp_host: str
    smtp_port: int
    enabled: bool
    last_polled_at: datetime | None
    last_error: str | None
    version: int


class MailboxCheckResult(BaseModel):
    ok: bool
    error: str | None = None


class IntakeView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    version: int
    received_at: datetime
    from_address: str
    from_name: str
    subject: str
    status: Status
    sender_verified: bool
    shipper_id: UUID | None
    shipper_name: str | None = None
    summary: str
    missing: list[str]
    order_id: UUID | None
    order_number: str | None = None
    error_code: str | None
    draft: dict | None = Field(description='Booking body the Order agent built; incomplete values are null')


class IntakeDetail(IntakeView):
    body: str
    extraction: dict | None


class IntakeVersion(Input):
    version: int = Field(ge=1)


class IntakeShipper(IntakeVersion):
    shipper_id: UUID


class IntakeOrder(IntakeVersion):
    booking: Booking
