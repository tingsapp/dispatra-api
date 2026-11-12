"""Company-scoped daily briefing acceptance on real PostgreSQL."""
from app.assistant import provider
from app.assistant.schemas import BriefingCommentary
from test_manual_operations import setup, new_order, assign, check, BASE
from conftest import post


def test_briefing_uses_saved_daily_order_states(setup, monkeypatch):
    w = setup
    new_order(w)
    done = new_order(w)
    assign(w, done)
    current = check(w['client'].get(BASE + f'/orders/{done["id"]}'))
    check(post(w['client'], BASE + f'/orders/{done["id"]}/complete', {'version': current['version']}))
    monkeypatch.setattr(provider, 'generate', lambda *args: BriefingCommentary(message='Review the waiting work.'))
    reply = check(post(w['client'], BASE + '/assistant/messages', {'kind': 'BRIEFING', 'message': 'Give me the current briefing.'}))
    assert reply['briefing']['received'] == 2
    assert reply['briefing']['unassigned'] == 1
    assert reply['briefing']['completed'] == 1
    assert reply['briefing']['assigned'] == reply['briefing']['in_progress'] == 0
    assert reply['briefing']['time_zone'] == 'America/Vancouver'
    assert reply['message'] == 'Review the waiting work.'
    assert reply['references'] == [] and reply['mode'] == 'AI'


def test_briefing_keeps_verified_counts_when_provider_is_unavailable(setup, monkeypatch):
    w = setup
    new_order(w)
    def unavailable(*args): raise provider.Unavailable()
    monkeypatch.setattr(provider, 'generate', unavailable)
    reply = check(post(w['client'], BASE + '/assistant/messages', {'kind': 'BRIEFING', 'message': 'Give me the current briefing.'}))
    assert reply['briefing']['received'] == reply['briefing']['unassigned'] == 1
    assert reply['mode'] == 'SEARCH'
    assert 'unavailable' in reply['message'].lower()
    assert reply['references'] == []


def test_live_briefing_totals_follow_saved_order_changes(setup, monkeypatch):
    w = setup
    def unexpected(*args): raise AssertionError('live totals must not call the AI provider')
    monkeypatch.setattr(provider, 'generate', unexpected)
    before = check(w['client'].get(BASE + '/assistant/briefing'))
    order = new_order(w)
    after = check(w['client'].get(BASE + '/assistant/briefing'))
    assert after['briefing']['received'] == before['briefing']['received'] + 1
    assert after['briefing']['unassigned'] == before['briefing']['unassigned'] + 1
    assign(w, order)
    assigned = check(w['client'].get(BASE + '/assistant/briefing'))
    assert assigned['briefing']['unassigned'] == before['briefing']['unassigned']
    assert assigned['briefing']['assigned'] == before['briefing']['assigned'] + 1
    assert assigned['observed_at'] and assigned['briefing']['time_zone'] == 'America/Vancouver'
