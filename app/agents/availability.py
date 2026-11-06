"""A public yes/no availability signal; no database, customer information or internal details."""
import json
import os
from pathlib import Path
import time
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get('/api/v1/availability', include_in_schema=False)
def availability():
    try:
        state = json.loads(Path(os.environ.get('TECHNICAL_WATCHDOG_STATE', '/var/lib/dispatra-watchdog/state.json')).read_text())
        healthy = state.get('ok') is True and 0 <= time.time() - state['at'] < 120
    except (OSError, ValueError, TypeError, KeyError, AttributeError): healthy = False
    return JSONResponse({'status': 'ok' if healthy else 'unavailable'}, status_code=200 if healthy else 503,
        headers={'Cache-Control': 'no-store'})
