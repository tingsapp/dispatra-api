"""Private, bounded disk storage for derived ORS routes, not business records."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import fcntl


class RouteCacheStore:
    def __init__(self, directory, *, max_entries=512, max_bytes=32_000_000, clock=time.time):
        self.directory = Path(directory)
        self.max_entries, self.max_bytes, self.clock = max_entries, max_bytes, clock

    def _read(self, path, ttl):
        try:
            if path.stat().st_size > self.max_bytes: return None
            data = json.loads(path.read_bytes())
            remaining = float(data['expires_at']) - self.clock()
            encoded = json.dumps(data['value'], separators=(',', ':'), sort_keys=True).encode()
            if data['version'] != 1 or not 0 < remaining <= ttl or hashlib.sha256(encoded).hexdigest() != data['checksum']:
                return None
            path.touch()
            return data['value'], remaining
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write(self, path, value, expires_at):
        encoded = json.dumps(value, separators=(',', ':'), sort_keys=True).encode()
        data = json.dumps(dict(version=1, expires_at=expires_at,
            checksum=hashlib.sha256(encoded).hexdigest(), value=value), separators=(',', ':')).encode()
        if len(data) > self.max_bytes: return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix='.route-', delete=False) as out:
                temporary = out.name
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary): os.unlink(temporary)

    def _prune(self):
        with (self.directory / '.prune.lock').open('a+b') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            files = []
            for path in self.directory.glob('*.json'):
                try:
                    stat = path.stat()
                    files.append((stat.st_mtime, stat.st_size, path))
                except FileNotFoundError: pass
            files.sort()
            size, count = sum(row[1] for row in files), len(files)
            for _, length, path in files:
                if size <= self.max_bytes and count <= self.max_entries: break
                path.unlink(missing_ok=True)
                size -= length
                count -= 1

    def get_or_compute(self, key, compute, ttl):
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('Invalid route cache key')
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            # A fixed number of locks bounds sidecar files and coalesces across processes.
            guard = (self.directory / f'.stripe-{int(key[:2], 16) % 64}.lock').open('a+b')
        except OSError:
            return compute(), ttl
        with guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX)
            except OSError:
                return compute(), ttl
            path = self.directory / (key + '.json')
            cached = self._read(path, ttl)
            if cached is not None: return cached
            value = compute()  # Provider errors never become persistent entries.
            expires_at = self.clock() + ttl
            try:
                self._write(path, value, expires_at)
                self._prune()
            except OSError:
                pass  # Cache availability must not prevent an otherwise valid route.
            return value, max(0, expires_at - self.clock())
