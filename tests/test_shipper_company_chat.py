"""One private company conversation across orders, with immutable legacy history."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from conftest import post, login, company, HEADERS, owner_engine
from test_manual_operations import setup, check, new_order, assign, finish, BASE
from test_events import shipper_portal, sync
from test_chat import say, thread


def path(w): return BASE + f'/shippers/{w["shipper"]["id"]}/chat'


def second_shipper(w):
    return check(post(w['client'], BASE + '/shippers', {**w['shipper_body'], 'name':'Second Shipper',
        'company_name':'Second Company', 'email':'second-shipper@example.com','phone':'6045550198'}), 201)


def test_shipper_company_chat_without_orders_and_retry_safe_reads(setup):
    w = setup
    with shipper_portal(w) as shipper:
        empty = check(shipper.get(path(w)))
        assert empty['conversation_id'] is None and empty['can_send'] and empty['title'] == 'Company'
        with owner_engine.connect() as db: assert db.scalar(text('SELECT count(*) FROM conversations')) == 0
        key = str(uuid4())
        sent = check(post(shipper, path(w) + '/messages', {'body':'Company question'}, key),201)
        assert check(post(shipper, path(w) + '/messages', {'body':'Company question'}, key),201) == sent
        assert post(shipper, path(w) + '/messages', {'body':'Different'}, key).status_code == 409
        assert check(w['client'].get(path(w)))['unread'] == 1
        reply = check(post(w['client'], path(w) + '/messages', {'body':'Company reply'}),201)
        assert reply['conversation_id'] == sent['conversation_id']
        assert check(shipper.get(path(w)))['unread'] == 1
        check(post(shipper, BASE + f'/chats/{reply["conversation_id"]}/read', {'seq':reply['seq']}))
        assert check(shipper.get(path(w)))['unread'] == 0
        assert sync(w['mobile'])['unread_messages'] == 0
        assert not [event for event in sync(w['mobile'])['changes'] if event['entity'] == 'message']
        other = second_shipper(w)
        other_path = BASE + f'/shippers/{other["id"]}/chat'
        other_message = check(post(w['client'], other_path + '/messages', {'body':'Separate company thread'}),201)
        assert other_message['conversation_id'] != sent['conversation_id']
        assert shipper.get(other_path).status_code == 404
        assert shipper.get(BASE + f'/chats/{other_message["conversation_id"]}/messages').status_code == 404
        order = new_order(w)
        order_channels = check(shipper.get(BASE + f'/orders/{order["id"]}/chats'))
        assert order_channels[0]['conversation_id'] == sent['conversation_id']
        route = assign(w, order); finish(w, route)
        assert check(shipper.get(path(w)))['conversation_id'] == sent['conversation_id']
        assert check(shipper.get(path(w)))['can_send'], 'Company chat remains open after an order completes'


def test_company_history_combines_orders_without_rewriting_messages_or_read_state(setup):
    w = setup
    with shipper_portal(w) as shipper:
        first_order, second_order = new_order(w), new_order(w)
        first = say(w['client'], first_order, 'DISPATCH_SHIPPER', 'First order history')
        second = say(w['client'], second_order, 'DISPATCH_SHIPPER', 'Second order history')
        assert first['conversation_id'] != second['conversation_id']
        with owner_engine.connect() as db:
            before = db.execute(text('SELECT id, conversation_id, body, seq FROM conversation_messages ORDER BY seq')).all()
        channel = check(shipper.get(path(w)))
        assert channel['unread'] == 2 and channel['conversation_id']
        history = thread(shipper, channel['conversation_id'])['messages']
        assert [m['body'] for m in history] == ['First order history','Second order history']
        # Marking the shared history read covers its older order threads monotonically.
        check(post(shipper, BASE + f'/chats/{channel["conversation_id"]}/read', {'seq':second['seq']}))
        check(post(shipper, BASE + f'/chats/{channel["conversation_id"]}/read', {'seq':first['seq']}))
        assert check(shipper.get(path(w)))['unread'] == 0
        sent = check(post(shipper, path(w) + '/messages', {'body':'New ongoing reply'}),201)
        assert [m['body'] for m in thread(shipper,sent['conversation_id'])['messages']] == ['First order history','Second order history','New ongoing reply']
        inbox = check(w['client'].get(BASE + '/chats'))
        assert len(inbox) == 1 and inbox[0]['order_id'] is None
        assert inbox[0]['shipper_id'] == w['shipper']['id'] and inbox[0]['unread'] == 1
        assert inbox[0]['last_message']['body'] == 'New ongoing reply'
        with owner_engine.connect() as db:
            after = db.execute(text('SELECT id, conversation_id, body, seq FROM conversation_messages ORDER BY seq LIMIT 2')).all()
        assert after == before, 'Legacy history must stay immutable'
        assign(w,first_order)
        direct = say(w['mobile'], first_order, 'DRIVER_SHIPPER', 'Private order chat')
        assert direct['body'] not in [m['body'] for m in thread(shipper,sent['conversation_id'])['messages']]


def test_shipper_company_chat_denies_other_participants_and_tenants(setup):
    w = setup
    with shipper_portal(w) as shipper:
        sent = check(post(shipper,path(w) + '/messages',{'body':'Private shipper company chat'}),201)
    for url in [path(w), BASE + f'/chats/{sent["conversation_id"]}/messages']:
        assert w['mobile'].get(url).status_code == 404
    assert post(w['mobile'],path(w) + '/messages',{'body':'Not my chat'}).status_code == 404
    other = second_shipper(w)
    with TestClient(app,headers=HEADERS) as stranger:
        login(stranger,'customer','acme',other['email'],other['initial_password'])
        assert stranger.get(path(w)).status_code == 404
        assert stranger.get(BASE + f'/chats/{sent["conversation_id"]}/messages').status_code == 404
        assert post(stranger, BASE + f'/chats/{sent["conversation_id"]}/read', {'seq':sent['seq']}).status_code == 404
        assert check(stranger.get(BASE + '/chats')) == []
        assert sync(stranger)['unread_messages'] == 0
    with TestClient(app,headers=HEADERS) as foreign:
        login(foreign); company(foreign,'other'); login(foreign,'dispatch','other','dispatcher')
        assert foreign.get('/api/v1/companies/other' + path(w)[len(BASE):]).status_code == 404
        assert foreign.get(f'/api/v1/companies/other/chats/{sent["conversation_id"]}/messages').status_code == 404


def test_concurrent_first_shipper_company_sends_create_one_conversation(setup):
    w = setup
    with shipper_portal(w) as shipper, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(post, client, path(w) + '/messages', {'body':body}) for client,body in [(w['client'],'Dispatch hello'),(shipper,'Shipper hello')]]
        messages = [check(future.result(),201) for future in futures]
        assert len({m['conversation_id'] for m in messages}) == 1
        assert len(thread(shipper,messages[0]['conversation_id'])['messages']) == 2


def test_inactive_shipper_company_chat_keeps_read_only_history(setup):
    w = setup
    sent = check(post(w['client'],path(w) + '/messages',{'body':'History before inactive'}),201)
    current = check(w['client'].get(BASE + f'/shippers/{w["shipper"]["id"]}'))
    check(w['client'].put(BASE + f'/shippers/{current["id"]}',json={'version':current['version'],'data':w['shipper_body'],'status':'INACTIVE'},headers={'Idempotency-Key':str(uuid4())}))
    view = check(w['client'].get(path(w)))
    assert not view['can_send'] and 'inactive' in view['closed_reason']
    assert post(w['client'],path(w) + '/messages',{'body':'Should be blocked'}).status_code == 409
    assert thread(w['client'],sent['conversation_id'])['messages'][0]['body'] == 'History before inactive'


def test_inbox_limit_counts_company_conversations_instead_of_legacy_order_threads(setup):
    w = setup
    other = second_shipper(w)
    check(post(w['client'],BASE + f'/shippers/{other["id"]}/chat/messages',{'body':'Other company history'}),201)
    for number in range(2):
        say(w['client'],new_order(w),'DISPATCH_SHIPPER',f'Latest legacy order {number}')
    inbox = check(w['client'].get(BASE + '/chats?limit=2'))
    assert len(inbox) == 2
    assert {row['shipper_id'] for row in inbox} == {w['shipper']['id'],other['id']}
