"""Ready times are lower bounds; assigned work remains distinct from execution."""
from datetime import timedelta
from types import SimpleNamespace as NS
from uuid import UUID
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import engine, context
from app.models import now
from app.operations.attention import flags
from app.operations.models import Order, OrderStop, Route
from app.order_watch import commands, scan
from app.order_watch.detector import evaluate, signal
from app.order_watch.evidence import deadline
from app.order_watch.models import Incident, Question
from conftest import owner_engine
from test_manual_operations import setup, new_order, assign


def scenario():
    current=now()
    order=NS(id='o',version=1,route_id=None,status='NEW',scheduled_at=current-timedelta(minutes=6),pricing={'status':'PRICED'},facts={'stops':[
        {'id':'p','kind':'PICKUP','window_start':(current-timedelta(minutes=6)).isoformat()},
        {'id':'d','kind':'DROPOFF','window_end':(current+timedelta(hours=2)).isoformat()}]})
    stops={s['id']:NS(id=s['id'],order_id=order.id,kind=s['kind'],data=s) for s in order.facts['stops']}
    return current,order,stops


def test_ready_time_does_not_create_late_start_unassigned_or_arrival_warning():
    current,order,stops=scenario()
    assert deadline(order,stops['p']) is None
    assert flags(order,{},current)==[]
    assert evaluate(order,None,[],stops,None,current)==[]
    order.route_id='r';order.status='ASSIGNED'
    route=NS(id='r',generation=1,status='PLANNED',planned_at=current+timedelta(minutes=1))
    visits=[NS(id='v'+s.id,stop_id=s.id,status='PENDING',arrived_at=None,planned_at=current+timedelta(minutes=10)) for s in stops.values()]
    assert flags(order,{v.stop_id:v for v in visits},current)==[]
    assert evaluate(order,route,visits,stops,None,current)==[]
    # Published route start is still monitored independently of ready time.
    route.planned_at=current-timedelta(minutes=1)
    assert [s['kind'] for s in evaluate(order,route,visits,stops,None,current)]==['START_OVERDUE']


def test_explicit_pickup_deadline_and_delivery_deadline_still_warn():
    current,order,stops=scenario()
    stops['p'].data['window_end']=(current-timedelta(minutes=1)).isoformat()
    assert flags(order,{},current)[0][0]=='LATE_START'
    assert evaluate(order,None,[],stops,None,current)[0]['kind']=='UNASSIGNED'
    order.route_id='r';order.status='ASSIGNED'
    route=NS(id='r',generation=1,status='PLANNED',planned_at=current+timedelta(minutes=1))
    visit=NS(id='v',stop_id='p',status='PENDING',arrived_at=None)
    assert evaluate(order,route,[visit],stops,None,current)[0]['kind']=='ARRIVAL_OVERDUE'
    visit.arrived_at=current
    assert flags(order,{'p':visit},current)==[]
    assert evaluate(order,route,[visit],stops,None,current)==[]
    stops['p'].data.pop('window_end');stops['d'].data['window_end']=(current-timedelta(minutes=1)).isoformat()
    order.route_id=None;order.status='NEW'
    assert flags(order,{},current)==[('AT_RISK','A delivery deadline is missed or the planned arrival is late.')]
    assert evaluate(order,None,[],stops,None,current)[0]['kind']=='UNASSIGNED'


def test_no_candidate_is_actionable_without_an_invented_deadline():
    current,order,stops=scenario()
    stops['d'].data.pop('window_end')
    decision=NS(excluded=[],created_at=current)
    result=evaluate(order,None,[],stops,None,current,decision)
    assert result[0]['kind']=='UNASSIGNED' and result[0]['severity']=='WARNING'
    assert result[0]['response_due_at'] is None


@pytest.mark.parametrize('kind',['ARRIVAL_OVERDUE','ARRIVAL_RISK','TRAFFIC_RISK','UNASSIGNED'])
def test_old_ready_time_incidents_are_superseded_and_questions_closed(setup,kind):
    w=setup;order=new_order(w,scheduled_at=(now()-timedelta(minutes=6)).isoformat())
    route=assign(w,order) if kind!='UNASSIGNED' else None
    with Session(owner_engine) as db:
        org=db.get(Order,UUID(order['id'])).organization_id
    with Session(engine) as db,db.begin():
        context(db,org);actor=commands.system(org);saved=db.get(Order,UUID(order['id']))
        pickup=db.scalar(select(OrderStop).where(OrderStop.order_id==saved.id,OrderStop.kind=='PICKUP'))
        obs=signal('legacy-ready-time',kind,'Old timing warning','Ready time incorrectly used as deadline',[],pickup.id if route else None,certainty='NEEDS_CONFIRMATION')
        row=commands.apply(db,actor,saved,obs);incident_id=row.id
        if route:commands.ask(db,actor,row,db.get(Route,UUID(route['id'])),'Confirm progress')
        scan.scan_company(db,actor)
        assert row.status=='SUPERSEDED' and 'ready time is not a deadline' in row.resolution
        if route:assert db.scalar(select(Question).where(Question.incident_id==incident_id)).status=='CLOSED'
        scan.scan_company(db,actor)
        assert row.status=='SUPERSEDED'
        assert db.get(Order,saved.id).status==('ASSIGNED' if route else 'NEW')


def test_reported_evidence_is_not_erased_by_deadline_cleanup(setup):
    w=setup;order=new_order(w);route=assign(w,order)
    with Session(owner_engine) as db:org=db.get(Order,UUID(order['id'])).organization_id
    with Session(engine) as db,db.begin():
        context(db,org);actor=commands.system(org);saved=db.get(Order,UUID(order['id']))
        pickup=db.scalar(select(OrderStop).where(OrderStop.order_id==saved.id,OrderStop.kind=='PICKUP'))
        row=commands.apply(db,actor,saved,signal('reported-ready-time','ARRIVAL_OVERDUE','Driver update','Driver reports a delay',[],pickup.id,certainty='REPORTED'))
        scan.scan_company(db,actor)
        assert row.status=='OPEN' and row.certainty=='REPORTED'
