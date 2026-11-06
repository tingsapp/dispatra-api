"""Run generated code with no host home, credentials, network or live database access."""
from dataclasses import dataclass
import os
from pathlib import Path
import resource
import signal
import subprocess
import tempfile
import time
from dotenv import dotenv_values
from .policy import UnsafeRepair


@dataclass
class Result:
    passed: bool
    assertion_failed: bool
    summary: str


def limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class Sandbox:
    def __init__(self, roots, heartbeat=lambda: None, timeout=7200):
        self.roots, self.heartbeat, self.timeout = roots, heartbeat, timeout
        # This browser-visible key is part of the deployed web build, never an API/provider secret.
        settings = {}
        for name in ('.env', '.env.local', '.env.production', '.env.production.local'):
            path = roots['client'] / name
            if path.is_file(): settings.update(dotenv_values(path))
        self.maps_key = os.environ.get('VITE_GOOGLE_MAPS_API_KEY') or settings.get('VITE_GOOGLE_MAPS_API_KEY')

    def argv(self, work, project, mode, regression, identity):
        api, client = self.roots['api'], self.roots['client']
        args = [os.environ.get('TECHNICAL_TEST_BWRAP', '/usr/bin/bwrap'), '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
            '--clearenv', '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/tmp/home',
            '--ro-bind', str(identity / 'passwd'), '/etc/passwd', '--ro-bind', str(identity / 'group'), '/etc/group',
            '--ro-bind', str(identity / 'hosts'), '/etc/hosts',
            '--ro-bind', str(work), '/work', '--tmpfs', '/work/client/dist',
            '--ro-bind', os.environ.get('TECHNICAL_TEST_PYTHON', str(api / '.venv')), '/deps/python',
            '--ro-bind', os.environ.get('TECHNICAL_TEST_POSTGRES', str(api / '.local/postgres-runtime')), '/deps/postgres',
            '--ro-bind', str(client / 'node_modules'), '/work/client/node_modules',
            '--tmpfs', '/work/client/node_modules/.vite', '--tmpfs', '/work/client/node_modules/.vite-temp',
            '--ro-bind', str(Path(__file__).with_name('test_entry.py')), '/test_entry.py',
            '--bind', str(work / '.artifacts'), '/artifacts',
            '--ro-bind', os.environ.get('TECHNICAL_TEST_NODE', '/usr/lib/chatgpt/resources/cua_node'), '/deps/node',
            '--setenv', 'PATH', '/deps/node/bin:/work/client/node_modules/.bin:/usr/bin:/bin',
            '--setenv', 'HOME', '/tmp/home', '--setenv', 'LANG', 'C.UTF-8',
            '--setenv', 'PYTHONPATH', '/work/api', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
            '--setenv', 'NODE_OPTIONS', '--max-old-space-size=2048',
            '--chdir', '/work/' + project, '/deps/python/bin/python', '/test_entry.py', project, mode, regression]
        if project == 'client' and self.maps_key:
            position = args.index('--chdir')
            args[position:position] = ['--setenv', 'VITE_GOOGLE_MAPS_API_KEY', self.maps_key]
        elif project == 'client' and mode == 'suite' and os.environ.get('TECHNICAL_PRODUCTION_CODE_REPAIR') == '1':
            raise UnsafeRepair('The deployed public map configuration is missing; web publication is blocked.')
        return args

    def run(self, work, project, mode, regression, log):
        (work / 'client/node_modules').mkdir(parents=True, exist_ok=True)
        (work / 'client/dist').mkdir(parents=True, exist_ok=True)
        (work / '.artifacts').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='technical-test-user-') as directory, log.open('wb') as output:
            identity = Path(directory)
            (identity / 'passwd').write_text(f'repair:x:{os.getuid()}:{os.getgid()}::/tmp/home:/bin/sh\n')
            (identity / 'group').write_text(f'repair:x:{os.getgid()}:\n')
            (identity / 'hosts').write_text('127.0.0.1 localhost\n::1 localhost\n')
            child = subprocess.Popen(self.argv(work, project, mode, regression, identity), stdout=output, stderr=subprocess.STDOUT,
                env={'PATH': '/usr/bin:/bin'}, start_new_session=True, preexec_fn=limits)
            start = time.monotonic()
            try:
                while child.poll() is None:
                    if time.monotonic() - start > self.timeout: raise UnsafeRepair('The isolated test run timed out.')
                    self.heartbeat()
                    time.sleep(2)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL); child.wait()
        output = log.read_text(errors='replace')[-16000:]
        success = child.returncode == 0 and (mode != 'regression' or 'REPAIR_REGRESSION_PASS' in output)
        assertion = child.returncode == 1 and 'REPAIR_REGRESSION_FAIL' in output
        # Only isolated synthetic output may be fed back to the planner, never host/live logs.
        return Result(success, assertion, output[-6000:])
