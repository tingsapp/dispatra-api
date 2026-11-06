#!/usr/bin/python3
"""Root-owned narrow service/static-release helper. Never execute candidate code or shell."""
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID

NAMES = ('api','intake','dispatch','operational','technical','email','verification','integrations',
    'connectors','push','company-billing')
BASE = Path('/opt/dispatra/repair-releases')


def main(args):
    if args == ['current-web']:
        current = Path('/opt/dispatra/web').resolve()
        if current.parent.parent != BASE or current.name != 'web': raise ValueError('Unmanaged web release')
        print(str(UUID(current.parent.name))); return
    if len(args) != 2: raise ValueError('Unsupported action')
    action, value = args
    if action in ('start','stop') and value in {f'dispatra-{name}.service' for name in NAMES}:
        subprocess.run(['/usr/bin/systemctl',action,value],check=True,timeout=75); return
    if action == 'web':
        identity = str(UUID(value)); target = BASE / identity / 'web'
        if target.is_symlink() or target.resolve() != target or not (target/'index.html').is_file(): raise ValueError('Invalid release')
        if any(p.is_symlink() for p in target.rglob('*')): raise ValueError('Symlink in release')
        temporary = Path('/opt/dispatra/.web-next')
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        os.replace(temporary, '/opt/dispatra/web'); return
    raise ValueError('Unsupported action')


if __name__ == '__main__': main(sys.argv[1:])
