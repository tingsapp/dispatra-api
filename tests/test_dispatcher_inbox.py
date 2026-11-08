"""Admin/dispatcher isolation, paged history, search and incoming notifications."""
from uuid import uuid4
from sqlalchemy import text
from fastapi.testclient import TestClient
from app.main import app
from conftest import HEADERS, owner_engine, login, post, company
from test_manual_operations import setup, check, BASE, new_order, assign
from test_events import shipper_portal, sync
from test_chat import say


def notes(client): return [n for n in check(client.get(BASE + '/notifications')) if n['kind'] == 'message.created']


def test_admin_dispatcher_chat_private_and_notifies_both_sides(setup):
    w=setup; dispatcher=w['client']
    uid=check(dispatcher.get('/api/v1/auth/me'))['id']
    with TestClient(app, headers=HEADERS) as admin:
        login(admin)
        contact=check(admin.get('/api/v1/platform/chat-contacts'))['items']
        assert {r['id'] for r in contact} == {uid}
        assert check(dispatcher.get(BASE+'/admin-chat'))['conversation_id'] is None
        key=str(uuid4()); sent=check(post(dispatcher,BASE+'/admin-chat/messages',{'body':'Need help'},key),201)
        assert check(post(dispatcher,BASE+'/admin-chat/messages',{'body':'Need help'},key),201)==sent
        cid=sent['conversation_id']
        inbox=check(admin.get('/api/v1/platform/chats'))
        assert len(inbox['items'])==1 and inbox['items'][0]['dispatcher_id']==uid and inbox['items'][0]['unread']==1
        notification=check(admin.get('/api/v1/platform/notifications'))['items'][0]
        assert notification['target']=='chat' and notification['conversation_id']==cid
        assert check(admin.get('/api/v1/platform/chat-unread'))['unread']==1
        assert check(post(admin,f'/api/v1/platform/chats/{cid}/read',{'seq':sent['seq']}))['notifications_unread']==0
        reply=check(post(admin,f'/api/v1/platform/dispatchers/{uid}/chat/messages',{'body':'Happy to help'}),201)
        assert reply['conversation_id']==cid and reply['sender_role']=='ADMIN'
        assert notes(dispatcher)[0]['conversation_id']==cid and sync(dispatcher)['unread_messages']==1
        assert check(post(dispatcher,BASE+f'/chats/{cid}/read',{'seq':reply['seq']}))['notifications_unread']==0
        assert len(check(dispatcher.get(BASE+f'/chats/{cid}/messages'))['messages'])==2
        # Owner cannot use the company endpoints or see any operational conversation through admin endpoints.
        driver_message=check(post(w['mobile'],BASE+f'/drivers/{w["driver"]["id"]}/chat/messages',{'body':'Driver private'}),201)
        assert admin.get(BASE+'/chat-inbox').status_code==404
        for suffix in ['', '/messages']:
            assert admin.get('/api/v1/platform/chats/'+driver_message['conversation_id']+suffix).status_code==404
        assert post(admin,'/api/v1/platform/chats/'+driver_message['conversation_id']+'/read',{'seq':driver_message['seq']}).status_code==404
        assert len(check(admin.get('/api/v1/platform/chats'))['items'])==1
        assert len(check(admin.get('/api/v1/platform/notifications'))['items'])==1
        with shipper_portal(w) as shipper:
            for excluded in [shipper,w['mobile']]:
                assert excluded.get(BASE+'/admin-chat').status_code==403
                assert excluded.get(BASE+f'/chats/{cid}/messages').status_code==404
                assert excluded.get('/api/v1/platform/chats').status_code==403
        assert admin.get(f'/api/v1/platform/dispatchers/{w["driver"]["id"]}/chat').status_code==404


def test_dispatcher_support_isolated_from_other_company(setup):
    w=setup; sent=check(post(w['client'],BASE+'/admin-chat/messages',{'body':'Private'}),201)
    with TestClient(app,headers=HEADERS) as foreign:
        login(foreign); company(foreign,'other'); login(foreign,'dispatch','other','dispatcher')
        for suffix in ['', '/messages']:
            assert foreign.get('/api/v1/companies/other/chats/'+sent['conversation_id']+suffix).status_code==404
        assert check(foreign.get('/api/v1/companies/other/chat-inbox'))['items']==[]


