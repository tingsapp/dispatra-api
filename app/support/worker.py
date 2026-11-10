"""Durable platform support email delivery; also polled by the existing email worker."""
import argparse
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from app.database import context, engine
from app.email_templates import layout, paragraph
from app.models import now
from app.operations import smtp_transport
from .models import SupportTicket
from .service import RECIPIENT

LEASE = timedelta(minutes=5)


def message(row):
    body = f'Ticket: {row.number}\nReply email: {row.email}\nIssue: {row.subject}\n\n{row.details}'
    content = ''.join(paragraph(line) for line in [f'Reply email: {row.email}', row.subject, row.details])
    return SimpleNamespace(recipient=RECIPIENT, subject=f'[{row.number}] {row.subject}',
        message_id=f'<support-{row.id}@dispatra.com>', body_text=body,
        body_html=layout(category='Support request', title=f'Ticket {row.number}',
            preheader=row.subject, content=content, footer='Submitted through Dispatra public chat.'))


def process_one():
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        row = db.scalar(select(SupportTicket).where(or_(
            and_(SupportTicket.delivery_status == 'PENDING', SupportTicket.available_at <= now()),
            and_(SupportTicket.delivery_status == 'SENDING', SupportTicket.claimed_at < now() - LEASE),
        )).order_by(SupportTicket.available_at, SupportTicket.id).limit(1).with_for_update(skip_locked=True))
        if not row: return False
        if row.delivery_status == 'SENDING':
            row.delivery_status = 'UNKNOWN'; row.error_code = 'WORKER_INTERRUPTED'
            print(f'Support email {row.number}: UNKNOWN WORKER_INTERRUPTED', flush=True)
            return True
        identifier, claim = row.id, uuid4()
        row.delivery_status = 'SENDING'; row.claimed_at = now(); row.claim_id = claim
        row.attempts += 1; row.error_code = None
        delivery = message(row)
    status, code = 'SENT', None
    try:
        # Fixed support recipient and platform SMTP account, never a customer's mailbox.
        smtp_transport.send(delivery)
    except smtp_transport.TemporaryFailure as exc: status, code = 'PENDING', exc.code
    except smtp_transport.PermanentFailure as exc: status, code = 'FAILED', exc.code
    except smtp_transport.UnknownAcceptance as exc: status, code = 'UNKNOWN', exc.code
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        row = db.get(SupportTicket, identifier, with_for_update=True)
        if row.claim_id != claim or row.delivery_status != 'SENDING': return True
        if status == 'PENDING' and row.attempts >= 5: status = 'FAILED'
        row.delivery_status = status; row.error_code = code; row.claimed_at = None; row.claim_id = None
        if status == 'SENT': row.sent_at = now()
        if status == 'PENDING': row.available_at = now() + timedelta(seconds=min(3600, 30 * 2 ** row.attempts))
        if status in {'FAILED', 'UNKNOWN'}: print(f'Support email {row.number}: {status} {code}', flush=True)
    return True


def poll_once(limit=25):
    count = 0
    while count < limit and process_one(): count += 1
    return count


def retry(number):
    """Private operator recovery. UNKNOWN requires checking the receiving inbox first."""
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        row = db.scalar(select(SupportTicket).where(SupportTicket.number == number).with_for_update())
        if row is None or row.delivery_status not in {'FAILED', 'UNKNOWN'}:
            raise ValueError('Only failed or uncertain support deliveries may be retried.')
        row.delivery_status = 'PENDING'; row.attempts = 0; row.available_at = now()
        row.claimed_at = None; row.claim_id = None; row.error_code = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retry', metavar='TICKET_NUMBER', help='Requeue a failed delivery; check the inbox first for UNKNOWN.')
    args = parser.parse_args()
    if args.retry: retry(args.retry)
    print(f'Processed {poll_once()} support email request(s).')


if __name__ == '__main__': main()
