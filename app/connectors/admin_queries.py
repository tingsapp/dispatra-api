"""Owner-only connector usage counts; no accounts, credentials or vendor payloads."""
from sqlalchemy import select, func, or_, and_
from app.models import Organization, User
from .models import Connection, ProviderControl
from .catalog import PROVIDERS, LEGACY_PROVIDERS
from .schemas import ProviderView
from . import availability

def providers(db):
    counts=dict(db.execute(select(Connection.provider,func.count(User.id)).select_from(Connection)
        .join(Organization,Organization.id==Connection.organization_id)
        .join(User,User.organization_id==Connection.organization_id)
        .join(ProviderControl,ProviderControl.provider==Connection.provider)
        .where(Organization.active,User.role=='DISPATCHER',User.active,
            Connection.credentials.is_not(None),Connection.paused.is_(False),ProviderControl.enabled,
            or_(Connection.status=='CONNECTED',and_(Connection.status=='SYNCING',Connection.last_sync_at.is_not(None))))
        .group_by(Connection.provider)).all())
    saved=set(db.scalars(select(Connection.provider).where(Connection.status!='DISCONNECTED')))
    catalogue=[*PROVIDERS,*[p for p in LEGACY_PROVIDERS if p.id in saved]]
    return [ProviderView(id=p.id,name=p.name,initials=p.initials,
        connected_dispatchers=counts.get(p.id,0) if availability.ready(p.id) else 0) for p in catalogue]
