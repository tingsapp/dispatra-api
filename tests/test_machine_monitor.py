from datetime import timedelta
from uuid import UUID, uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import now
from app.agents.models import AgentRun, AgentLog
from app.agents.machine_events import WORKSPACE_RECORDS_READ
from app.events.models import Event
from app.operations.models import Order, Settings
from app.dispatch.models import DispatchDecision
from conftest import login, owner_engine
from test_manual_operations import setup, new_order, assign, finish

ROOT = '/api/v1/platform/agents/monitor'


def view(client):
    response = client.get(ROOT)
    assert response.status_code == 200, response.text
    return response.json()


def test_machine_owner_only_empty_is_unknown_and_contains_no_fabricated_work(client):
    assert client.get(ROOT).status_code == 401
    login(client)
    machine = view(client)['machine']
    assert all(row['state'] == 'UNKNOWN' for row in machine['agents'])
    assert not any(machine['workload'].values())
    assert machine['activity'] == []
    assert machine['activity_truncated'] is False


def test_committed_order_assignment_and_delivery_handoffs_use_real_public_references(client, setup):
    w = setup
    order = new_order(w)
    route = assign(w, order)
    finish(w, route)
    login(client)
    machine = view(client)['machine']
    assert machine['workload']['completed_24h'] == 1
    assert machine['workload']['orders_in_progress'] == 0
    activities = {row['kind']: row for row in machine['activity']}
    for kind, start, end in [('order.created', 'inputs', 'data'), ('order.assigned', 'DISPATCH', 'delivery'), ('order.started', 'delivery', 'data'), ('order.completed', 'delivery', 'data')]:
        assert activities[kind]['from_node'] == start
        assert activities[kind]['to_node'] == end
        assert activities[kind]['reference'] == order['number']
        assert activities[kind]['company_name'] == 'Acme'
    serialized = str(machine)
    for private in ['alice@example.com', 'dana@example.com', 'recipient_name', 'content_base64', 'facts', 'booking', 'pricing']:
        assert private not in serialized
    login(client, 'dispatch', 'acme', 'dispatcher')
    assert client.get(ROOT).status_code == 403
    assert w['mobile'].get(ROOT).status_code == 403


def test_current_work_prioritizes_fresh_parallel_run_and_exposes_error_and_queue_reasons(client, setup):
    order = new_order(setup)
    at = now()
    with Session(owner_engine) as db, db.begin():
        saved = db.scalar(select(Order).where(Order.id == UUID(order['id'])))
        settings = db.scalar(select(Settings).where(Settings.organization_id == saved.organization_id))
        settings.data = {**settings.data, 'dispatch_mode': 'AUTO'}
        db.add(DispatchDecision(organization_id=saved.organization_id, order_id=saved.id, order_version=saved.version, mode='AUTO', status='NO_CANDIDATE'))
        current = AgentRun(agent='DISPATCH', status='RUNNING', step='Checking actual candidate feasibility.', progress=45, created_at=at - timedelta(seconds=5), heartbeat_at=at)
        db.add(current);db.flush()
        db.add(AgentLog(run_id=current.id, organization_id=saved.organization_id, message='Checking actual candidate feasibility.'))
        db.add(AgentRun(agent='DISPATCH', status='SUCCEEDED', step='Run completed', progress=100, created_at=at - timedelta(seconds=1), heartbeat_at=at, finished_at=at))
        failed = AgentRun(agent='CHAT', status='FAILED', step='Run completed with errors', progress=100, heartbeat_at=at, finished_at=at)
        db.add(failed);db.flush()
        db.add(AgentLog(run_id=failed.id, level='ERROR', message='AI answers unavailable; company search results returned.'))
        db.add(AgentRun(agent='TECHNICAL', status='QUEUED', step='Requested check', heartbeat_at=at))
        current_id = current.id
        db.add(AgentRun(agent='OPERATIONAL', status='RUNNING', step='Reviewing orders', heartbeat_at=at - timedelta(minutes=6)))
    login(client)
    first = view(client);machine = first['machine'];states = {row['agent']: row for row in machine['agents']}
    assert first['running'] == 1
    assert states['DISPATCH']['state'] == 'PROCESSING'
    assert states['DISPATCH']['detail'] == 'Checking actual candidate feasibility.'
    assert states['DISPATCH']['company_name'] == 'Acme'
    assert machine['workload']['no_driver'] == machine['workload']['auto_waiting'] == 1
    assert states['CHAT']['state'] == 'ERROR' and 'unavailable' in states['CHAT']['detail']
    assert states['OPERATIONAL']['state'] == 'ERROR' and 'overdue' in states['OPERATIONAL']['detail']
    assert states['TECHNICAL']['state'] == 'WAITING'
    with Session(owner_engine) as db, db.begin():
        current = db.get(AgentRun, current_id);current.step = 'Summarizing the global dispatch cycle.'
        db.add(AgentLog(run_id=current_id, message=current.step))
    states = {row['agent']: row for row in view(client)['machine']['agents']}
    assert states['DISPATCH']['company_name'] is None  # A global step must not inherit the previous company's label.
    with Session(owner_engine) as db, db.begin():
        saved = db.get(Order, UUID(order['id']));saved.version += 1
        run = db.get(AgentRun, current_id);run.status = 'SUCCEEDED'
    states = {row['agent']: row for row in view(client)['machine']['agents']}
    assert states['DISPATCH']['state'] == 'WAITING'
    assert '0 have no eligible driver' in states['DISPATCH']['detail']


def test_metadata_allowlist_read_signals_and_window_are_bounded_without_private_data(client, setup):
    order = new_order(setup);at = now()
    with Session(owner_engine) as db, db.begin():
        row = db.get(Order, UUID(order['id']));row.source = 'EMAIL'
        run = AgentRun(agent='CHAT', status='SUCCEEDED', step='Run completed', heartbeat_at=at, finished_at=at)
        db.add(run);db.flush()
        db.add(AgentLog(run_id=run.id, organization_id=row.organization_id, message=WORKSPACE_RECORDS_READ))
        run_id = run.id
        for kind, age, dispatcher in [('message.created', 0, True), ('driver.location_updated', 0, True), ('watch.detected', 0, False), ('order.started', 25, True)]:
            db.add(Event(organization_id=row.organization_id, type=kind, entity='order', entity_id=row.id, order_id=row.id, actor_type='SYSTEM', correlation_id=uuid4(), dispatchers=dispatcher, created_at=at-timedelta(hours=age)))
    login(client);machine = view(client)['machine']
    created = next(row for row in machine['activity'] if row['kind'] == 'order.created')
    assert created['from_node'] == 'ORDER'
    read = next(row for row in machine['activity'] if row['title'] == WORKSPACE_RECORDS_READ)
    assert (read['from_node'], read['to_node'], read['reference']) == ('data', 'CHAT', None)
    assert not {'message.created', 'driver.location_updated', 'watch.detected', 'order.started'} & {row['kind'] for row in machine['activity']}
    with Session(owner_engine) as db, db.begin():
        for _ in range(45): db.add(AgentLog(run_id=run_id, message='Approved safe activity step.'))
    machine = view(client)['machine']
    assert len(machine['activity']) == 40 and machine['activity_truncated']
    assert len({row['id'] for row in machine['activity']}) == 40
