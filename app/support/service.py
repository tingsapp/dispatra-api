"""The AI proposes a handoff; this command owns validation, numbering and persistence."""
import hashlib
import json
import re
from uuid import uuid4
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field, TypeAdapter, ValidationError
from sqlalchemy import select, text
from app.security import rate_limit
from .models import SupportTicket

RECIPIENT = 'contact@dispatra.com'
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_EMAIL_TYPE = TypeAdapter(EmailStr)
_SECRET = re.compile(r'(?i)((?:password|api[ _-]?key|client[ _-]?secret|access[ _-]?token|bearer)(?:\s*[:=]\s*|\s+))[^\s,;]+')


class SupportDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    subject: str = Field(min_length=3, max_length=160)
    details: str = Field(min_length=10, max_length=3000)


def session_scope(db, conversation_id):
    key = hashlib.sha256(str(conversation_id).encode()).hexdigest()
    db.execute(text("SELECT set_config('app.support_session', :key, true)"), {'key': key})
    return key


def fingerprint(question):
    return hashlib.sha256(json.dumps(question.model_dump(mode='json'), sort_keys=True).encode()).hexdigest()


def existing(db, session_hash):
    return db.scalar(select(SupportTicket).where(SupportTicket.session_hash == session_hash))


def receipt(row):
    return {
        'message': f'Ticket {row.number} has been created. Our team will review your request and contact you by email. '
                   'Please keep this ticket number for reference.',
        'ticket_number': row.number,
    }


def replay(db, question):
    row = existing(db, session_scope(db, question.conversation_id))
    if row and row.request_id == question.request_id:
        if row.request_hash != fingerprint(question):
            raise HTTPException(409, 'This message was changed. Send it as a new message.')
        return receipt(row)
    return None


def contact_email(question):
    # Only visitor-authored text may supply the contact address, never AI history or a model guess.
    messages = [turn.content for turn in question.history if turn.role == 'user'] + [question.message]
    for message in reversed(messages):
        candidates = list(_EMAIL.finditer(message))
        if candidates:
            # Ambiguous addresses must be clarified rather than choosing someone else's address.
            addresses = {match.group().rstrip('.') for match in candidates
                         if match.group().rstrip('.').casefold() != RECIPIENT}
            if len(addresses) != 1: return None
            try: return str(_EMAIL_TYPE.validate_python(addresses.pop()))
            except ValidationError: return None
    return None


def create(db, question, draft, identity):
    session_hash = session_scope(db, question.conversation_id)
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': 'support:' + session_hash})
    previous = existing(db, session_hash)
    if previous:
        if previous.request_id == question.request_id and previous.request_hash != fingerprint(question):
            raise HTTPException(409, 'This message was changed. Send it as a new message.')
        result = receipt(previous)
        if previous.request_id != question.request_id:
            result['message'] = f'Your support ticket is {previous.number}. Our team will follow up by email. For a separate issue, start a new chat so I can create a separate ticket.'
        return result
    email = contact_email(question)
    if not email:
        return {'message': 'Our team can help with this. What email address should we use to follow up? '
                           'I’ll create a support ticket with the issue you described.'}
    rate_limit('support-ip:' + identity, 5)
    rate_limit('support-email:' + email.casefold(), 3)
    rate_limit('support-all', 100)
    identity = uuid4()
    row = SupportTicket(id=identity, session_hash=session_hash, request_id=question.request_id,
        request_hash=fingerprint(question), number='DSP-' + identity.hex[:16].upper(), email=email,
        subject=_SECRET.sub(r'\1[redacted]', draft.subject).replace('\r', ' ').replace('\n', ' ')[:160],
        details=_SECRET.sub(r'\1[redacted]', draft.details))
    db.add(row)
    db.flush()
    # The caller commits before returning this receipt. Email delivery never changes the ticket identity.
    return receipt(row)
