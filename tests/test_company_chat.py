"""Persistent company chat: restricted-role PostgreSQL, recipients, replay and event isolation."""
from uuid import uuid4
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from conftest import post, login, company, HEADERS, owner_engine
from test_manual_operations import setup, check, address, new_order, assign, BASE
from test_events import shipper_portal, sync


def path(w): return BASE + f'/drivers/{w["driver"]["id"]}/chat'


def test_company_chat_without_order_or_duty_and_retry_safe_reads(setup):
    w = setup
    dispatcher, driver = w['client'], w['mobile']
    check(post(driver, BASE + f'/driver/duty/{w["duty"]["id"]}/end', {'version':w['duty']['version'], 'ended_at':datetime.now(timezone.utc).isoformat()}))
    empty = check(driver.get(path(w)))
    assert empty['title'] == 'Company' and empty['conversation_id'] is None and empty['can_send']
    assert check(dispatcher.get(path(w)))['title'] == 'Dana Driver'
    with owner_engine.connect() as db: assert db.scalar(text('SELECT count(*) FROM conversations')) == 0
    key = str(uuid4())
    first = check(post(driver, path(w) + '/messages', {'body':'Vehicle question'}, key), 201)
    assert check(post(driver, path(w) + '/messages', {'body':'Vehicle question'}, key), 201) == first
    assert post(driver, path(w) + '/messages', {'body':'Changed text'}, key).status_code == 409
    cid = first['conversation_id']
    inbox = check(dispatcher.get(BASE + '/chats'))
    assert len(inbox) == 1 and inbox[0]['order_id'] is None and inbox[0]['order_number'] is None
    assert inbox[0]['driver_id'] == w['driver']['id'] and inbox[0]['unread'] == 1
    assert sync(dispatcher)['unread_messages'] == 1
    reply = check(post(dispatcher, path(w) + '/messages', {'body':'Please call when safely stopped.'}), 201)
    assert reply['conversation_id'] == cid
    assert check(driver.get(path(w)))['unread'] == 1
    check(post(driver, BASE + f'/chats/{cid}/read', {'seq':reply['seq']}))
    assert check(driver.get(path(w)))['unread'] == 0
    assert len(check(driver.get(BASE + f'/chats/{cid}/messages'))['messages']) == 2
    # Assignment/release changes do not replace or close the company conversation.
    check(post(driver, BASE + '/driver/duty', {}), 201)
    order = new_order(w); route = assign(w, order)
    check(post(dispatcher, BASE + f'/routes/{route["id"]}/release', {'version':route['version'],'generation':route['generation']}))
    assert check(driver.get(path(w)))['conversation_id'] == cid
    assert check(driver.get(path(w)))['can_send']


def test_company_chat_isolated_from_shipper_other_driver_and_other_company(setup):
    w = setup
    dispatcher, driver = w['client'], w['mobile']
    first = check(post(driver, path(w) + '/messages', {'body':'Private company question'}), 201)
    cid = first['conversation_id']
    with shipper_portal(w) as shipper:
        assert shipper.get(path(w)).status_code == 404
        assert post(shipper, path(w) + '/messages', {'body':'Not a participant'}).status_code == 404
        assert shipper.get(BASE + f'/chats/{cid}/messages').status_code == 404
        assert check(shipper.get(BASE + '/chats')) == []
        assert sync(shipper)['unread_messages'] == 0
        assert not [c for c in sync(shipper)['changes'] if c['entity'] == 'message']
    second = check(post(dispatcher, BASE + '/drivers', {'name':'Second Driver','email':'second@example.com','phone':'6045550198','address':address()}), 201)
    with TestClient(app, headers=HEADERS) as other:
        login(other, 'driver', 'acme', second['email'], second['initial_password'])
        assert other.get(path(w)).status_code == 404
        assert other.get(BASE + f'/chats/{cid}/messages').status_code == 404
        assert post(other, BASE + f'/chats/{cid}/read', {'seq':first['seq']}).status_code == 404
        assert check(other.get(BASE + '/chats')) == []
    with TestClient(app, headers=HEADERS) as foreign:
        login(foreign); company(foreign,'other'); login(foreign,'dispatch','other','dispatcher')
        assert foreign.get('/api/v1/companies/other' + path(w)[len(BASE):]).status_code == 404
        assert foreign.get(f'/api/v1/companies/other/chats/{cid}/messages').status_code == 404


def test_company_chat_concurrent_first_send_creates_one_thread(setup):
    w = setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(post, client, path(w) + '/messages', {'body':body})
            for client, body in [(w['client'],'Dispatch first'), (w['mobile'],'Driver first')]]
        messages = [check(f.result(),201) for f in futures]
    assert messages[0]['conversation_id'] == messages[1]['conversation_id']
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM conversations WHERE order_id IS NULL')) == 1
        assert db.scalar(text('SELECT count(*) FROM conversation_messages')) == 2


def test_company_push_has_no_order_or_message_content(setup):
    from test_mobile_driver import native_login
    from test_push import register, pushes
    w = setup
    with TestClient(app, headers={'X-Requested-With':'Dispatra'}) as native:
        native_login(native,w); register(native)
        sent = check(post(w['client'], path(w) + '/messages', {'body':'Private vehicle issue'}),201)
        queued = pushes()
        assert len(queued) == 1
        assert queued[0]['data'] == {'type':'company_chat','conversation_id':sent['conversation_id']}
        assert 'Private' not in queued[0]['body'] and 'order' not in queued[0]['body'].lower()
        check(post(native, path(w) + '/messages', {'body':'Driver reply'}),201)
        assert len(pushes()) == 1


def test_inactive_driver_company_chat_becomes_read_only(setup):
    w = setup
    sent = check(post(w['mobile'], path(w) + '/messages', {'body':'Before deactivation'}),201)
    latest = check(w['client'].get(BASE + f'/drivers/{w["driver"]["id"]}'))
    response = w['client'].put(BASE + f'/drivers/{latest["id"]}', json={'version':latest['version'],'data':{**w['driver_body'],'active':False}},headers={'Idempotency-Key':str(uuid4())})
    check(response)
    channel = check(w['client'].get(path(w)))
    assert not channel['can_send'] and 'inactive' in channel['closed_reason']
    assert post(w['client'], path(w) + '/messages', {'body':'Must not send'}).status_code == 409
    assert check(w['client'].get(BASE + f'/chats/{sent["conversation_id"]}/messages'))['messages'][0]['body'] == 'Before deactivation'
