"""Operational monitoring acceptance using isolated, restricted-role PostgreSQL."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID,uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,text,func
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from app.main import app
from app.database import engine,context
from app.models import now
from app.operations.models import RouteStop,OrderStop,Order,Route,Issue
from app.order_watch import scan,commands,traffic,worker
from app.order_watch.models import Incident,Question,RouteEstimate
from app.order_watch.evidence import location_usable,separation
from conftest import login,post,HEADERS,owner_engine
from test_manual_operations import setup,new_order,assign,check,BASE

ROOT='/api/v1/platform/agents/operational'


def review(w,at=None):
    with Session(engine) as db,db.begin():
        context(db,platform=True)
        org=db.scalar(select(Order.organization_id).limit(1))
        context(db,org)
        return scan.scan_company(db,commands.system(org))


def owner_reports(w):
    login(w['client'])
    return check(w['client'].get(ROOT+'/issues'))['items']


def arrived(w):
    order=new_order(w,scheduled_at=now().isoformat());route=assign(w,order)
    mobile=w['mobile']
    route=check(post(mobile,BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    visit=route['stops'][0]
    route=check(post(mobile,BASE+f'/driver/routes/{route["id"]}/stops/{visit["id"]}/arrive',dict(version=route['version'],generation=route['generation'],captured_at=now().isoformat())))
    return order,route,visit


def test_reported_issues_immediately_enter_operational_monitor_without_technical_reports(setup):
    w=setup;order=new_order(w)
    data=dict(kind='VEHICLE',description='Truck liftgate does not work')
    key=str(uuid4())
    report=check(post(w['client'],BASE+f'/orders/{order["id"]}/issues',data,key),201)
    assert check(post(w['client'],BASE+f'/orders/{order["id"]}/issues',data,key),201)==report
    rows=owner_reports(w)
    assert len(rows)==1 and rows[0]['certainty']=='REPORTED' and rows[0]['description']==data['description']
    monitor=check(w['client'].get('/api/v1/platform/agents/monitor'))
    assert next(a for a in monitor['agents'] if a['agent']=='OPERATIONAL')['issue_count']==1
    assert monitor['issues']==[]
    inbox=check(w['client'].get('/api/v1/platform/notifications'))
    assert any(n['kind']=='issue.reported' and n['target']=='operational' for n in inbox['items'])


def test_waiting_requires_recorded_arrival_and_context_not_universal_twenty_minutes(setup,monkeypatch):
    w=setup;order,route,visit=arrived(w)
    with Session(owner_engine) as db,db.begin():
        stop=db.get(OrderStop,UUID(visit['stop_id']));stop.data={**stop.data,'service_minutes':90}
    monkeypatch.setattr(scan,'now',lambda:now()+timedelta(minutes=20))
    review(w)
    assert not any(r['kind']=='STOP_OVERRUN' for r in owner_reports(w))
    with Session(owner_engine) as db,db.begin():
        stop=db.get(OrderStop,UUID(visit['stop_id']));stop.data={**stop.data,'service_minutes':10}
    review(w);rows=owner_reports(w);row=next(r for r in rows if r['kind']=='STOP_OVERRUN')
    assert row['certainty']=='NEEDS_CONFIRMATION'
    assert 'Waiting versus handling' in row['description']
    questions=check(w['mobile'].get(BASE+'/driver/operational/questions'))
    assert len(questions)==1 and questions[0]['order_id']==order['id']
    review(w)
    assert len(check(w['mobile'].get(BASE+'/driver/operational/questions')))==1
    with Session(owner_engine) as db:
        assert db.scalar(select(func.count()).select_from(Question))==1
        assert db.scalar(text("SELECT count(*) FROM notifications WHERE kind='watch.question_created'"))==1


def test_driver_reply_is_versioned_idempotent_reported_evidence_and_not_resolution(setup,monkeypatch):
    w=setup;order,route,visit=arrived(w)
    monkeypatch.setattr(scan,'now',lambda:now()+timedelta(minutes=11));review(w)
    q=check(w['mobile'].get(BASE+'/driver/operational/questions'))[0]
    body=dict(version=q['version'],reason='ITEMS_NOT_READY',details='Warehouse says another half hour',estimated_ready_at=(now()+timedelta(minutes=30)).isoformat())
    key=str(uuid4());path=BASE+f'/driver/operational/questions/{q["id"]}/reply'
    saved=check(post(w['mobile'],path,body,key));assert saved==check(post(w['mobile'],path,body,key))
    assert post(w['mobile'],path,{**body,'details':'Different'},key).status_code==409
    row=next(r for r in owner_reports(w) if r['kind']=='STOP_OVERRUN')
    assert row['certainty']=='REPORTED' and row['status']=='OPEN' and row['driver_reply']==body['details']
    review(w)
    refreshed=next(r for r in owner_reports(w) if r['kind']=='STOP_OVERRUN')
    assert refreshed['certainty']=='REPORTED'
    assert refreshed['suggestions'][0]=='Confirm the reported readiness estimate with the sender.'


def test_driver_cannot_read_other_incidents_and_reassigned_questions_are_inaccessible(setup,monkeypatch):
    w=setup;order,route,visit=arrived(w)
    monkeypatch.setattr(scan,'now',lambda:now()+timedelta(minutes=11));review(w)
    q=check(w['mobile'].get(BASE+'/driver/operational/questions'))[0]
    assert w['mobile'].get(ROOT+'/issues').status_code==403
    assert w['mobile'].get(BASE+'/operational/issues').status_code==403
    with Session(engine) as db,db.begin():
        context(db,UUID(order['organization_id']) if 'organization_id' in order else UUID(w['client'].get('/api/v1/auth/me').json()['organization']['id']),driver_id=UUID(w['driver']['id']))
        assert db.scalar(select(func.count()).select_from(Incident))==0
    with Session(owner_engine) as db,db.begin():
        row=db.get(Route,UUID(route['id']));row.generation+=1
    assert check(w['mobile'].get(BASE+'/driver/operational/questions'))==[]
    assert post(w['mobile'],BASE+f'/driver/operational/questions/{q["id"]}/reply',dict(version=q['version'],reason='NO_PROBLEM')).status_code==404


def test_closed_stop_resolves_observation_but_not_an_unresolved_report(setup,monkeypatch):
    w=setup;order,route,visit=arrived(w)
    monkeypatch.setattr(scan,'now',lambda:now()+timedelta(minutes=11));review(w)
    check(post(w['mobile'],BASE+f'/driver/stops/{visit["stop_id"]}/issue',dict(kind='SHORT_LOAD',description='Only one of the two boxes is ready')),201)
    with Session(owner_engine) as db,db.begin():
        db.get(RouteStop,UUID(visit['id'])).status='COMPLETED'
    review(w);rows=owner_reports(w)
    assert not any(r['kind']=='STOP_OVERRUN' for r in rows)
    assert any(r['kind']=='REPORTED' and r['status']=='OPEN' for r in rows)
    login(w['client'],'dispatch','acme','dispatcher')
    history=check(w['client'].get(BASE+'/operational/issues',params={'status':'ALL'}))['items']
    assert next(r for r in history if r['kind']=='STOP_OVERRUN')['status']=='RESOLVED'


def test_missing_or_inaccurate_location_cannot_prove_near_arrival_or_traffic():
    current=now();duty=SimpleNamespace(started_at=current-timedelta(hours=1),ended_at=None)
    fresh=SimpleNamespace(captured_at=current,data=dict(latitude=49.26,longitude=-123.11,accuracy_m=20))
    assert location_usable(fresh,duty,current)
    assert separation(fresh,dict(latitude=49.26,longitude=-123.11))==0
    fresh.data['accuracy_m']=200;assert not location_usable(fresh,duty,current)
    fresh.data['accuracy_m']=20;fresh.captured_at=current-timedelta(minutes=3);assert not location_usable(fresh,duty,current)
    fresh.captured_at=current+timedelta(seconds=1);assert not location_usable(fresh,duty,current)
    assert not location_usable(None,duty,current)


def test_live_estimate_is_validated_and_includes_remaining_service_without_simulation(monkeypatch):
    current=now();snapshot=dict(origin=dict(latitude=49.26,longitude=-123.11),targets=[dict(stop_id='a',address=dict(latitude=49.27,longitude=-123.11),service_minutes=30),dict(stop_id='b',address=dict(latitude=49.28,longitude=-123.11),service_minutes=10)])
    monkeypatch.setattr(traffic.travel,'provider',lambda:'google')
    calls=[]
    def google(db,actor,method,payload,mask):
        calls.append(payload);return dict(routes=[dict(legs=[dict(duration='600s',staticDuration='300s',distanceMeters=1000),dict(duration='600s',staticDuration='600s',distanceMeters=2000)])])
    monkeypatch.setattr(traffic.travel,'google',google)
    result=traffic.obtain(None,snapshot,current)
    assert result['stops'][1]['eta']==(current+timedelta(minutes=50)).isoformat()
    assert result['stops'][1]['traffic_added_seconds']==300
    assert result['vehicle_access_verified'] is False and calls[0]['routingPreference']=='TRAFFIC_AWARE'
    monkeypatch.setattr(traffic.travel,'provider',lambda:'demo')
    assert traffic.obtain(None,snapshot,current) is None


def test_owner_review_request_coalesces_and_worker_records_real_operational_activity(setup):
    w=setup;new_order(w);login(w['client'])
    first=check(post(w['client'],ROOT+'/check',{}),202)
    second=check(post(w['client'],ROOT+'/check',{}),202)
    assert first['id']==second['id'] and first['status']=='QUEUED'
    assert worker.cycle(force=True)
    state=check(w['client'].get('/api/v1/platform/agents/monitor'))
    op=next(a for a in state['agents'] if a['agent']=='OPERATIONAL')
    assert op['run']['status']=='SUCCEEDED' and op['issue_count']>=1
    assert op['queued']==0
    assert all(r['kind']!='TECHNICAL' for r in owner_reports(w))


def test_acknowledgement_is_not_resolution_and_company_access_is_scoped(setup):
    w=setup;new_order(w);review(w);row=owner_reports(w)[0]
    key=str(uuid4());path=ROOT+f'/issues/{row["id"]}/acknowledge'
    body=dict(version=row['version'])
    saved=check(post(w['client'],path,body,key));assert saved==check(post(w['client'],path,body,key))
    assert saved['status']=='ACKNOWLEDGED' and saved['resolved_at'] is None
    assert post(w['client'],path,body).status_code==409
    with TestClient(app,headers=HEADERS) as anonymous:assert anonymous.get(ROOT+'/issues').status_code==401


def test_safety_reply_uses_existing_issue_workflow_without_duplicate_incident(setup,monkeypatch):
    w=setup;order,route,visit=arrived(w)
    monkeypatch.setattr(scan,'now',lambda:now()+timedelta(minutes=11));review(w)
    q=check(w['mobile'].get(BASE+'/driver/operational/questions'))[0]
    check(post(w['mobile'],BASE+f'/driver/operational/questions/{q["id"]}/reply',dict(version=q['version'],reason='UNSAFE',details='Unstable loading ramp')))
    rows=owner_reports(w)
    assert len(rows)==1 and rows[0]['severity']=='CRITICAL' and rows[0]['certainty']=='REPORTED'
    with Session(owner_engine) as db:
        assert db.scalar(select(func.count()).select_from(Issue))==1
        assert db.scalar(select(Incident.source_issue_id)) is not None
    with Session(owner_engine) as db,db.begin():db.get(RouteStop,UUID(visit['id'])).status='COMPLETED'
    review(w)
    assert len(owner_reports(w))==1  # Progress cannot erase an unresolved safety report.


def test_future_unassigned_order_is_not_late_based_on_arbitrary_lead_time(setup):
    w=setup;new_order(w,scheduled_at=(now()+timedelta(hours=8)).isoformat())
    review(w);assert owner_reports(w)==[]


def test_current_prediction_can_resolve_risk_but_stale_prediction_cannot(setup):
    w=setup;order=new_order(w,scheduled_at=(now()+timedelta(hours=1)).isoformat());route=assign(w,order)
    route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    visit=route['stops'][0];current=now()
    with Session(owner_engine) as db,db.begin():
        stop=db.get(OrderStop,UUID(visit['stop_id']));stop.data={**stop.data,'window_end':(current+timedelta(minutes=20)).isoformat()}
        db.add(RouteEstimate(organization_id=db.get(Order,UUID(order['id'])).organization_id,route_id=UUID(route['id']),generation=route['generation'],route_version=route['version'],next_stop_id=UUID(visit['stop_id']),captured_at=current,observed_at=current,data=dict(source='Validated test road provider',stops=[dict(stop_id=visit['stop_id'],eta=(current+timedelta(minutes=30)).isoformat(),traffic_added_seconds=300,road_distance_metres=2000)],straight_line_metres=1500,accuracy_metres=20)))
    review(w);rows=owner_reports(w);assert any(r['kind']=='TRAFFIC_RISK' and r['certainty']=='PREDICTED' for r in rows)
    with Session(owner_engine) as db,db.begin():
        estimate=db.scalar(select(RouteEstimate));estimate.captured_at=current-timedelta(minutes=10)
    review(w);row=next(r for r in owner_reports(w) if r['kind']=='TRAFFIC_RISK');assert row['certainty']=='NEEDS_CONFIRMATION'
    with Session(owner_engine) as db,db.begin():
        estimate=db.scalar(select(RouteEstimate));estimate.captured_at,estimate.observed_at=now(),now()
        estimate.data={**estimate.data,'stops':[{**estimate.data['stops'][0],'eta':(current+timedelta(minutes=10)).isoformat()}]}
    review(w);assert not any(r['kind']=='TRAFFIC_RISK' for r in owner_reports(w))


def test_early_arrival_waiting_before_booked_service_is_not_an_overrun(setup,monkeypatch):
    w=setup;order=new_order(w,scheduled_at=(now()+timedelta(hours=1)).isoformat());route=assign(w,order)
    route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    visit=route['stops'][0]
    check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/stops/{visit["id"]}/arrive',dict(version=route['version'],generation=route['generation'],captured_at=now().isoformat())))
    monkeypatch.setattr(scan,'now',lambda:now()+timedelta(minutes=20));review(w)
    assert not any(r['kind']=='STOP_OVERRUN' for r in owner_reports(w))


def test_reported_ready_time_drives_followup_without_repeated_unchanged_questions(setup,monkeypatch):
    w=setup;order,route,visit=arrived(w)
    monkeypatch.setattr(scan,'now',lambda:now()+timedelta(minutes=11));review(w)
    q=check(w['mobile'].get(BASE+'/driver/operational/questions'))[0]
    check(post(w['mobile'],BASE+f'/driver/operational/questions/{q["id"]}/reply',dict(version=q['version'],reason='ITEMS_NOT_READY',details='Waiting for packing',estimated_ready_at=(now()+timedelta(minutes=20)).isoformat())))
    monkeypatch.setattr(scan,'now',lambda:now()+timedelta(minutes=21));review(w)
    pending=check(w['mobile'].get(BASE+'/driver/operational/questions'));assert len(pending)==1
    assert pending[0]['version']==q['version']+2 and 'estimated readiness' in pending[0]['prompt']
    review(w);assert check(w['mobile'].get(BASE+'/driver/operational/questions'))[0]['version']==pending[0]['version']
    report=owner_reports(w)[0];assert any(e['label']=='Driver note' and e['value']=='Waiting for packing' for e in report['evidence'])
    assert any(e['label']=='Reported ready time passed without stop completion' for e in report['evidence'])
