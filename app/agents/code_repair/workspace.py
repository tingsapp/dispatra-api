"""Copy source, never credentials; compare the full snapshot before touching local files."""
import hashlib
import os
from pathlib import Path
from .policy import UnsafeRepair

TOP = {'api': {'alembic.ini', 'requirements.txt', 'requirements-dev.txt', 'requirements.lock'},
    'client': {'package.json', 'package-lock.json', 'index.html', 'tsconfig.json', 'vite.config.ts', 'postcss.config.js'}}
DIRS = {'api': ('app', 'tests', 'migrations', 'scripts'), 'client': ('src', 'tests', 'public')}
SKIP = {'__pycache__', '.pytest_cache', 'node_modules', '.git', '.local'}


def files(root, project):
    if root.is_symlink(): raise UnsafeRepair('A source checkout cannot be a symlink.')
    paths = [root / name for name in TOP[project] if (root / name).is_file()]
    for folder in DIRS[project]:
        if (root / folder).is_symlink(): raise UnsafeRepair('Symlinks in source snapshots require review.')
        paths.extend((root / folder).rglob('*'))
    result = {}
    for path in paths:
        relative = path.relative_to(root)
        if any(part in SKIP or part.startswith('.') for part in relative.parts): continue
        # rglob does not traverse directory symlinks; reject the link itself before reading any file.
        if path.is_symlink():
            raise UnsafeRepair('Symlinks in source snapshots require review.')
        if not path.is_file(): continue
        if path.suffix in ('.pem', '.key') or (path.suffix == '.json' and any(word in path.name.lower()
                for word in ('firebase-adminsdk', 'service-account', 'credentials'))): continue
        if path.stat().st_size > 12_000_000: raise UnsafeRepair('Source snapshot contains an oversized file.')
        result[relative.as_posix()] = path.read_bytes()
    if not result: raise UnsafeRepair('Source snapshot is empty.')
    return result


def digest(source):
    h = hashlib.sha256()
    for path, data in sorted(source.items()):
        h.update(path.encode() + b'\0' + hashlib.sha256(data).digest())
    return h.hexdigest()


def materialize(source, target):
    target.mkdir(parents=True, exist_ok=True)
    for name, data in source.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def atomic_write(path, data):
    temporary = path.with_name(path.name + '.technical-repair-tmp')
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    with temporary.open('xb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)
