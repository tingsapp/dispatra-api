"""Error categories and optional source-code locations; never runtime values or request data."""
from pathlib import Path
import re
from uuid import uuid4
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import now
from .models import RuntimeDiagnostic


def record(source, kind, organization_id=None, *, code_path=None, code_line=None, error_type=None):
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        seen = now()
        statement = insert(RuntimeDiagnostic).values(id=uuid4(), organization_id=organization_id, source=source, kind=kind,
            bucket=seen.replace(minute=0, second=0, microsecond=0), occurrences=1, last_seen_at=seen,
            code_path=code_path, code_line=code_line, error_type=error_type)
        db.execute(statement.on_conflict_do_update(constraint='runtime_diagnostic_window',
            set_={'occurrences': RuntimeDiagnostic.occurrences + 1, 'last_seen_at': seen,
                'code_path': code_path, 'code_line': code_line, 'error_type': error_type}))


def api_failure(organization_id=None, error=None):
    location = {}
    if error is not None:
        root = Path(__file__).resolve().parents[2]
        frame = error.__traceback__
        while frame:
            try:
                path = Path(frame.tb_frame.f_code.co_filename).resolve().relative_to(root).as_posix()
                if re.fullmatch(r'app/[A-Za-z0-9_/-]+\.py', path):
                    location = {'code_path': path, 'code_line': frame.tb_lineno}
            except ValueError: pass
            frame = frame.tb_next
        name = type(error).__name__
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', name): location['error_type'] = name
    try: record('API', 'HTTP_5XX', organization_id, **location)
    except Exception:
        # A database outage must still return the original HTTP error.
        pass
