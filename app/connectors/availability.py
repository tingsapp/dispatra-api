"""Non-secret provider availability shared by owner, dispatcher and workers."""
from sqlalchemy import select, text
from .models import ProviderControl
from .catalog import BY_ID

def enabled(db, provider: str, lock=False) -> bool:
    if lock:lock_provider(db,provider,shared=True)
    query=select(ProviderControl.enabled).where(ProviderControl.provider == provider)
    return BY_ID[provider].auth != 'request_setup' and db.scalar(query) is True

def lock_provider(db,provider,shared=False):
    # Advisory locks serialize availability with imports without granting company write access.
    function='pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
    db.execute(text(f'SELECT {function}(hashtextextended(:scope,0))'),{'scope':'connector-provider:'+provider})

def ready(provider: str) -> bool:
    from . import profiles
    from .service import origin
    from fastapi import HTTPException
    if provider == 'samsara': return True
    if profiles.installed(provider) is None: return False
    if BY_ID[provider].auth == 'oauth':
        try: origin()
        except HTTPException: return False
    return True
