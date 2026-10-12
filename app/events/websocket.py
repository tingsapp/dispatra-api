"""Read-only, authenticated streaming of the durable, audience-scoped sync feed.

Every read opens a short restricted-role transaction and revalidates the session. No
database connection is held while waiting on the network. Reading the shared event
log works across API/worker processes and preserves the existing gap-free cursor.
"""
import asyncio
import os
import time
from collections import Counter
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app import auth
from app.database import session
from .routes import sync
from .schemas import SyncView

router = APIRouter()
connections = Counter()
MAX_CONNECTIONS = 128  # Per API process; bound database/thread-pool demand.
MAX_PER_ACCOUNT = 4
HEARTBEAT_SECONDS = 15


def interval():
    try: value = float(os.environ.get('WEBSOCKET_SYNC_SECONDS', '1'))
    except ValueError: value = 1
    return min(max(value, 0.25), 5)


def request_for(socket):
    return Request({**socket.scope, 'type': 'http', 'method': 'GET'})


def valid_origin(socket):
    origins = {value.strip() for value in os.environ.get('WEB_ORIGINS',
        'http://localhost:3000,http://127.0.0.1:3000').split(',') if value.strip()}
    origins.add('https://dispatra.vercel.app')
    origin = socket.headers.get('origin')
    # Native drivers use the existing bearer header; browser sessions require an exact Origin.
    native = (not socket.cookies.get(auth.COOKIE)
        and socket.headers.get('authorization', '').startswith('Bearer dm_')
        and socket.headers.get('x-requested-with') == 'Dispatra')
    return origin in origins or (native and not origin)


def snapshot(request, slug, cursor):
    with contextmanager(session)() as db:
        user = auth.authenticated(request, db)
        user = auth.company_user(slug, user, db)
        view = SyncView.model_validate(sync(slug, db, cursor, user)).model_dump(mode='json')
        return str(user.id), view


@router.websocket('/api/v1/companies/{slug}/ws')
async def stream(socket: WebSocket, slug: str):
    cursor = socket.query_params.get('cursor')
    if not valid_origin(socket) or (cursor is not None and len(cursor) > 42):
        await socket.close(code=1008)
        return
    request = request_for(socket)
    account = None
    try:
        identity, view = await run_in_threadpool(snapshot, request, slug, cursor)
        if sum(connections.values()) >= MAX_CONNECTIONS or connections[identity] >= MAX_PER_ACCOUNT:
            await socket.close(code=1013)
            return
        account = identity
        connections[account] += 1
        await socket.accept()
        last_sent, summary = 0.0, None
        while True:
            current = (view['unread'], view['unread_messages'])
            if not last_sent or view['changes'] or view['reset'] or current != summary or time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
                await asyncio.wait_for(socket.send_json(view), timeout=10)
                cursor, summary, last_sent = view['cursor'], current, time.monotonic()
            # Receiving detects a closed browser immediately. This transport accepts no commands.
            try:
                await asyncio.wait_for(socket.receive_text(), timeout=0 if view['next_poll_ms'] == 0 else interval())
                await socket.close(code=1008)
                return
            except asyncio.TimeoutError:
                pass
            identity, view = await run_in_threadpool(snapshot, request, slug, cursor)
            if identity != account:
                await socket.close(code=1008)
                return
    except HTTPException:
        await socket.close(code=1008)
    except (SQLAlchemyError, asyncio.TimeoutError):
        await socket.close(code=1013)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if account:
            connections[account] -= 1
            if not connections[account]: del connections[account]
