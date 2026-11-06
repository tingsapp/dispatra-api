"""Explicit real namespace acceptance; no live application or model call."""
import os
from pathlib import Path
import tempfile
import unittest
from app.agents.code_repair.sandbox import Sandbox
from app.agents.code_repair.workspace import files, materialize


@unittest.skipUnless(os.environ.get('RUN_CODE_REPAIR_SANDBOX_TESTS') == '1', 'Explicit isolated-runner acceptance only')
class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        api = Path(__file__).resolve().parents[1]
        self.runner = Sandbox({'api': api, 'client': api.parent / 'client'}, timeout=180)
        (self.work / 'client/src').mkdir(parents=True)
        (self.work / 'client/tests').mkdir()
        (self.work / 'api').mkdir()

    def test_client_assertion_failure_then_pass_and_host_isolation(self):
        (self.work / 'client/package.json').write_text('{"type":"module"}')
        source = self.work / 'client/src/format.ts'; source.write_text('export const double = (x: number) => x;')
        test = self.work / 'client/tests/fault.test.ts'
        test.write_text("import test from 'node:test'; import assert from 'node:assert/strict'; import {double} from '../src/format.ts';\n"
            "import {existsSync} from 'node:fs';\n"
            "test('isolated and fixed', () => {assert.equal(existsSync('/home/amu'), false); assert.equal(process.env.OPENAI_API_KEY, undefined); assert.equal(double(2), 4);});")
        before = self.runner.run(self.work, 'client', 'regression', 'tests/fault.test.ts', self.work / 'before.log')
        self.assertTrue(before.assertion_failed, before.summary)
        source.write_text('export const double = (x: number) => x * 2;')
        after = self.runner.run(self.work, 'client', 'regression', 'tests/fault.test.ts', self.work / 'after.log')
        self.assertTrue(after.passed, after.summary)

    def test_api_uses_disposable_postgres_and_reproduces_then_passes(self):
        api = self.runner.roots['api']
        snapshot = files(api, 'api')
        snapshot = {name: data for name, data in snapshot.items() if not name.startswith('tests/') or name == 'tests/conftest.py'}
        materialize(snapshot, self.work / 'api')
        source = self.work / 'api/app/repair_fixture.py'; source.write_text('def double(x): return x\n')
        (self.work / 'api/tests/test_fault.py').write_text('from app.repair_fixture import double\n'
            'from conftest import owner_engine\nfrom sqlalchemy import text\n'
            'def test_fault():\n    with owner_engine.connect() as db:\n        assert db.scalar(text("SELECT current_database()")) == "code_repair_test"\n    assert double(2) == 4\n')
        before = self.runner.run(self.work, 'api', 'regression', 'tests/test_fault.py', self.work / 'before.log')
        self.assertTrue(before.assertion_failed, before.summary)
        source.write_text('def double(x): return x * 2\n')
        after = self.runner.run(self.work, 'api', 'regression', 'tests/test_fault.py', self.work / 'after.log')
        self.assertTrue(after.passed, after.summary)


if __name__ == '__main__': unittest.main()
