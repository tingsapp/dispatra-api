"""Technical agent: scheduled platform checks, safe maintenance and approved repairs."""
import argparse
import time
from datetime import timedelta
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import now
from . import checks, repairs
from .models import AgentRun, TechnicalIssue
from .telemetry import monitor_run


def cycle(force=False):
    # One Technical agent at a time, even across API/worker processes. Always release the session lock.
    with engine.connect() as lock:
        checks.require_runtime_role(lock)
        acquired = lock.scalar(text("SELECT pg_try_advisory_lock(hashtextextended('technical-agent-cycle', 0))"))
        lock.commit()
        if not acquired: return False
        try:
            with Session(engine) as db, db.begin():
                context(db, platform=True)
                queued = db.scalar(select(AgentRun).where(AgentRun.agent == 'TECHNICAL', AgentRun.status == 'QUEUED')
                                   .order_by(AgentRun.created_at).with_for_update(skip_locked=True).limit(1))
                latest = db.scalar(select(AgentRun).where(AgentRun.agent == 'TECHNICAL').order_by(AgentRun.created_at.desc()).limit(1))
                approved = db.scalar(select(TechnicalIssue.id).where(TechnicalIssue.status == 'APPROVED').limit(1))
                if queued is None and not force and not approved and latest and latest.heartbeat_at > now() - timedelta(seconds=60): return False
                identity = queued.id if queued else None
                if queued: queued.status, queued.heartbeat_at = 'RUNNING', now()
                # An interrupted approved repair is reported, never blindly replayed.
                for issue in db.scalars(select(TechnicalIssue).where(TechnicalIssue.status == 'APPLYING', TechnicalIssue.updated_at < now() - timedelta(minutes=5)).with_for_update()):
                    issue.status, issue.description, issue.updated_at = 'FAILED', 'Repair execution was interrupted. Verify its outcome before retrying.', now()
                    issue.version += 1
            with monitor_run('TECHNICAL', identity) as progress:
                progress.step('Checking platform health and safe maintenance.', 5)
                repairs.execute(progress)
                with Session(engine) as db, db.begin():
                    context(db, platform=True)
                    checks.scan(db, progress)
            return True
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(hashtextextended('technical-agent-cycle', 0))"))
            lock.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    while True:
        try: cycle(force=args.once)
        except Exception as error:
            print(f'Technical agent error: {type(error).__name__}', flush=True)
            if args.once: raise SystemExit(1)
        if args.once: return
        time.sleep(5)


if __name__ == '__main__': main()
