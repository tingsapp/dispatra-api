"""Order monitoring worker. Provider calls occur outside business transactions; commands revalidate state."""
import argparse
import time
from datetime import timedelta
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from app.database import context,engine
from app.models import Organization,now
from app.agents.models import AgentRun
from app.agents.checks import require_runtime_role
from app.agents.telemetry import monitor_run
from app.operations.models import Route,RouteStop,DutySession
from app.operations.common import company_lock
from . import traffic
from .commands import system
from .models import RouteEstimate
from .scan import scan_company


def acquire_estimates(organization_id,progress):
    actor=system(organization_id);current=now()
    with Session(engine) as db,db.begin():
        context(db,organization_id)
        snapshots=[traffic.route_input(db,actor,r,current) for r in db.scalars(select(Route).where(Route.organization_id==organization_id,Route.status=='IN_PROGRESS'))]
    for snapshot in filter(None,snapshots):
        requested_at=now()
        try: data=traffic.obtain(actor,snapshot,requested_at)
        except HTTPException:
            progress.step('A live road estimate is unavailable; no traffic cause inferred.',organization_id=organization_id)
            continue
        if data is None:continue
        with Session(engine) as db,db.begin():
            context(db,organization_id);company_lock(db,actor)
            route=db.get(Route,UUID(snapshot['route_id']))
            duty=db.get(DutySession,UUID(snapshot['duty_id']))
            if not duty or duty.ended_at is not None or (now()-snapshot['captured_at']).total_seconds()>120:continue
            if not route or route.status!='IN_PROGRESS' or route.version!=snapshot['route_version'] or route.generation!=snapshot['generation']:continue
            visit=db.scalar(select(RouteStop).where(RouteStop.organization_id==organization_id,RouteStop.route_id==route.id,
                RouteStop.status!='COMPLETED').order_by(RouteStop.position).limit(1))
            if not visit or visit.arrived_at is not None or str(visit.stop_id)!=snapshot['next_stop_id']:continue
            row=db.scalar(select(RouteEstimate).where(RouteEstimate.organization_id==organization_id,RouteEstimate.route_id==route.id))
            if row is None:
                row=RouteEstimate(organization_id=organization_id,route_id=route.id);db.add(row)
            row.generation,row.route_version=route.generation,route.version
            row.next_stop_id,row.captured_at=visit.stop_id,snapshot['captured_at']
            row.observed_at,row.data=requested_at,data


def cycle(force=False):
    with engine.connect() as lock:
        require_runtime_role(lock)
        acquired=lock.scalar(text("SELECT pg_try_advisory_lock(hashtextextended('order-watch-cycle',0))"));lock.commit()
        if not acquired:return False
        try:
            with Session(engine) as db,db.begin():
                context(db,platform=True)
                queued=db.scalar(select(AgentRun).where(AgentRun.agent=='OPERATIONAL',AgentRun.status=='QUEUED').order_by(AgentRun.created_at).with_for_update(skip_locked=True).limit(1))
                latest=db.scalar(select(AgentRun).where(AgentRun.agent=='OPERATIONAL').order_by(AgentRun.created_at.desc()).limit(1))
                if not queued and not force and latest and latest.heartbeat_at>now()-timedelta(seconds=60):return False
                identity=queued.id if queued else None
                if queued:queued.status='RUNNING';queued.heartbeat_at=now()
                organizations=list(db.scalars(select(Organization.id).where(Organization.active.is_(True))))
            with monitor_run('OPERATIONAL',identity) as progress:
                progress.step('Reviewing recorded order progress and operational reports.',5)
                for index,organization_id in enumerate(organizations):
                    acquire_estimates(organization_id,progress)
                    with Session(engine) as db,db.begin():
                        context(db,organization_id)
                        org=db.get(Organization,organization_id)
                        if not org or not org.active:continue
                        count=scan_company(db,system(organization_id))
                    progress.step(f'Reviewed {count} active orders using recorded evidence.',
                        10+int(85*(index+1)/max(1,len(organizations))),organization_id)
            return True
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(hashtextextended('order-watch-cycle',0))"));lock.commit()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    while True:
        try:cycle(force=args.once)
        except Exception as error:
            print(f'Operational agent error: {type(error).__name__}',flush=True)
            if args.once:raise SystemExit(1)
        if args.once:return
        time.sleep(5)


if __name__=='__main__':main()
