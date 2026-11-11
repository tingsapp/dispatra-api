"""Partial save, completion link, authorization and final booking on disposable PostgreSQL."""
from copy import deepcopy
from uuid import uuid4
from sqlalchemy import text
from conftest import login, post, owner_engine
from test_email_intake import agent, facts, raw_email, check, queue, worker, BASE, INTAKE, address


def incomplete(agent):
    agent['extracted']['value'] = facts(items=[dict(facts().items[0].model_dump(), weight_each=None)])
    agent['inbox'].add(raw_email())
    worker.cycle()
    return queue(agent['client'], 'NEEDS_REVIEW')[0]


def save(client, row, draft, key=None):
    return client.put(INTAKE + f'/messages/{row["id"]}/draft', json={'version':row['version'],'draft':draft}, headers={'Idempotency-Key':key or str(uuid4())})


def shipper_login(agent):
    login(agent['client'], 'customer', 'acme', 'alice@example.com', agent['shipper']['initial_password'])


def request(agent, row, monkeypatch):
    monkeypatch.setenv('PUBLIC_WEB_URL', 'https://example.test')
    return check(post(agent['client'], INTAKE+f'/messages/{row["id"]}/request-details', {'version':row['version']}),202)


def test_partial_save_preserves_nulls_identity_body_and_concurrency(agent):
    row=incomplete(agent); client=agent['client']; draft=deepcopy(row['draft'])
    draft['stops'][1]['address']={'text':'456 Lougheed Highway','city':'Burnaby','province':'','postal_code':''}
    key=str(uuid4())
    saved=check(save(client,row,draft,key))
    assert saved['draft']['items'][0]['weight_kg'] is None
    assert saved['draft']['stops'][1]['address']['text']=='456 Lougheed Highway'
    assert saved['draft']['items'][0]['id']==draft['items'][0]['id']
    assert saved['body'].startswith('Please pick up') and saved['status']=='NEEDS_REVIEW'
    assert check(save(client,row,draft,key))==saved
    assert save(client,row,draft).status_code==409
    assert client.get(BASE+'/orders').json()==[]
    assert post(client,INTAKE+f'/messages/{row["id"]}/order',{'version':saved['version'],'booking':saved['draft']}).status_code==422


def test_create_order_cannot_bypass_unsaved_draft_blockers(agent):
    row = incomplete(agent)
    # The submitted payload looks complete, but the stored draft still has a
    # required weight missing. A dispatcher must save the correction first.
    booking = deepcopy(row['draft'])
    booking['items'][0]['weight_kg'] = '12'
    response = post(agent['client'], INTAKE + f'/messages/{row["id"]}/order', {'version': row['version'], 'booking': booking})
    assert response.status_code == 422
    assert 'Complete the draft before creating an Order' in response.json()['detail']
    assert agent['client'].get(BASE + '/orders').json() == []


def test_authenticated_shipper_completion_returns_to_dispatch_without_creating_order(agent, monkeypatch):
    row=incomplete(agent);client=agent['client'];identity=row['id']
    row=check(save(client,row,{**row['draft'],'internal_notes':'Dispatcher only','imported_total':'123'}))
    delivery=request(agent,row,monkeypatch)
    assert delivery['intake_id']==identity and delivery['status']=='PENDING'
    with owner_engine.connect() as db:
        email=db.execute(text('SELECT recipient, body_text FROM email_deliveries WHERE id=:id'),{'id':delivery['id']}).one()
        assert email.recipient=='alice@example.com'
        assert f'https://example.test/acme/shipper?draft={identity}' in email.body_text
    latest=check(client.get(INTAKE+f'/messages/{identity}'))
    assert post(client,INTAKE+f'/messages/{identity}/request-details',{'version':latest['version']}).status_code==409
    shipper_login(agent)
    own=check(client.get(INTAKE+f'/drafts/{identity}'))
    assert 'internal_notes' not in own['draft'] and 'imported_total' not in own['draft'] and 'body' not in own
    assert client.get(INTAKE+f'/messages/{identity}').status_code==403
    malicious={**own['draft'],'shipper_id':str(uuid4())}
    assert post(client,INTAKE+f'/drafts/{identity}/submit',{'version':own['version'],'draft':malicious}).status_code==422
    assert post(client,INTAKE+f'/drafts/{identity}/submit',{'version':own['version'],'draft':own['draft']}).status_code==422
    own['draft']['items'][0]['weight_kg']='12'
    key=str(uuid4());payload={'version':own['version'],'draft':own['draft']}
    result=check(post(client,INTAKE+f'/drafts/{identity}/submit',payload,key))
    assert result['submitted_at']
    assert check(post(client,INTAKE+f'/drafts/{identity}/submit',payload,key))==result
    assert post(client,INTAKE+f'/drafts/{identity}/submit',payload).status_code==409
    login(client,'dispatch','acme','dispatcher')
    finished=check(client.get(INTAKE+f'/messages/{identity}'))
    assert finished['draft']['items'][0]['weight_kg']=='12'
    assert finished['draft']['internal_notes']=='Dispatcher only' and finished['draft']['imported_total']=='123'
    assert finished['status']=='NEEDS_REVIEW' and finished['order_id'] is None
    assert 'Draft details received' in {n['title'] for n in client.get(BASE+'/notifications').json()}
    order=check(post(client,INTAKE+f'/messages/{identity}/order',{'version':finished['version'],'booking':finished['draft']}),201)
    assert order['source']=='EMAIL'
    shipper_login(agent)
    assert client.get(INTAKE+f'/drafts/{identity}').status_code in (404,409)


