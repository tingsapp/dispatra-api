"""Dedicated local repair worker. Source edits never run in the API/health-check process."""
import argparse
from datetime import timedelta
import os
import time
from sqlalchemy import select, text, func
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import now
from app.services import audit
from .. import checks, escalation
from ..brain import Brain
from ..models import TechnicalIssue, RuntimeDiagnostic
from ..telemetry import monitor_run
from . import queue, pipeline
from .install import LocalInstaller
from .planner import Planner
from .policy import UnsafeRepair
from .sandbox import Sandbox


def installer_for(*args, **kwargs):
    if os.environ.get('TECHNICAL_PRODUCTION_CODE_REPAIR') == '1':
        from .production import ProductionInstaller
        return ProductionInstaller(*args, **kwargs)
    return LocalInstaller(*args, **kwargs)


def update(identity, stage, message, *, status=None):
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        row = db.scalar(select(TechnicalIssue).where(TechnicalIssue.id == identity).with_for_update())
        if row is None or row.status != 'APPLYING': raise UnsafeRepair('The repair report changed; execution stopped.')
        row.repair_stage, row.repair_summary, row.updated_at = stage, message[:500], now()
        if status:
            row.status = status; row.fixed_count = int(status == 'FIXED'); row.version += 1
            if status == 'FAILED': row.notified_at = None
            audit(db, checks.SYSTEM, 'platform_owner.code_repair_finished', row.id, row.organization_id, 'AGENT')


def execute(identity, project, fault, progress):
    roots = queue.roots()
    folder = roots['api'] / '.local/code-repairs' / str(identity)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    tick = [0.0]
    current = ['ANALYZING', 'Preparing isolated source diagnosis.']
    def heartbeat():
        if time.monotonic() - tick[0] < 15: return
        update(identity, *current)
        progress.step(current[1])
        tick[0] = time.monotonic()
    def stage(name, message):
        if name == 'APPLYING': verification_started[0] = now()
        current[:] = [name, message]; tick[0] = 0; heartbeat()
    verification_started = [None]
    def runtime_ok():
        if verification_started[0] is None: verification_started[0] = now()
        with Session(engine) as db, db.begin():
            context(db, platform=True)
            return not db.scalar(select(RuntimeDiagnostic.id).where(RuntimeDiagnostic.source == fault['source'],
                RuntimeDiagnostic.kind != 'READ_RECOVERED', RuntimeDiagnostic.last_seen_at > verification_started[0]).limit(1))
    installer = installer_for(roots[project], project, folder, heartbeat, runtime_ok)
    try:
        installer.recover()
        if os.environ.get('TECHNICAL_PRODUCTION_CODE_REPAIR') == '1':
            import shutil
            if shutil.disk_usage(roots['api']).free < 3 * 1024**3:
                raise UnsafeRepair('Insufficient free disk space for isolated testing and a rollback copy.')
        planner = Planner()
        pipeline.run(project, roots, folder, fault, planner, Sandbox(roots, heartbeat), installer, stage)
        update(identity, 'COMPLETE', 'Regression failed before and passed after the fix. Existing tests and build checks passed; application health verified after release.', status='FIXED')
    except Exception as error:
        # Persist a bounded, non-sensitive explanation. Provider output and test logs stay local.
        message = str(error) if isinstance(error, UnsafeRepair) else 'The code-repair worker could not complete safely. Inspect its saved test evidence.'
        update(identity, 'ESCALATED', message, status='FAILED')
        progress.step(message[:500], level='ERROR')
    escalation.escalate(progress, Brain())


def cycle():
    if not queue.enabled(): return False
    with engine.connect() as lock:
        checks.require_runtime_role(lock)
        acquired = lock.scalar(text("SELECT pg_try_advisory_lock(hashtextextended('technical-local-code-repair', 0))")); lock.commit()
        if not acquired: return False
        try:
            with Session(engine) as db, db.begin():
                context(db, platform=True)
                interrupted = db.scalars(select(TechnicalIssue).where(TechnicalIssue.status == 'APPLYING',
                    TechnicalIssue.repair_stage.is_not(None), TechnicalIssue.repair_stage != 'QUEUED')).all()
                for previous in interrupted:
                    evidence = queue.diagnostic(db, previous)
                    if evidence and evidence.source in ('API', 'WEB'):
                        project = 'api' if evidence.source == 'API' else 'client'
                        local_roots = queue.roots()
                        folder = local_roots['api'] / '.local/code-repairs' / str(previous.id)
                        try: installer_for(local_roots[project], project, folder).recover()
                        except Exception: pass  # Preserve journal and escalate; never replay an uncertain installation.
                    previous.status, previous.repair_stage = 'FAILED', 'ESCALATED'
                    previous.repair_summary = 'Code repair was interrupted. Any incomplete installation was checked for rollback; inspect the local journal before retrying.'
                    previous.notified_at = None; previous.version += 1; previous.updated_at = now()
                row = db.scalar(select(TechnicalIssue).where(TechnicalIssue.status == 'APPLYING',
                    TechnicalIssue.repair_stage == 'QUEUED').order_by(TechnicalIssue.created_at).with_for_update(skip_locked=True).limit(1))
                if row is None: return False
                evidence = queue.diagnostic(db, row)
                if evidence is None or evidence.source not in ('API', 'WEB'):
                    row.status, row.repair_stage, row.repair_summary = 'FAILED', 'ESCALATED', 'The fault evidence is no longer available.'
                    return True
                recent = db.scalar(select(func.count()).select_from(TechnicalIssue).where(
                    TechnicalIssue.repair_stage.not_in(['QUEUED']), TechnicalIssue.repair_stage.is_not(None),
                    TechnicalIssue.updated_at > now() - timedelta(hours=1)))
                if recent >= 3:
                    row.status, row.repair_stage, row.repair_summary = 'FAILED', 'ESCALATED', 'Automatic code-repair budget exhausted: three incidents per hour. Admin review is required.'
                    return True
                identity, project = row.id, 'api' if evidence.source == 'API' else 'client'
                fault = {key: getattr(evidence, key) for key in ('source', 'kind', 'code_path', 'code_line', 'error_type')}
                row.repair_stage, row.updated_at = 'ANALYZING', now()
            with monitor_run('TECHNICAL', purpose='CODE_REPAIR') as progress: execute(identity, project, fault, progress)
            return True
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(hashtextextended('technical-local-code-repair', 0))")); lock.commit()


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--once', action='store_true'); args = parser.parse_args()
    while True:
        try: cycle()
        except Exception as error:
            print(f'Code repair: {type(error).__name__}', flush=True)
            if args.once: raise SystemExit(1)
        if args.once: return
        time.sleep(5)


if __name__ == '__main__': main()
