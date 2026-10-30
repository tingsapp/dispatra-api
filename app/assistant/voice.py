"""Public WebRTC handshake. No provider credentials or tenant data reach the browser."""
import hashlib
import hmac
from contextlib import asynccontextmanager
import json
import os
import re
import threading
import time
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .provider import Unavailable

MODEL = 'gpt-realtime-mini'
MAX_SECONDS = 300
_active: dict[str, threading.Timer] = {}
_lock = threading.Lock()


@asynccontextmanager
async def lifespan(_app):
    yield
    # Graceful service restarts must not leave paid provider sessions running.
    with _lock:
        calls = list(_active.items())
        _active.clear()
    for call_id, timer in calls:
        timer.cancel()
        _hangup(call_id)


class VoiceOffer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sdp: str = Field(min_length=10, max_length=64000, pattern=r'^v=0\r?\n')


class VoiceSession(BaseModel):
    sdp: str
    max_seconds: int
    end_token: str


class VoiceEnd(BaseModel):
    model_config = ConfigDict(extra='forbid')
    end_token: str = Field(min_length=10, max_length=400)


def _headers():
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise Unavailable('not_configured')
    return {'Authorization': f'Bearer {key}'}


def _hangup(call_id, attempt=0):
    with _lock:
        timer = _active.pop(call_id, None)
    if timer:
        timer.cancel()
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(f'https://api.openai.com/v1/realtime/calls/{call_id}/hangup', headers=_headers())
            if response.status_code not in (404, 410):
                response.raise_for_status()
    except (httpx.HTTPError, Unavailable):
        # Retry without recording audio, SDP, keys or provider response bodies.
        if attempt < 3:
            retry = threading.Timer(10, _hangup, args=(call_id, attempt + 1))
            retry.daemon = True
            retry.start()


def _signature(value):
    return hmac.new(_headers()['Authorization'].encode(), value.encode(), hashlib.sha256).hexdigest()


def end_session(token):
    try:
        call_id, expires, signature = token.split('.')
        if not re.fullmatch(r'rtc_[A-Za-z0-9_-]{1,200}', call_id):
            raise ValueError()
        if not 0 <= int(expires) - time.time() <= MAX_SECONDS + 120:
            raise ValueError()
        if not hmac.compare_digest(_signature(f'{call_id}.{expires}'), signature):
            raise ValueError()
    except (ValueError, Unavailable):
        raise Unavailable('invalid_session') from None
    _hangup(call_id)


def create_session(sdp: str, instructions: str, identity: str):
    config = {
        'type': 'realtime', 'model': MODEL,
        'instructions': instructions + '\nYou are speaking aloud. Keep answers short and conversational. '
        'Begin by saying: Hello, I am Dispatra. How can I help you?',
        'max_output_tokens': 512,
        'audio': {
            'input': {'turn_detection': {'type': 'server_vad', 'create_response': True, 'interrupt_response': True}},
            'output': {'voice': 'marin'},
        },
        'tools': [],
    }
    headers = {**_headers(), 'OpenAI-Safety-Identifier': hashlib.sha256(identity.encode()).hexdigest()}
    try:
        with httpx.Client(timeout=25) as client:
            response = client.post('https://api.openai.com/v1/realtime/calls', headers=headers,
                                   files={'sdp': (None, sdp), 'session': (None, json.dumps(config))})
            response.raise_for_status()
        call_id = urlparse(response.headers.get('location', '')).path.rsplit('/', 1)[-1]
        if not re.fullmatch(r'rtc_[A-Za-z0-9_-]{1,200}', call_id):
            raise Unavailable('invalid_session')
        # A trusted backend timer also closes the call if the visitor's tab disappears.
        timer = threading.Timer(MAX_SECONDS, _hangup, args=(call_id,))
        timer.daemon = True
        with _lock:
            _active[call_id] = timer
        timer.start()
        value = f'{call_id}.{int(time.time()) + MAX_SECONDS + 60}'
        return VoiceSession(sdp=response.text, max_seconds=MAX_SECONDS, end_token=f'{value}.{_signature(value)}')
    except httpx.HTTPError:
        raise Unavailable('voice_unavailable') from None
