"""The automatic repair allowlist: remove sessions that are already expired."""
from sqlalchemy import delete
from app.models import LoginSession, now
from app.services import audit


def clean_expired_sessions(db, actor, run_id):
    if actor.id is not None or getattr(actor, 'role', None) != 'TECHNICAL':
        raise ValueError('Internal Technical agent required.')
    count = db.execute(delete(LoginSession).where(LoginSession.expires_at <= now())).rowcount
    if count: audit(db, actor, 'platform_owner.expired_sessions_cleaned', run_id, None, 'AGENT')
    return count
