"""Production source install and atomic static release, using a fixed privileged helper."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from contextlib import contextmanager
from uuid import UUID
from .install import LocalInstaller
from .policy import UnsafeRepair

SERVICES = ('dispatra-api.service', 'dispatra-intake.service', 'dispatra-dispatch.service',
    'dispatra-operational.service', 'dispatra-technical.service', 'dispatra-email.service',
    'dispatra-verification.service', 'dispatra-integrations.service', 'dispatra-connectors.service',
    'dispatra-push.service', 'dispatra-company-billing.service')
CONTROL = '/usr/local/libexec/dispatra-repair-control'
RELEASES = Path('/opt/dispatra/repair-releases')
CURRENT_WEB = Path('/opt/dispatra/web')
WATCHDOG_STATE = Path('/var/lib/dispatra-watchdog')


class ProductionInstaller(LocalInstaller):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.root.resolve() != Path('/opt/dispatra') / self.project:
            raise UnsafeRepair('Production source root is not the configured deployment.')

    def control(self, *args):
        return subprocess.run(['sudo', '-n', CONTROL, *args], check=True, timeout=90,
            capture_output=True, text=True).stdout.strip()

    def service(self, action, *names):
        if action in ('start', 'stop'):
            for name in names: self.control(action, name)
            return ''
        return subprocess.run(['systemctl', action, *names], check=True, timeout=30,
            capture_output=True, text=True).stdout.strip()

    def active(self):
        if self.project == 'client': return []  # Caddy serves the new symlink atomically; no stop needed.
        active = []
        for name in SERVICES:
            try:
                if self.service('is-active', name) == 'active': active.append(name)
            except subprocess.CalledProcessError: continue
        if 'dispatra-api.service' not in active: raise UnsafeRepair('API availability must be restored before a code release.')
        return active

    def prepare(self, candidate):
        if self.project != 'client': return
        artifact = candidate / '.artifacts/web'
        if not (artifact / 'index.html').is_file(): raise UnsafeRepair('The tested client build artifact is missing.')
        if any(p.is_symlink() for p in artifact.rglob('*')): raise UnsafeRepair('Client artifacts cannot contain symlinks.')
        release = RELEASES / str(UUID(self.folder.name)) / 'web'
        release.parent.mkdir(parents=True, exist_ok=False)
        # Retain earlier hashed assets/downloads for open browser tabs; new build replaces current entry points.
        shutil.copytree(CURRENT_WEB, release, symlinks=False, ignore=shutil.ignore_patterns('*.map'))
        shutil.copytree(artifact, release, dirs_exist_ok=True)

    def save(self, record):
        if self.project == 'client' and 'web_before' not in record:
            record['web_before'] = self.control('current-web')
            record['web_after'] = str(UUID(self.folder.name))
        super().save(record)

    def healthy(self):
        from urllib.request import urlopen
        import time
        expected = None
        if self.project == 'client' and self.journal.exists():
            record = json.loads(self.journal.read_text())
            target = record['web_before'] if record['state'] == 'ROLLED_BACK' else record['web_after']
            self.control('web', target)
            expected = (RELEASES / target / 'web/index.html').read_bytes()
        for _ in range(12):
            self.heartbeat()
            try:
                with urlopen('http://127.0.0.1:8000/ready', timeout=8) as response:
                    ready = response.status == 200 and json.load(response).get('status') == 'ready'
                with urlopen('https://dispatra.com/', timeout=8) as response:
                    published = response.status == 200 and (expected is None or response.read() == expected)
                if ready and published: return True
            except OSError: pass
            time.sleep(2)
        return False

    @contextmanager
    def maintenance(self):
        # The watchdog sees a bounded maintenance lease, not an unlimited outage suppression.
        import time
        lease = WATCHDOG_STATE / 'maintenance.json'
        import fcntl
        with (WATCHDOG_STATE / 'release.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lease.write_text(json.dumps({'until': time.time() + 180, 'job': self.folder.name}))
            try: yield
            finally: lease.unlink(missing_ok=True)

    def apply(self, originals, updates, stage):
        with self.maintenance(): super().apply(originals, updates, stage)

    def recover(self):
        if self.journal.exists() and json.loads(self.journal.read_text())['state'] == 'INSTALLING':
            with self.maintenance(): super().recover()

    def rollback(self, record):
        try: super().rollback(record)
        finally:
            if self.project == 'client': self.control('web', record['web_before'])
