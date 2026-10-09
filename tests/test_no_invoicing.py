"""V1 completion, removed contract and data-preserving invoicing retirement."""
import json
from uuid import uuid4
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.main import app
from app.models import Base
from conftest import owner_engine, post
from test_manual_operations import setup, new_order, booking, check, BASE


def test_invoicing_is_absent_from_api_contract_and_runtime_models():
    contract = app.openapi()
    assert not any('invoice' in path for path in contract['paths'])
    assert 'InvoiceView' not in contract['components']['schemas']
    assert 'Finalize' not in contract['components']['schemas']
    assert 'invoice_id' not in contract['components']['schemas']['EmailDeliveryView']['properties']
    assert 'invoices' not in Base.metadata.tables
    assert 'invoice_id' not in Base.metadata.tables['email_deliveries'].columns
    for schema in ['ShipperData', 'ShipperView']:
        assert 'terms' not in contract['components']['schemas'][schema]['properties']
    assert 'terms' not in Base.metadata.tables['shippers'].columns
    for name, schema in contract['components']['schemas'].items():
        if name.startswith('RateData'):
            assert 'settle_actual' not in schema['properties']


def test_retirement_archives_legacy_invoices_and_cancels_old_sends(setup):
    w = setup
    client = w['client']
    order = new_order(w)
    quote = check(post(client, BASE + '/quotes', booking(None, w['service'], w['type_id'], rate=w['fixed'])), 201)
    live_mail = check(post(client, BASE + f'/quotes/{quote["id"]}/send',
        {'version':quote['version'], 'recipient':'quote@example.com'}), 202)
    requested_by = check(client.get('/api/v1/auth/me'))['id']
    invoice_id, mail_id, event_id = str(uuid4()), str(uuid4()), str(uuid4())
    config = Config('alembic.ini')
    command.downgrade(config, '0027_vehicle_unit_numbers')
    try:
        with owner_engine.begin() as db:
            db.execute(text("SELECT set_config('app.platform','true',true)"))
            organization_id = db.scalar(text('SELECT organization_id FROM orders WHERE id = :id'), {'id':order['id']})
            db.execute(text("UPDATE shippers SET terms='NET60' WHERE id=:id"), {'id':w['shipper']['id']})
            db.execute(text("UPDATE orders SET status='INVOICED', completed_at=now() WHERE id=:id"),{'id':order['id']})
            db.execute(text("""INSERT INTO invoices (id, organization_id, order_id, number, snapshot, subtotal, tax, total, version, created_at, updated_at)
                VALUES (:id,:org,:order,'INV-HISTORY',CAST(:snapshot AS jsonb),100,5,105,1,now(),now())"""),
                {'id':invoice_id,'org':organization_id,'order':order['id'],'snapshot':json.dumps({'pricing':order['pricing']})})
            db.execute(text("""INSERT INTO email_deliveries (id, organization_id, invoice_id, requested_by, recipient, subject, body_text, body_html,
                status, message_id, version, created_at, updated_at)
                VALUES (:id,:org,:invoice,:actor,'history@example.test','Old invoice','Saved body','Saved body','PENDING',:message,1,now(),now())"""),
                {'id':mail_id,'org':organization_id,'invoice':invoice_id,'actor':requested_by,'message':str(uuid4())})
            db.execute(text("""INSERT INTO outbox_events (id, organization_id, event_type, entity_id, created_at, available_at, attempts)
                VALUES (:id,:org,'email.requested',:mail,now(),now(),0)"""),{'id':event_id,'org':organization_id,'mail':mail_id})
        command.upgrade(config, 'head')
        done = check(client.get(BASE + f'/orders/{order["id"]}'))
        assert done['status'] == 'COMPLETED' and done['pricing'] == order['pricing']
        with owner_engine.begin() as db:
            assert db.scalar(text('SELECT snapshot FROM archived_invoices WHERE id=:id'), {'id':invoice_id}) == {'pricing':order['pricing']}
            assert db.scalar(text('SELECT status FROM archived_invoice_email_deliveries WHERE id=:id'),{'id':mail_id}) == 'PENDING'
            assert db.scalar(text('SELECT count(*) FROM email_deliveries WHERE id=:id'),{'id':mail_id}) == 0
            assert db.scalar(text('SELECT published_at FROM outbox_events WHERE id=:id'),{'id':event_id}) is not None
            assert db.scalar(text('SELECT status FROM email_deliveries WHERE id=:id'),{'id':live_mail['id']}) == 'PENDING'
            assert db.scalar(text("SELECT has_table_privilege('dispatra_app','archived_invoices','SELECT')")) is False
            assert db.scalar(text("SELECT has_table_privilege('dispatra_app','archived_shipper_payment_terms','SELECT')")) is False
            assert db.scalar(text('SELECT terms FROM archived_shipper_payment_terms WHERE id=:id'), {'id':w['shipper']['id']}) == 'NET60'
            with pytest.raises(IntegrityError):
                with db.begin_nested(): db.execute(text("UPDATE orders SET status='INVOICED' WHERE id=:id"), {'id':order['id']})
        for path in ['/invoices', f'/invoices/{invoice_id}', f'/invoices/{invoice_id}/document']:
            assert client.get(BASE + path).status_code == 404
        assert post(client, BASE + f'/orders/{order["id"]}/invoice', {'version':done['version']}).status_code == 404
        command.downgrade(config, '0027_vehicle_unit_numbers')
        with owner_engine.begin() as db:
            assert db.scalar(text('SELECT count(*) FROM invoices WHERE id=:id'), {'id':invoice_id}) == 1
            assert db.scalar(text('SELECT invoice_id FROM email_deliveries WHERE id=:id'), {'id':mail_id}) == uuid_from(invoice_id)
            assert db.scalar(text('SELECT published_at FROM outbox_events WHERE id=:id'),{'id':event_id}) is not None
            assert db.scalar(text('SELECT terms FROM shippers WHERE id=:id'), {'id':w['shipper']['id']}) == 'NET60'
    finally:
        command.upgrade(config, 'head')


def test_new_quotes_and_bookings_omit_retired_financial_defaults(setup):
    w = setup
    with owner_engine.begin() as db:
        db.execute(text("UPDATE rate_cards SET data=data || jsonb_build_object('settle_actual',true) WHERE id=:id"), {'id':w['fixed']})
    order = new_order(w)
    assert order['pricing']['status'] == 'PRICED'
    assert 'settle_actual' not in order['pricing']['context']['card']
    assert all('terms' not in party for party in order['booking'].values())
    quote = check(post(w['client'], BASE + '/quotes', booking(None, w['service'], w['type_id'], rate=w['fixed'])), 201)
    assert 'settle_actual' not in quote['pricing']['context']['card']


def uuid_from(value):
    from uuid import UUID
    return UUID(value)
