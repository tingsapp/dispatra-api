"""Independent durable progress transactions. Messages are developer-defined, never provider text."""
from contextlib import contextmanager
from contextvars import ContextVar
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import now
from .models import AgentLog, AgentRun

CURRENT = ContextVar('agent_progress', default=None)


class Progress:
    def __init__(self, agent, identity=None):
        self.errors = 0
        with Session(engine) as db, db.begin():
            context(db, platform=True)
            row = db.get(AgentRun, identity) if identity else AgentRun(agent=agent, step='Starting agent run')
            row.status, row.heartbeat_at = 'RUNNING', now()
            if not identity: db.add(row)
            db.flush()
            self.id = row.id

    def step(self, message, progress=None, organization_id=None, level='INFO'):
        if level == 'ERROR': self.errors += 1
        with Session(engine) as db, db.begin():
            context(db, platform=True)
            row = db.get(AgentRun, self.id)
            row.step, row.heartbeat_at = message, now()
            if progress is not None: row.progress = max(0, min(100, progress))
            db.add(AgentLog(run_id=self.id, organization_id=organization_id, message=message, level=level))

    def finish(self):
        with Session(engine) as db, db.begin():
            context(db, platform=True)
            row = db.get(AgentRun, self.id)
            row.status = 'FAILED' if self.errors else 'SUCCEEDED'
            row.step = 'Run completed with errors' if self.errors else 'Run completed'
            row.progress, row.heartbeat_at, row.finished_at = 100, now(), now()
            db.add(AgentLog(run_id=self.id, message=row.step, level='ERROR' if self.errors else 'INFO'))


@contextmanager
def monitor_run(agent, identity=None):
    progress = Progress(agent, identity)
    token = CURRENT.set(progress)
    try:
        yield progress
    except Exception:
        progress.step('Agent run failed; investigation required.', level='ERROR')
        raise
    finally:
        CURRENT.reset(token)
        progress.finish()


def step(message, organization_id=None, level='INFO'):
    current = CURRENT.get()
    if current: current.step(message, organization_id=organization_id, level=level)
