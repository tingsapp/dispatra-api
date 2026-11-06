"""Real restricted-role queue/privacy/notification checks; no live edits or model calls."""
from datetime import timedelta
from unittest.mock import Mock
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.agents import diagnostics, technical_worker
from app.agents.code_repair import worker, pipeline, queue
from app.agents.code_repair.policy import UnsafeRepair
from app.agents.models import TechnicalIssue, RuntimeDiagnostic
from app.models import now
from conftest import owner_engine, login


def report():
    with Session(owner_engine) as db:
        return db.scalar(select(TechnicalIssue).where(TechnicalIssue.kind == 'RUNTIME_ERROR'))


def prepare(monkeypatch, source='WEB'):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    monkeypatch.setenv('TECHNICAL_LOCAL_CODE_REPAIR', '1')
    diagnostics.record(source, 'RUNTIME_ERROR' if source == 'WEB' else 'HTTP_5XX')
    technical_worker.cycle(force=True)


def fake_runner(monkeypatch, tmp_path, result=None):
    roots = {'api': tmp_path / 'api', 'client': tmp_path / 'client'}
    for root in roots.values(): root.mkdir()
    monkeypatch.setattr(queue, 'roots', lambda: roots)
    monkeypatch.setattr(worker, 'Planner', Mock())
    monkeypatch.setattr(pipeline, 'run', Mock(side_effect=result))


def test_code_repair_is_quiet_while_queued_and_exposes_progress(client, monkeypatch):
    prepare(monkeypatch); row = report()
    assert row.status == 'APPLYING' and row.repair_stage == 'QUEUED' and row.notified_at is None
    login(client)
    snapshot = client.get('/api/v1/platform/agents/monitor').json()
    assert snapshot['platform_attention'] == 0
    assert snapshot['issues'][0]['repair_stage'] == 'QUEUED'


def test_verified_pipeline_success_stays_quiet(client, monkeypatch, tmp_path):
    prepare(monkeypatch, 'API'); fake_runner(monkeypatch, tmp_path)
    assert worker.cycle()
    row = report()
    assert row.status == 'FIXED' and row.repair_stage == 'COMPLETE' and row.fixed_count == 1 and row.notified_at is None


def test_failed_or_risky_pipeline_escalates_once(client, monkeypatch, tmp_path):
    prepare(monkeypatch); fake_runner(monkeypatch, tmp_path, UnsafeRepair('The fix touches protected application behavior.'))
    assert worker.cycle()
    row = report(); seen = row.notified_at
    assert row.status == 'FAILED' and row.repair_stage == 'ESCALATED' and seen
    technical_worker.cycle(force=True)
    assert report().notified_at == seen
    login(client)
    assert client.get('/api/v1/platform/agents/monitor').json()['platform_attention'] == 1


def test_stalled_queue_escalates_instead_of_hiding_failure(client, monkeypatch):
    prepare(monkeypatch)
    with Session(owner_engine) as db, db.begin():
        db.get(TechnicalIssue, report().id).updated_at = now() - timedelta(minutes=46)
    technical_worker.cycle(force=True)
    assert report().status == 'FAILED' and report().notified_at


def test_fault_locations_keep_distinct_faults_separate(client):
    diagnostics.record('API', 'HTTP_5XX', code_path='app/formatting.py', code_line=10, error_type='TypeError')
    diagnostics.record('API', 'HTTP_5XX', code_path='app/formatting.py', code_line=11, error_type='TypeError')
    diagnostics.record('API', 'HTTP_5XX', code_path='app/formatting.py', code_line=10, error_type='TypeError')
    with Session(owner_engine) as db:
        assert sorted(db.scalars(select(RuntimeDiagnostic.occurrences))) == [1, 2]


def test_browser_rejects_runtime_data_and_accepts_only_source_locations(client):
    login(client)
    path = '/api/v1/diagnostics/runtime'
    assert client.post(path, json={'source':'WEB', 'kind':'RUNTIME_ERROR', 'code_path':'/home/user/secrets'}).status_code == 422
    assert client.post(path, json={'source':'WEB', 'kind':'RUNTIME_ERROR', 'code_path':'src/Foo.tsx?token=private'}).status_code == 422
    assert client.post(path, json={'source':'WEB', 'kind':'RUNTIME_ERROR', 'error_type':'private data'}).status_code == 422
    assert client.post(path, json={'source':'WEB', 'kind':'RUNTIME_ERROR', 'code_path':'src/Foo.tsx', 'code_line':12, 'error_type':'TypeError'}).status_code == 202


def test_recurrence_after_verified_fix_creates_a_fresh_repair(client, monkeypatch):
    prepare(monkeypatch)
    with Session(owner_engine) as db, db.begin():
        row = db.get(TechnicalIssue, report().id)
        row.status, row.repair_stage, row.updated_at = 'FIXED', 'COMPLETE', now() - timedelta(seconds=10)
    diagnostics.record('WEB', 'RUNTIME_ERROR')
    technical_worker.cycle(force=True)
    with Session(owner_engine) as db:
        rows = db.scalars(select(TechnicalIssue).where(TechnicalIssue.kind == 'RUNTIME_ERROR')).all()
        assert len(rows) == 2 and sorted(row.status for row in rows) == ['APPLYING', 'FIXED']


def test_active_code_tests_do_not_suppress_health_scanning(client, monkeypatch):
    from app.agents.telemetry import monitor_run
    monkeypatch.setenv('OPENAI_API_KEY', '')
    with monitor_run('TECHNICAL', purpose='CODE_REPAIR') as progress:
        progress.step('Testing an isolated code fix.')
        assert technical_worker.cycle(force=False)


def test_code_repair_queue_keeps_its_own_timeout(client, monkeypatch):
    prepare(monkeypatch)
    with Session(owner_engine) as db, db.begin():
        db.get(TechnicalIssue, report().id).updated_at = now() - timedelta(minutes=6)
    technical_worker.cycle(force=True)
    row = report()
    assert row.status == 'APPLYING' and row.repair_stage == 'QUEUED' and row.notified_at is None
