"""Deterministic repair boundaries; a model cannot grant itself additional permissions."""
import ast
import difflib
import re
from pathlib import PurePosixPath
from pydantic import BaseModel, Field


class UnsafeRepair(ValueError): pass


class Edit(BaseModel):
    path: str = Field(max_length=240)
    content: str = Field(max_length=60000)


class Proposal(BaseModel):
    explanation: str = Field(max_length=500)
    risk: str = Field(pattern='^(LOW|REVIEW)$')
    edits: list[Edit] = Field(min_length=1, max_length=4)
    regression: str = Field(min_length=1, max_length=20000)


class Review(BaseModel):
    safe: bool
    reproduces_reported_fault: bool
    reason: str = Field(max_length=500)


PROTECTED_API = ('app/agents/', 'app/auth', 'app/security', 'app/database', 'app/main', 'app/models',
    'app/routes', 'app/schemas', 'app/services', 'app/bootstrap', 'app/provision', 'app/registration',
    'app/verification', 'app/subscription', 'app/maintenance', 'app/demo', 'app/operations/',
    'app/platform/', 'app/company_billing/', 'app/events/', 'app/push/', 'app/dispatch/', 'app/integrations/')
PROTECTED_WEB = ('src/portal/api', 'src/portal/schema', 'src/portal/runtimeDiagnostics', 'src/portal/readRecovery',
    'src/portal/admin/', 'src/lib/pricing', 'src/lib/assignment', 'src/lib/server', 'src/lib/companyTax',
    'src/lib/fuel', 'src/lib/tax', 'src/lib/quotation', 'src/lib/scopedStorage')
DANGEROUS = re.compile(r'\b(eval|exec|subprocess|__import__|open|unlink|remove|rmtree|write_text|write_bytes|'
    r'os\.system|fetch|XMLHttpRequest|sendBeacon|localStorage|sessionStorage|dangerouslySetInnerHTML)\b')


def source_path(project, path):
    pure = PurePosixPath(path)
    if path != pure.as_posix() or pure.is_absolute() or '..' in pure.parts or not re.fullmatch(r'[A-Za-z0-9_./-]+', path):
        return False
    if project == 'api':
        return path.startswith('app/') and path.endswith('.py')
    return path.startswith('src/') and pure.suffix in ('.ts', '.tsx', '.css')


def validate(project, proposal, originals):
    if proposal.risk != 'LOW': raise UnsafeRepair('The proposed fix needs human review.')
    if len({e.path for e in proposal.edits}) != len(proposal.edits): raise UnsafeRepair('Duplicate edits.')
    changed = 0
    for edit in proposal.edits:
        path = edit.path
        if not source_path(project, path) or path not in originals: raise UnsafeRepair('Only existing source files may be repaired.')
        if path.startswith(PROTECTED_API if project == 'api' else PROTECTED_WEB):
            raise UnsafeRepair('The fix touches protected application behavior.')
        if project == 'api' and (PurePosixPath(path).stem in ('models', 'schemas', 'routes', 'service', 'worker', 'vault', 'booking', 'transport')
                or any(word in path.lower() for word in ('credential', 'password', 'payment', 'auth'))):
            raise UnsafeRepair('The fix touches security, persistence or business commands.')
        before = originals[path].decode()
        if edit.content == before: raise UnsafeRepair('No source change proposed.')
        delta = [line for line in difflib.ndiff(before.splitlines(), edit.content.splitlines()) if line[:2] in ('+ ', '- ')]
        changed += len(delta)
        if any(DANGEROUS.search(line[2:]) for line in delta): raise UnsafeRepair('The change adds or alters sensitive side effects.')
        if project == 'api':
            old, new = ast.parse(before), ast.parse(edit.content)
            imports = lambda tree: [ast.dump(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
            if imports(old) != imports(new): raise UnsafeRepair('Import changes require review.')
        elif any(re.search(r'\b(import|require)\b', line[2:]) for line in delta):
            raise UnsafeRepair('Import changes require review.')
    if changed > 240: raise UnsafeRepair('The repair is too large for automatic application.')
    # Test execution and independent review provide the semantic checks; tests cannot alter existing tests.
    if project == 'api': ast.parse(proposal.regression)
    return proposal
