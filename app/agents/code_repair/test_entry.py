"""Trusted test entry point, mounted read-only inside the networkless test namespace."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import xml.etree.ElementTree as ET

PYTHON = '/deps/python/bin/python'
PG = '/deps/postgres/usr/lib/postgresql/16/bin/'


def command(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, check=False)
    if result.returncode: raise SystemExit(result.returncode)


def api(mode, test):
    os.environ['LD_LIBRARY_PATH'] = '/deps/postgres/usr/lib/x86_64-linux-gnu'
    command([PG + 'initdb', '-D', '/tmp/database', '-A', 'trust', '-U', 'test_owner'], '/tmp')
    command([PG + 'pg_ctl', '-D', '/tmp/database', '-l', '/tmp/postgres.log', '-o',
        "-k /tmp -c listen_addresses='' -c fsync=off -c synchronous_commit=off", 'start'], '/tmp')
    command([PG + 'createdb', '-h', '/tmp', '-U', 'test_owner', 'code_repair_test'], '/tmp')
    os.environ['PGHOST'] = '/tmp'
    owner = 'postgresql+psycopg://test_owner@/code_repair_test'
    runtime = 'postgresql+psycopg://dispatra_app@/code_repair_test'
    os.environ.update(MIGRATION_DATABASE_URL=owner, TEST_OWNER_DATABASE_URL=owner,
        TEST_DATABASE_URL=runtime, DATABASE_URL=runtime, OPENAI_API_KEY='')
    command([PYTHON, '-m', 'alembic', 'upgrade', 'head'], '/work/api')
    command([PG + 'psql', '-h', '/tmp', '-U', 'test_owner', '-d', 'code_repair_test', '-c',
        'ALTER ROLE dispatra_app LOGIN'], '/tmp')
    args = [PYTHON, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', test if mode == 'regression' else 'tests', '--junitxml=/tmp/results.xml']
    result = subprocess.run(args, cwd='/work/api')
    if mode == 'regression':
        # Collection/import failures, skips and an empty suite do not reproduce a bug.
        try:
            cases = list(ET.parse('/tmp/results.xml').iter('testcase'))
            failed = [c for c in cases if c.find('failure') is not None]
            invalid = any(c.find('error') is not None or c.find('skipped') is not None for c in cases)
            assertion = bool(failed) and all('AssertionError' in (c.find('failure').get('message', '') + (c.find('failure').text or '')) for c in failed)
            if cases and not invalid and result.returncode == 0: print('REPAIR_REGRESSION_PASS', flush=True)
            elif assertion and not invalid and result.returncode == 1: print('REPAIR_REGRESSION_FAIL', flush=True)
        except (OSError, ET.ParseError): pass
    raise SystemExit(result.returncode)


def client(mode, test):
    cwd = '/work/client'
    if mode == 'regression':
        result = subprocess.run(['node', '--import', 'tsx', '--test', '--test-reporter=tap', test],
            cwd=cwd, capture_output=True, text=True)
        print(result.stdout); print(result.stderr)
        # Require a real assertion failure, never syntax/loader failures or skipped tests.
        import re
        count = re.search(r'^# tests (\d+)$', result.stdout, re.M)
        skip = re.search(r'^# (?:skipped|cancelled) [1-9]', result.stdout, re.M)
        if count and int(count[1]) and not skip:
            if result.returncode == 0: print('REPAIR_REGRESSION_PASS', flush=True)
            elif 'ERR_ASSERTION' in result.stdout: print('REPAIR_REGRESSION_FAIL', flush=True)
        raise SystemExit(result.returncode)
    scripts = json.loads(Path(cwd, 'package.json').read_text())['scripts']
    if mode == 'suite':
        generated = sorted(str(p.relative_to(cwd)) for p in Path(cwd, 'tests').glob('technicalGenerated*.test.ts'))
        if generated: command(['node', '--import', 'tsx', '--test', *generated], cwd)
        command(['sh', '-c', scripts['test']], cwd)
    command(['node', 'node_modules/typescript/bin/tsc', '--noEmit'], cwd)
    command(['node', 'node_modules/vite/bin/vite.js', 'build'], cwd)
    # Publish exactly the artifact built in this isolated, validated candidate.
    output = Path('/artifacts/web')
    if output.exists(): shutil.rmtree(output)
    shutil.copytree(Path(cwd, 'dist'), output)


if __name__ == '__main__':
    project, mode, test = sys.argv[1:]
    (api if project == 'api' else client)(mode, test)
