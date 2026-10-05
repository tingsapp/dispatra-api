"""Order agent: mailbox settings, email parsing, extraction mapping and Order creation on real PostgreSQL.

IMAP, OpenAI and geocoding are replaced by in-process fakes; no external calls are made.
"""
import os
from email.message import EmailMessage
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from conftest import login, company, post, owner_engine

os.environ['MAILBOX_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
from app.intake import booking, extract, geocode, mailbox, service, worker  # noqa: E402

BASE = '/api/v1/companies/acme'
INTAKE = BASE + '/email-intake'
PASSWORD = 'mailbox-app-password'


def check(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def address(postal='V5Y 1V4', lat=49.26, lng=-123.11):
    return dict(text=f'123 Main Street, Vancouver, BC {postal}, Canada', city='Vancouver', province='BC', postal_code=postal, latitude=lat, longitude=lng)


def raw_email(sender='alice@example.com', verified=True, subject='Delivery tomorrow', body='Please pick up 2 boxes.', message_id=None):
    message = EmailMessage()
    if verified: message['Authentication-Results'] = f'mx.example.net; dkim=pass header.i=@{sender.split("@")[1]}; spf=pass; dmarc=pass header.from={sender.split("@")[1]}'
    message['From'] = f'Alice <{sender}>'
    message['To'] = 'orders@acme.test'
    message['Subject'] = subject
    message['Message-ID'] = message_id or f'<{uuid4()}@example.com>'
    message['Date'] = 'Mon, 05 Oct 2026 09:00:00 -0700'
    message.set_content(body)
    return message.as_bytes()


def facts(**changes):
    value = dict(is_order_request=True, summary='Two boxes from the warehouse to a customer.', service=None, vehicle_type=None,
        scheduled_at='2026-10-06T09:00:00-07:00', reference='PO-77',
        stops=[dict(kind='PICKUP', use_shipper_warehouse=True, address=None, contact_name=None, phone=None, instructions=None, window_start=None, window_end=None),
            dict(kind='DROPOFF', use_shipper_warehouse=False, address=dict(street='500 Granville Street', city='Vancouver', province='British Columbia', postal_code='v6c1w6'),
                contact_name='Bob', phone='6045550199', instructions='Front desk', window_start=None, window_end='2026-10-06T17:00:00-07:00')],
        items=[dict(pickup_index=0, delivery_index=1, quantity=2, weight_each=22, weight_unit='lb', length=12, width=10, height=8,
            dimension_unit='in', pallets_each=None, fragile=True, dangerous_goods=False, description='Boxes')])
    value.update(changes)
    return extract.Extraction.model_validate(value)


class Inbox:
    def __init__(self): self.messages, self.next_uid = [], 1

    def add(self, raw):
        self.messages.append(mailbox.Fetched(self.next_uid, raw))
        self.next_uid += 1

    def fetch(self, host, port, username, password, folder, uid_validity, last_uid, limit=25):
        assert password == PASSWORD
        if uid_validity != 7: return 7, self.next_uid - 1, []
        new = [m for m in self.messages if m.uid > last_uid][:limit]
        return 7, (new[-1].uid if new else last_uid), new


@pytest.fixture
def agent(client, monkeypatch):
    inbox, extracted = Inbox(), {'value': facts()}
    monkeypatch.setattr(mailbox, 'check', lambda *args: None)
    monkeypatch.setattr(mailbox, 'fetch_new', inbox.fetch)
    def fake_extract(email, context):
        value = extracted['value']
        if isinstance(value, Exception): raise value
        assert context['shipper'] is None or set(context['shipper']) == {'name', 'warehouse_city'}
        return value
    monkeypatch.setattr(extract, 'extract', fake_extract)
    monkeypatch.setattr(geocode, 'verify', lambda text_, postal: {'text': text_, 'latitude': 49.28, 'longitude': -123.12})
    login(client); company(client); login(client, 'dispatch', 'acme', 'dispatcher')
    catalog = check(client.get(BASE + '/catalog'))
    rates = check(client.get(BASE + '/rate-cards'))
    service_id = next(c['id'] for c in catalog if c['code'] == 'SAME_DAY')
    fixed = next(c['id'] for c in rates if c['data']['method'] == 'FIXED')
    shipper = check(post(client, BASE + '/shippers', dict(name='Alice Shipper', kind='BUSINESS', company_name='Fresh Test', email='alice@example.com',
        phone='6045550101', warehouse=address(), rate_card_id=fixed)), 201)
    mailbox_view = check(client.put(INTAKE + '/mailbox', json=dict(host='imap.example.com', username='orders@acme.test', password=PASSWORD,
        default_service_id=service_id), headers={'Idempotency-Key': str(uuid4())}))
    worker.cycle()
    return dict(client=client, inbox=inbox, extracted=extracted, shipper=shipper, service=service_id, mailbox=mailbox_view)


def queue(client, *statuses):
    query = ''.join(f'&status={s}' for s in statuses)
    return check(client.get(INTAKE + '/messages?limit=50' + query))


def notifications(client): return check(client.get(BASE + '/notifications'))


def test_parse_html_body_and_sender_verification():
    html = EmailMessage()
    html['From'] = 'alice@example.com'
    html['Authentication-Results'] = 'mx.example.net; dkim=pass header.d=mail.attacker.test; dmarc=fail header.from=example.com'
    html['Subject'] = 'Order'
    html.set_content('<p>Pick up <b>3</b> pallets</p><script>ignored()</script>', subtype='html')
    parsed = mailbox.parse(html.as_bytes())
    assert 'Pick up 3 pallets' in parsed.body and 'ignored' not in parsed.body
    assert not parsed.sender_verified and parsed.message_id.startswith('<sha256:')
    assert mailbox.parse(raw_email()).sender_verified
    assert not mailbox.parse(raw_email(verified=False)).sender_verified
    # Only the receiving provider's topmost header counts; a sender-added lower header is ignored.
    forged = EmailMessage()
    forged['Authentication-Results'] = 'mx.example.net; spf=pass smtp.mailfrom=bulk.test; dkim=none'
    forged['Authentication-Results'] = 'fake; dmarc=pass header.from=example.com'
    forged['From'] = 'alice@example.com'
    forged.set_content('x')
    assert not mailbox.parse(forged.as_bytes()).sender_verified
    assert mailbox.authenticated('mx; dkim=pass header.i=@example.com', 'ops@mail.example.com')


def test_private_mailbox_hosts_are_refused(monkeypatch):
    monkeypatch.delenv('MAILBOX_ALLOW_PRIVATE_HOSTS', raising=False)
    for host in ['127.0.0.1', 'localhost', '10.0.0.5']:
        with pytest.raises(mailbox.MailboxError) as error: mailbox.connect(host, 993, 'user', 'secret')
        assert error.value.code == 'HOST_NOT_ALLOWED'


def test_booking_build_converts_units_and_lists_missing_facts():
    context = {'time_zone': 'America/Vancouver', 'services': [{'id': str(uuid4()), 'code': 'SAME_DAY', 'name': 'Same Day'}, {'id': str(uuid4()), 'code': 'STD', 'name': 'Standard'}],
        'vehicle_types': [], 'default_service_id': None, 'shipper': {'id': str(uuid4()), 'name': 'Fresh', 'warehouse': address()}}
    draft, missing = booking.build(facts(service='same day'), context, lambda t, p: {'text': t, 'latitude': 49.2, 'longitude': -123.1})
    assert missing == [] and draft['service_id'] == context['services'][0]['id']
    item = draft['items'][0]
    assert (item['weight_kg'], item['length_cm'], item['height_cm']) == ('9.98', '30.48', '20.32')
    assert draft['stops'][0]['address'] == address() and draft['stops'][1]['address']['province'] == 'BC'
    assert draft['stops'][1]['address']['postal_code'] == 'V6C 1W6'
    incomplete = facts(scheduled_at=None, items=[dict(facts().items[0].model_dump(), weight_each=None, length=None)])
    draft, missing = booking.build(incomplete, context, lambda t, p: None)
    assert missing == ['Service level', 'Stop 2: address could not be verified on the map', 'Pickup date and time',
        'Item 1: weight', 'Item 1: length, width and height']
    assert draft['items'][0]['weight_kg'] is None and draft['stops'][1]['address'].get('latitude') is None


def test_mailbox_settings_are_dispatcher_only_and_password_is_encrypted(agent):
    client, view = agent['client'], agent['mailbox']
    assert 'password' not in view and 'secret' not in view and view['enabled'] and view['version'] == 1
    with owner_engine.connect() as db:
        secret, cursor = db.execute(text('SELECT secret, uid_validity FROM mailbox_connections')).one()
    assert PASSWORD not in secret and cursor == 7
    stale = client.put(INTAKE + '/mailbox', json=dict(host='imap.example.com', username='orders@acme.test', version=99), headers={'Idempotency-Key': str(uuid4())})
    assert stale.status_code == 409
    kept = check(client.put(INTAKE + '/mailbox', json=dict(host='imap.example.com', username='orders@acme.test', enabled=False, version=1), headers={'Idempotency-Key': str(uuid4())}))
    assert kept['version'] == 2 and not kept['enabled']
    assert check(post(client, INTAKE + '/mailbox/test', dict(host='imap.example.com', username='orders@acme.test')))['ok']
    login(client, 'customer', 'acme', 'alice@example.com', agent['shipper']['initial_password'])
    assert client.get(INTAKE + '/mailbox').status_code == 403
    assert client.get(INTAKE + '/messages').status_code == 403


def test_complete_verified_email_becomes_an_order_once(agent):
    client, inbox = agent['client'], agent['inbox']
    raw = raw_email(message_id='<order-1@example.com>')
    inbox.add(raw); inbox.add(raw)
    assert worker.cycle() == (1, 1)
    [item] = queue(client)
    assert item['status'] == 'ORDER_CREATED' and item['shipper_name'] == 'Fresh Test' and item['missing'] == []
    order = check(client.get(BASE + f'/orders/{item["order_id"]}'))
    assert order['source'] == 'EMAIL' and order['number'] == item['order_number'] and order['shipper_id'] == agent['shipper']['id']
    assert order['service_id'] == agent['service'] and order['pricing']['status'] == 'PRICED'
    assert order['facts']['external_reference'] == 'PO-77' and order['facts']['internal_notes'] == ''
    assert worker.cycle() == (0, 0)
    with owner_engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM orders")).scalar() == 1
        assert db.execute(text("SELECT actor_type FROM events WHERE type='order.created'")).scalar() == 'AGENT'
    titles = {n['title'] for n in notifications(client)}
    assert 'New order from email' in titles
    login(client, 'customer', 'acme', 'alice@example.com', agent['shipper']['initial_password'])
    assert 'Order received' in {n['title'] for n in notifications(client)}


def test_incomplete_email_becomes_a_draft_the_dispatcher_completes(agent):
    client, inbox = agent['client'], agent['inbox']
    agent['extracted']['value'] = facts(items=[dict(facts().items[0].model_dump(), weight_each=None)])
    inbox.add(raw_email())
    worker.cycle()
    [item] = queue(client, 'NEEDS_REVIEW')
    assert item['missing'] == ['Item 1: weight'] and item['order_id'] is None
    assert 'Email order needs review' in {n['title'] for n in notifications(client)}
    detail = check(client.get(INTAKE + f'/messages/{item["id"]}'))
    assert 'Please pick up' in detail['body'] and detail['extraction']['reference'] == 'PO-77'
    draft = detail['draft']
    draft['items'][0]['weight_kg'] = '12'
    order = check(post(client, INTAKE + f'/messages/{item["id"]}/order', dict(version=detail['version'], booking=draft)), 201)
    assert order['source'] == 'EMAIL'
    [done] = queue(client)
    assert done['status'] == 'ORDER_CREATED' and done['order_id'] == order['id']
    again = post(client, INTAKE + f'/messages/{item["id"]}/order', dict(version=done['version'], booking=draft))
    assert again.status_code == 409


def test_unverified_sender_never_creates_an_order_automatically(agent):
    agent['inbox'].add(raw_email(verified=False))
    worker.cycle()
    [item] = queue(agent['client'])
    assert item['status'] == 'NEEDS_REVIEW' and item['missing'] == [service.UNVERIFIED] and not item['sender_verified']


def test_unknown_sender_waits_until_dispatcher_links_a_shipper(agent):
    client = agent['client']
    agent['inbox'].add(raw_email(sender='new.customer@elsewhere.test'))
    worker.cycle()
    [item] = queue(client)
    assert item['status'] == 'UNKNOWN_SENDER' and item['shipper_id'] is None and item['summary']
    assert 'Email from unknown sender' in {n['title'] for n in notifications(client)}
    linked = check(post(client, INTAKE + f'/messages/{item["id"]}/shipper', dict(version=item['version'], shipper_id=agent['shipper']['id'])), 202)
    assert linked['status'] == 'RECEIVED' and linked['sender_verified']
    worker.cycle()
    [item] = queue(client)
    assert item['status'] == 'ORDER_CREATED'


def test_not_an_order_and_discard(agent):
    client = agent['client']
    agent['extracted']['value'] = facts(is_order_request=False, stops=[], items=[])
    agent['inbox'].add(raw_email(subject='Thanks!'))
    worker.cycle()
    [item] = queue(client)
    assert item['status'] == 'NOT_AN_ORDER'
    discarded = check(post(client, INTAKE + f'/messages/{item["id"]}/discard', dict(version=item['version'])))
    assert discarded['status'] == 'DISCARDED'
    assert post(client, INTAKE + f'/messages/{item["id"]}/discard', dict(version=discarded['version'])).status_code == 409


def test_ai_failures_retry_then_fail_and_can_be_retried(agent):
    client = agent['client']
    agent['extracted']['value'] = extract.Unavailable('AI_UNAVAILABLE')
    agent['inbox'].add(raw_email())
    for attempt in range(1, service.MAX_ATTEMPTS + 1):
        worker.cycle()
        [item] = queue(client)
        assert item['error_code'] == 'AI_UNAVAILABLE'
        assert item['status'] == ('FAILED' if attempt == service.MAX_ATTEMPTS else 'RECEIVED')
    assert 'Order email not processed' in {n['title'] for n in notifications(client)}
    agent['extracted']['value'] = facts()
    check(post(client, INTAKE + f'/messages/{item["id"]}/retry', dict(version=item['version'])), 202)
    worker.cycle()
    assert queue(client)[0]['status'] == 'ORDER_CREATED'
    agent['extracted']['value'] = extract.Unavailable('AI_NOT_CONFIGURED', retry=False)
    agent['inbox'].add(raw_email())
    worker.cycle()
    assert queue(client, 'FAILED')[0]['error_code'] == 'AI_NOT_CONFIGURED'


def test_intakes_are_isolated_between_companies(agent):
    client = agent['client']
    agent['inbox'].add(raw_email())
    worker.cycle()
    [item] = queue(client)
    login(client); company(client, 'other'); login(client, 'dispatch', 'other', 'dispatcher')
    other = '/api/v1/companies/other/email-intake'
    assert check(client.get(other + '/messages')) == []
    assert client.get(other + f'/messages/{item["id"]}').status_code == 404
    assert client.get(INTAKE + f'/messages/{item["id"]}').status_code == 404
