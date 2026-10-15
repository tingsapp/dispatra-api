"""Eligibility for enabling AUTO in an External TMS workspace."""
from fastapi import HTTPException
from sqlalchemy import select, or_, and_
from app.connectors.models import Connection
from app.connectors.profiles import installed


def require_connected_tms(db, actor):
    providers = db.scalars(select(Connection.provider).where(
        Connection.organization_id == actor.organization_id,
        Connection.credentials.is_not(None),
        or_(Connection.status == 'CONNECTED', and_(Connection.status == 'SYNCING', Connection.last_sync_at.is_not(None))),
    ))
    if not any(installed(provider) is not None for provider in providers):
        raise HTTPException(409, 'Auto dispatch cannot be turned on because this account uses an External TMS, '
            '“Use Dispatra TMS” is unchecked, and no TMS connector is connected. '
            'Open Integration to connect your TMS, or check “Use Dispatra TMS” to manage operations in Dispatra.')
