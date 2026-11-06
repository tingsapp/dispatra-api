"""Apply only tested bytes to local services, with a durable rollback journal."""
import base64
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen
from .policy import UnsafeRepair
from .workspace import atomic_write, digest, files

SERVICES = {'api': ('dispatra-local-api.service', 'dispatra-local-intake.service', 'dispatra-local-operational.service'),
    'client': ('dispatra-local-client.service',)}
HEALTH = {'api': 'http://127.0.0.1:8000/ready', 'client': 'http://127.0.0.1:3000/'}


class LocalInstaller:
    def __init__(self, root, project, folder, heartbeat=lambda: None, runtime_ok=lambda: True):
        self.root, self.project, self.folder = root, project, folder
        self.heartbeat, self.runtime_ok = heartbeat, runtime_ok
        self.journal = folder / 'install.json'

    def service(self, action, *names):
        return subprocess.run(['systemctl', '--user', action, *names], timeout=45,
            capture_output=True, text=True, check=True).stdout.strip()

    def active(self):
        result = []
        for name in SERVICES[self.project]:
            try:
                if self.service('is-active', name) == 'active':
                    directory = self.service('show', name, '--property=WorkingDirectory', '--value')
                    if Path(directory).resolve() != self.root.resolve(): raise UnsafeRepair('Local service targets another checkout.')
                    result.append(name)
            except subprocess.CalledProcessError: continue
        if SERVICES[self.project][0] not in result: raise UnsafeRepair('The target local application is not running.')
        return result

    def healthy(self):
        for attempt in range(12):
            self.heartbeat()
            try:
                with urlopen(HEALTH[self.project], timeout=3) as response:
                    if response.status == 200: return True
            except OSError: pass
            time.sleep(1)
        return False

    def save(self, record):
        # fsync plus replace keeps a recoverable journal before the first source write.
        atomic_write(self.journal, json.dumps(record).encode())

    def rollback(self, record):
        for name in record['services']: self.service('stop', name)
        conflicts = []
        for path, versions in record['files'].items():
            live = self.root / path
            old = base64.b64decode(versions['old']) if versions['old'] is not None else None
            new = base64.b64decode(versions['new'])
            current = live.read_bytes() if live.exists() else None
            if current not in (old, new):
                conflicts.append(path)
        for path, versions in record['files'].items():
            if path in conflicts: continue
            live = self.root / path
            if versions['old'] is None: live.unlink(missing_ok=True)
            else: atomic_write(live, base64.b64decode(versions['old']))
        for name in record['services']: self.service('start', name)
        if conflicts:
            raise UnsafeRepair('Rollback preserved a concurrent local edit and restored the other files. Admin intervention is required.')
        record['state'] = 'ROLLED_BACK'; self.save(record)
        if not self.healthy(): raise UnsafeRepair('The fix was rolled back, but the application still needs attention.')

    def recover(self):
        if not self.journal.exists(): return
        record = json.loads(self.journal.read_text())
        if record['state'] == 'INSTALLING': self.rollback(record)

    def apply(self, originals, updates, stage):
        if digest(files(self.root, self.project)) != digest(originals):
            raise UnsafeRepair('Application source changed during testing. The tested fix was not applied.')
        services = self.active()
        record = {'state': 'INSTALLING', 'project': self.project, 'services': services,
            'files': {path: {'old': base64.b64encode(originals[path]).decode() if path in originals else None,
                'new': base64.b64encode(data).decode()} for path, data in updates.items()}}
        self.save(record)
        try:
            stage('APPLYING', 'Applying the tested fix to the application.')
            for name in services: self.service('stop', name)
            if digest(files(self.root, self.project)) != digest(originals):
                raise UnsafeRepair('Application source changed before application.')
            for path, data in updates.items(): atomic_write(self.root / path, data)
            for name in services: self.service('start', name)
            stage('VERIFYING', 'Checking the running application after the tested fix.')
            if not self.healthy(): raise UnsafeRepair('The application health check failed after the fix.')
            for _ in range(15):
                self.heartbeat(); time.sleep(2)
                if not self.runtime_ok(): raise UnsafeRepair('New application errors appeared after the fix.')
            if not self.healthy(): raise UnsafeRepair('The application became unhealthy after the fix.')
            if any((self.root / path).read_bytes() != data for path, data in updates.items()):
                raise UnsafeRepair('The applied source changed during verification.')
            record['state'] = 'COMPLETE'; self.save(record)
        except BaseException:
            self.rollback(record)
            raise