def test_inbox_search_pagination_unread_and_complete_history(setup):
    w=setup; d=w['client']
    driver_path=BASE+f'/drivers/{w["driver"]["id"]}/chat/messages'
    first=check(post(w['mobile'],driver_path,{'body':'needle in driver history'}),201)
    with shipper_portal(w) as shipper:
        second=check(post(shipper,BASE+f'/shippers/{w["shipper"]["id"]}/chat/messages',{'body':'Shipper question'}),201)
    support=check(post(d,BASE+'/admin-chat/messages',{'body':'Admin help'}),201)
    all_rows=[];cursor=None
    while True:
        page=check(d.get(BASE+'/chat-inbox',params={'limit':1, **({'cursor':cursor} if cursor else {})}))
        all_rows.extend(page['items']);cursor=page['next_cursor']
        if not cursor:break
    assert len(all_rows)==3 and len({r['conversation_id'] for r in all_rows})==3
    assert len(check(d.get(BASE+'/chat-inbox',params={'unread_only':True}))['items'])==2
    assert check(d.get(BASE+'/chat-inbox',params={'search':'needle'}))['items'][0]['conversation_id']==first['conversation_id']
    assert len(check(d.get(BASE+'/chat-inbox',params={'kind':'DISPATCH_SHIPPER'}))['items'])==1
    assert not check(d.get(BASE+'/chat-inbox',params={'search':'%'}))['items']
    assert d.get(BASE+'/chat-inbox',params={'cursor':'bad'}).status_code==422
    # Bulk seed old history in the disposable database; HTTP reads still use the restricted role.
    with owner_engine.begin() as db:
        db.execute(text("""INSERT INTO conversation_messages(id,organization_id,conversation_id,driver_id,sender_user_id,sender_role,sender_name,body,created_at)
            SELECT gen_random_uuid(),organization_id,conversation_id,driver_id,sender_user_id,sender_role,sender_name,'Older message '||n,created_at
            FROM conversation_messages CROSS JOIN generate_series(1,225) n WHERE id=:id"""),{'id':first['id']})
    received=[];before=None
    while True:
        page=check(d.get(BASE+f'/chats/{first["conversation_id"]}/messages',params={'limit':50,**({'before':before} if before else {})}))
        received+=page['messages']
        if not page['has_more']:break
        before=page['messages'][0]['seq']
    assert len(received)==226 and len({m['id'] for m in received})==226


def test_every_company_message_notifies_without_duplicate_retry(setup):
    w=setup; d=w['client'];path=BASE+f'/drivers/{w["driver"]["id"]}/chat/messages'
    key=str(uuid4()); first=check(post(w['mobile'],path,{'body':'first'},key),201)
    check(post(w['mobile'],path,{'body':'first'},key),201)
    second=check(post(w['mobile'],path,{'body':'second'}),201)
    assert len(notes(d))==2 and all(n['conversation_id']==first['conversation_id'] for n in notes(d))
    assert not notes(w['mobile'])
    check(post(d,BASE+f'/chats/{first["conversation_id"]}/read',{'seq':first['seq']}))
    assert len([n for n in notes(d) if not n['read_at']])==1
    check(post(d,path,{'body':'dispatch reply'}),201)
    assert len(notes(w['mobile']))==1


def test_replacement_dispatcher_cannot_read_previous_admin_chat(setup):
    w=setup; d=w['client']; uid=check(d.get('/api/v1/auth/me'))['id']; second_id=uuid4()
    with TestClient(app,headers=HEADERS) as admin:
        login(admin)
        sent=check(post(d,BASE+'/admin-chat/messages',{'body':'Private support request'}),201)
        cid=sent['conversation_id']
        check(post(admin,f'/api/v1/platform/dispatchers/{uid}/chat/messages',{'body':'Private reply'}),201)
        assert len(notes(d))==1
        # The product permits one active dispatcher per company. Replacing it must not transfer private history.
        with owner_engine.begin() as db:
            db.execute(text('UPDATE users SET active=false WHERE id=:id'),{'id':uid})
            db.execute(text("""INSERT INTO users(id,organization_id,scope,login_id,password_hash,role,active,created_at,display_name)
                SELECT :id,organization_id,scope,'second-dispatcher',password_hash,'DISPATCHER',true,created_at,'Second dispatcher'
                FROM users WHERE id=:original"""),{'id':second_id,'original':uid})
        with TestClient(app,headers=HEADERS) as second:
            login(second,'dispatch','acme','second-dispatcher')
            assert second.get(BASE+f'/chats/{cid}').status_code==404
            assert second.get(BASE+f'/chats/{cid}/messages').status_code==404
            assert not check(second.get(BASE+'/chat-inbox'))['items'] and not notes(second)
            assert check(second.get(BASE+'/admin-chat'))['conversation_id'] is None
            channel=check(admin.get(f'/api/v1/platform/chats/{cid}'))
            assert not channel['can_send'] and channel['closed_reason']
            assert post(admin,f'/api/v1/platform/dispatchers/{uid}/chat/messages',{'body':'Must not send'}).status_code==409
            assert len(check(admin.get(f'/api/v1/platform/chats/{cid}/messages'))['messages'])==2


def test_chat_migration_preserves_messages_and_backfills_notification_links(setup):
    from alembic import command
    from alembic.config import Config
    w=setup
    sent=check(post(w['mobile'],BASE+f'/drivers/{w["driver"]["id"]}/chat/messages',{'body':'Keep this history'}),201)
    note=notes(w['client'])[0]
    config=Config('alembic.ini')
    command.downgrade(config,'0063_shipper_logo')
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT body FROM conversation_messages WHERE id=:id'),{'id':sent['id']})=='Keep this history'
    command.upgrade(config,'head')
    restored=notes(w['client'])[0]
    assert restored['id']==note['id'] and restored['conversation_id']==sent['conversation_id']
    assert check(w['client'].get(BASE+f'/chats/{sent["conversation_id"]}/messages'))['messages'][0]['id']==sent['id']
