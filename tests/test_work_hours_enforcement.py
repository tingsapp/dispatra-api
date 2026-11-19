"""Company work hours and holidays: Shippers book inside them, dispatchers may override, AUTO dispatch waits for opening time, then assigns every Order."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo
from fastapi.testclient import TestClient
from app.main import app
from app.dispatch import service
from app.operations.calendar import closed_reason
from app.operations.schemas import SettingsData
from conftest import HEADERS, login, post
from test_manual_operations import BASE, check, booking, setup
from test_dispatch_agent import mode

ZONE = ZoneInfo('America/Vancouver')
HOURS = [{'day': d, 'open': d not in {'SAT', 'SUN'}, 'start': '08:00', 'end': '17:00'} for d in ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')]


def local(*args): return datetime(*args, tzinfo=ZONE)


def test_closed_reason_checks_holidays_closed_days_and_hours_in_company_time():
    config = SettingsData(work_hours=HOURS, holidays=[{'date': '2026-12-25', 'name': 'Christmas Day'}])
    assert closed_reason(config, local(2026, 12, 25, 10)) == 'Closed on Christmas Day (Dec 25).'
    assert closed_reason(config, local(2026, 11, 21, 10)) == 'Closed on Saturdays.'
    assert closed_reason(config, local(2026, 11, 23, 7, 59)) == 'Monday pickups are 08:00–17:00.'
    assert closed_reason(config, local(2026, 11, 23, 17)) == 'Monday pickups are 08:00–17:00.'
    assert closed_reason(config, local(2026, 11, 23, 8)) is None and closed_reason(config, local(2026, 11, 23, 16, 59)) is None
    # 16:30 UTC on a Monday is 08:30 in Vancouver.
    assert closed_reason(config, datetime(2026, 11, 23, 16, 30, tzinfo=timezone.utc)) is None
    # Unconfigured companies are open 06:00–19:00 every day; 23:59 closes at midnight.
    assert closed_reason(SettingsData(), local(2026, 11, 22, 6)) is None and closed_reason(SettingsData(), local(2026, 11, 22, 18, 59)) is None
    assert closed_reason(SettingsData(), local(2026, 11, 22, 19)) == 'Sunday pickups are 06:00–19:00.'
    late = SettingsData(work_hours=[h | {'open': True, 'end': '23:59'} for h in HOURS])
    assert closed_reason(late, local(2026, 11, 22, 23, 59)) is None


def next_monday(hour):
    today = datetime.now(ZONE).date()
    day = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return datetime(day.year, day.month, day.day, hour, tzinfo=ZONE)


def calendar(client, holidays=()):
    saved = check(client.get(BASE + '/settings'))
    return check(client.put(BASE + '/settings', json={'version': saved['version'], 'data': saved['data'] | {'work_hours': HOURS, 'holidays': list(holidays)}},
        headers={'Idempotency-Key': str(uuid4())}))


def test_shippers_book_only_in_work_hours_and_dispatchers_may_override(setup):
    w = setup
    holiday = next_monday(10) + timedelta(days=1)
    calendar(w['client'], [{'date': holiday.date().isoformat(), 'name': 'Company Day'}])
    with TestClient(app, headers=HEADERS) as portal:
        login(portal, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
        prefs = check(portal.get(BASE + '/booking-preferences'))
        assert prefs['work_hours'] == HOURS and prefs['holidays'][0]['name'] == 'Company Day'
        body = booking(w['shipper']['id'], w['service'], w['type_id'])
        early = post(portal, BASE + '/orders', body | {'scheduled_at': next_monday(6).isoformat()})
        assert early.status_code == 422 and 'Monday pickups are 08:00–17:00' in early.text
        closed = post(portal, BASE + '/orders', body | {'scheduled_at': holiday.isoformat()})
        assert closed.status_code == 422 and 'Closed on Company Day' in closed.text
        order = check(post(portal, BASE + '/orders', body | {'scheduled_at': next_monday(9).isoformat()}), 201)
        moved = portal.put(BASE + f'/orders/{order["id"]}', json={'version': order['version'], 'booking': body | {'scheduled_at': next_monday(18).isoformat()}},
            headers={'Idempotency-Key': str(uuid4())})
        assert moved.status_code == 422
    after_hours = booking(w['shipper']['id'], w['service'], w['type_id']) | {'scheduled_at': next_monday(19).isoformat()}
    assert check(post(w['client'], BASE + '/orders', after_hours), 201)['status'] == 'NEW'


def test_auto_dispatch_waits_for_opening_then_assigns_every_order(setup, monkeypatch):
    w = setup
    calendar(w['client'])
    mode(w['client'], 'AUTO')
    held = check(post(w['client'], BASE + '/orders', booking(w['shipper']['id'], w['service'], w['type_id']) | {'scheduled_at': next_monday(20).isoformat()}), 201)
    open_order = check(post(w['client'], BASE + '/orders', booking(w['shipper']['id'], w['service'], w['type_id']) | {'scheduled_at': next_monday(9).isoformat()}), 201)
    organization_id = service.auto_companies()[0]
    # While the company is closed AUTO assigns nothing and the waiting Orders raise no Needs Attention row.
    monkeypatch.setattr(service, 'now', lambda: next_monday(7))
    assert service.due(organization_id) == []
    assert not any(item['order_id'] == held['id'] for item in check(w['client'].get(BASE + '/monitor'))['needs_attention'])
    # From opening time every waiting Order is assigned, including a pickup outside work hours.
    monkeypatch.setattr(service, 'now', lambda: next_monday(8))
    assert {held['id'], open_order['id']} <= {str(i) for i in service.due(organization_id)}