def test_completion_scope_and_stale_version(agent,monkeypatch):
    row=incomplete(agent);client=agent['client'];identity=row['id']
    shipper_login(agent)
    assert client.get(INTAKE+f'/drafts/{identity}').status_code==404
    login(client,'dispatch','acme','dispatcher')
    delivery=request(agent,row,monkeypatch)
    from app.operations import email_worker, smtp_transport
    sent=[]
    monkeypatch.setattr(smtp_transport,'send',lambda message,**kwargs:sent.append(str(message.id)))
    email_worker.poll_once('draft-completion-test')
    assert delivery['id'] in sent
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT status FROM email_deliveries WHERE id=:id'),{'id':delivery['id']})=='SENT'
    second=check(post(client,BASE+'/shippers',{'name':'Other','company_name':'Other','kind':'BUSINESS','email':'other@example.com','phone':'6045550198','warehouse':address()}),201)
    login(client,'customer','acme','other@example.com',second['initial_password'])
    assert client.get(INTAKE+f'/drafts/{identity}').status_code==404
    shipper_login(agent);old=check(client.get(INTAKE+f'/drafts/{identity}'))
    login(client,'dispatch','acme','dispatcher')
    latest=check(client.get(INTAKE+f'/messages/{identity}'))
    check(save(client,latest,{**latest['draft'],'external_reference':'Dispatcher edit'}))
    shipper_login(agent)
    old['draft']['items'][0]['weight_kg']='10'
    assert post(client,INTAKE+f'/drafts/{identity}/submit',{'version':old['version'],'draft':old['draft']}).status_code==409
    login(client,'dispatch','acme','dispatcher')
    latest=check(client.get(INTAKE+f'/messages/{identity}'))
    reassigned=check(save(client,latest,{**latest['draft'],'shipper_id':second['id']}))
    assert reassigned['completion_requested_at'] is None and not reassigned['sender_verified']
    shipper_login(agent)
    assert client.get(INTAKE+f'/drafts/{identity}').status_code==404
    login(client,'customer','acme','other@example.com',second['initial_password'])
    assert client.get(INTAKE+f'/drafts/{identity}').status_code==404


def test_partial_addresses_and_unspecified_service_are_not_guessed():
    from app.intake import booking
    context={'time_zone':'America/Vancouver','services':[{'id':str(uuid4()),'name':'Same Day','code':'SAME_DAY'}], 'vehicle_types':[], 'default_service_id':str(uuid4()),'shipper':None}
    value=facts(service=None)
    value.stops[1].address.postal_code=None
    value.stops[1].address.province=None
    draft,missing=booking.build(value,context,lambda *_:None)
    assert draft['service_id'] is None
    assert draft['stops'][1]['address']['text']=='500 Granville Street, Vancouver'
    assert draft['stops'][1]['address']['postal_code']==''
    assert 'Service level' in missing
