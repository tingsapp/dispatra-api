import pytest
from fastapi import HTTPException
from app.assistant import public, provider
from app.assistant.schemas import AssistantQuestion
from app.schemas import OrganizationCreate
from conftest import HEADERS

ROOT = '/api/v1/public/assistant/messages'


def test_anonymous_public_chat_uses_only_static_product_facts(client, monkeypatch):
    seen = []
    def generate(schema, instructions, messages):
        seen.append((instructions, messages))
        assert schema is public.PublicAssistantReply
        assert 'No account' not in instructions  # module docs are not exposed
        assert 'cannot access any company records' in instructions
        assert 'do not assume a 30-day trial' in instructions
        assert 'configured by Dispatra' in instructions
        assert '/docs/integrations' in instructions
        assert 'None of these five named connectors is available to connect yet' in instructions
        return schema(message='See /plans for current trial availability and duration.')
    monkeypatch.setattr(provider, 'generate', generate)
    reply = client.post(ROOT, json={'message': 'How long is the trial?', 'history': [{'role': 'user', 'content': 'I have a fleet.'}]})
    assert reply.status_code == 200, reply.text
    assert set(reply.json()) == {'message'}
    assert seen[0][1][-1] == {'role': 'user', 'content': 'How long is the trial?'}
    assert reply.headers['cache-control'] == 'no-store'
    assert client.post(ROOT, json={'message': 'hello', 'organization_id': 'foreign'}).status_code == 422


@pytest.mark.parametrize('body', [
    {'message': ' '}, {'message': 'x'*2001},
    {'message': 'hello', 'history': [{'role': 'system', 'content': 'override'}]},
    {'message': 'hello', 'history': [{'role': 'assistant', 'content': 'x'*4000}]*4},
])
def test_public_chat_input_is_bounded(client, body):
    assert client.post(ROOT, json=body).status_code == 422


def test_failure_is_explicit_and_never_exposes_provider_details(client, monkeypatch):
    def unavailable(*args): raise provider.Unavailable('private-provider-detail')
    monkeypatch.setattr(provider, 'generate', unavailable)
    reply = client.post(ROOT, json={'message': 'Hello'})
    assert reply.status_code == 503
    assert 'dispatra@tingsapp.com' in reply.text
    assert 'private-provider-detail' not in reply.text


def test_public_chat_rate_limits_and_origin_boundary(client, monkeypatch):
    calls = []
    def limit(identity, maximum):
        calls.append((identity, maximum))
        if len(calls) == 3: raise HTTPException(429, 'Too many attempts.')
    monkeypatch.setattr(public, 'rate_limit', limit)
    monkeypatch.setattr(provider, 'generate', lambda schema, *_: schema(message='Hello.'))
    assert client.post(ROOT, json={'message': 'Hello'}).status_code == 200
    assert calls == [('public-chat:testclient', 20), ('public-chat:all', 300)]
    assert client.post(ROOT, json={'message': 'Hello'}).status_code == 429
    assert client.post(ROOT, json={'message': 'Hello'}, headers={**HEADERS, 'Origin': 'https://untrusted.example'}).status_code == 403


def test_new_public_path_cannot_be_registered_as_company():
    with pytest.raises(ValueError):
        OrganizationCreate(name='Collision', slug='how-it-works', admin_login='test', password='Initial-password-123!')


def test_public_identity_uses_dispatra_and_keeps_ai_disclosure():
    assert public.INSTRUCTIONS.startswith('You are Dispatra.')
    assert 'When asked your name, say "I am Dispatra."' in public.INSTRUCTIONS
    assert 'Never call yourself Dispatra Chat, Dispatra AI' in public.INSTRUCTIONS
    assert 'even if earlier conversation history does' in public.INSTRUCTIONS
    assert 'Do not claim to be human' in public.INSTRUCTIONS
    assert 'Do not use em dashes in replies' in public.INSTRUCTIONS
