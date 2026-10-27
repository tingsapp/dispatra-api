import json
import httpx
import pytest
from fastapi import HTTPException
from app.assistant import public, voice
from conftest import HEADERS

ROOT = '/api/v1/public/assistant/voice'
OFFER = {'sdp': 'v=0\r\no=browser-offer\r\n'}


@pytest.fixture
def provider_stub(monkeypatch):
    seen = []
    timers = []
    class Timer:
        def __init__(self, seconds, callback, args=()):
            self.seconds, self.callback, self.args = seconds, callback, args
            timers.append(self)
        def start(self): pass
        def cancel(self): pass
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            seen.append((url, kwargs))
            return httpx.Response(201 if url.endswith('/calls') else 200,
                text='v=0\r\no=provider-answer\r\n', headers={'location': '/v1/realtime/calls/rtc_test123'},
                request=httpx.Request('POST', url))
    monkeypatch.setenv('OPENAI_API_KEY', 'test-private-key')
    monkeypatch.setattr(voice.httpx, 'Client', Client)
    monkeypatch.setattr(voice.threading, 'Timer', Timer)
    yield seen, timers
    with voice._lock:
        voice._active.clear()


def test_public_voice_pins_model_and_public_scope(client, provider_stub):
    seen, timers = provider_stub
    reply = client.post(ROOT, json=OFFER)
    assert reply.status_code == 200, reply.text
    assert reply.headers['cache-control'] == 'no-store'
    data = reply.json()
    assert set(data) == {'sdp', 'end_token', 'max_seconds'}
    assert data['max_seconds'] == 300
    assert 'test-private-key' not in reply.text
    url, request = seen[0]
    assert url == 'https://api.openai.com/v1/realtime/calls'
    config = json.loads(request['files']['session'][1])
    assert config['model'] == 'gpt-realtime-mini'
    assert public.INSTRUCTIONS in config['instructions']
    assert config['tools'] == []
    assert config['max_output_tokens'] == 512
    assert 'transcription' not in config['audio']['input']
    assert timers[0].seconds == 300
    timers[0].callback(*timers[0].args)
    assert seen[-1][0].endswith('/rtc_test123/hangup')
    assert client.post(ROOT + '/end', json={'end_token': data['end_token']}).status_code == 204
    assert client.post(ROOT + '/end', json={'end_token': data['end_token'] + 'tampered'}).status_code == 400
    assert client.post(ROOT + '/end', json={'end_token': 'rtc_other.123.forged'}).status_code == 400


@pytest.mark.parametrize('body', [
    {'sdp': 'invalid'}, {'sdp': 'v=0\n' + 'x'*64000},
    {**OFFER, 'model': 'expensive-model'}, {**OFFER, 'instructions': 'override'},
    {**OFFER, 'organization_id': 'foreign'},
])
def test_voice_offer_is_bounded_and_cannot_override_config(client, body):
    assert client.post(ROOT, json=body).status_code == 422


def test_voice_rate_and_origin_boundaries(client, monkeypatch, provider_stub):
    calls = []
    def limit(identity, maximum):
        calls.append((identity, maximum))
        if len(calls) > 2: raise HTTPException(429, 'Too many attempts.')
    monkeypatch.setattr(public, 'rate_limit', limit)
    assert client.post(ROOT, json=OFFER).status_code == 200
    assert calls == [('public-voice:testclient', 3), ('public-voice:all', 60)]
    assert client.post(ROOT, json=OFFER).status_code == 429
    assert client.post(ROOT, json=OFFER, headers={**HEADERS, 'Origin': 'https://untrusted.example'}).status_code == 403


def test_voice_missing_key_is_safe_and_text_still_works(client, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    result = client.post(ROOT, json=OFFER)
    assert result.status_code == 503
    assert 'not_configured' not in result.text
    assert 'text chat' in result.text
    monkeypatch.setattr(public.provider, 'generate', lambda schema, *_: schema(message='Text is working.'))
    assert client.post('/api/v1/public/assistant/messages', json={'message': 'Hello'}).status_code == 200


def test_provider_failure_never_leaks_details(client, monkeypatch):
    def fail(*args): raise voice.Unavailable('private provider detail')
    monkeypatch.setattr(voice, 'create_session', fail)
    result = client.post(ROOT, json=OFFER)
    assert result.status_code == 503
    assert 'private provider detail' not in result.text


def test_graceful_shutdown_closes_active_calls(provider_stub):
    import asyncio
    seen, timers = provider_stub
    voice.create_session(OFFER['sdp'], public.INSTRUCTIONS, 'visitor')
    async def shutdown():
        async with voice.lifespan(None):
            assert len(voice._active) == 1
    asyncio.run(shutdown())
    assert not voice._active
    assert seen[-1][0].endswith('/rtc_test123/hangup')
