"""Shipper order-update emails: a copy of a Shipper's inbox notification, sent from the company mailbox.

Queued inside the notification's own transaction (outbox `email.requested`), so a rollback drops both.
Only companies with a connected mailbox send them, and only to Shippers who keep `email_updates` on.
Text is the notification text: Order numbers only, never addresses, prices or dispatcher notes.
"""
import os
from html import escape
from uuid import uuid4
from sqlalchemy import select
from app.models import Organization, OutboxEvent


class Sender:
    """Per-commit cache of whether and how one company sends order-update emails."""
    def __init__(self, db, organization_id):
        from app.intake.models import MailboxConnection
        self.db, self.organization_id = db, organization_id
        self.enabled = db.scalar(select(MailboxConnection.id).where(MailboxConnection.organization_id == organization_id)) is not None
        self._company = None

    def company(self):
        if self._company is None: self._company = self.db.get(Organization, self.organization_id)
        return self._company

    def queue(self, notification, user):
        from app.operations.models import EmailDelivery, Shipper
        if not self.enabled or not user.shipper_id: return
        shipper = self.db.get(Shipper, user.shipper_id)
        if shipper is None or not shipper.email_updates or not shipper.email: return
        company = self.company()
        base = os.environ.get('PUBLIC_WEB_URL', '').strip().rstrip('/')
        link = f'{base}/{company.slug}/shipper' if base else ''
        text = '\n'.join([f'Hello {shipper.name},', '', notification.body, *([f'', f'View your orders: {link}'] if link else []), '',
            f'— {company.name}', '', f'You receive order updates from {company.name}. Ask them to turn these emails off.']) + '\n'
        html = ('<!doctype html><html lang="en"><meta charset="utf-8">'
            f'<p>Hello {escape(shipper.name)},</p><p>{escape(notification.body)}</p>'
            + (f'<p><a href="{escape(link)}">View your orders</a></p>' if link else '')
            + f'<p>— {escape(company.name)}</p><p style="color:#64748b;font-size:12px">You receive order updates from {escape(company.name)}. Ask them to turn these emails off.</p></html>')
        identity = uuid4()
        self.db.flush()  # the delivery references the notification row
        self.db.add(EmailDelivery(id=identity, organization_id=self.organization_id, notification_id=notification.id, requested_by=user.id,
            recipient=shipper.email.lower(), subject=f'{company.name} — {notification.title}', body_text=text, body_html=html,
            message_id=f'<dispatra-{identity.hex}@dispatra.com>'))
        self.db.add(OutboxEvent(organization_id=self.organization_id, event_type='email.requested', entity_id=identity))
