"""Policy, test-before-apply, concurrent edits and rollback; run with unittest, no live DB."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from app.agents.code_repair.policy import Edit, Proposal, UnsafeRepair, validate
from app.agents.code_repair import pipeline
from app.agents.code_repair.sandbox import Result
from app.agents.code_repair.install import LocalInstaller
from app.agents.code_repair.workspace import files


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.roots = {name: self.root / name for name in ('api', 'client')}
        for name, root in self.roots.items():
            (root / ('app' if name == 'api' else 'src')).mkdir(parents=True)
            (root / 'tests').mkdir()
        (self.roots['api'] / 'app/formatting.py').write_text('def label(value):\n    return value.strip()\n')
        (self.roots['client'] / 'src/format.ts').write_text('export const label = (x: string) => x.trim();\n')
        self.folder = self.root / 'job'; self.folder.mkdir()
        self.proposal = Proposal(explanation='Handle a missing optional label.', risk='LOW',
            edits=[Edit(path='app/formatting.py', content='def label(value):\n    return (value or "").strip()\n')],
            regression='from app.formatting import label\ndef test_missing():\n    assert label(None) == ""\n')

    def planner(self):
        return Mock(context=Mock(return_value={'app/formatting.py': 'source'}), propose=Mock(return_value=self.proposal))

    def run_pipeline(self, outcomes, installer=None):
        results = iter(outcomes)
        def result(*args):
            next_result = next(results)
            return next_result(*args) if callable(next_result) else next_result
        runner = Mock(run=Mock(side_effect=result))
        installer = installer or Mock()
        pipeline.run('api', self.roots, self.folder, {}, self.planner(), runner, installer, Mock())
        return runner, installer

    def test_apply_requires_failure_before_and_success_after_and_suite(self):
        runner, installer = self.run_pipeline([Result(False, True, 'AssertionError'), Result(True, False, ''), Result(True, False, '')])
        self.assertEqual([call.args[2] for call in runner.run.call_args_list], ['regression', 'regression', 'suite'])
        installer.apply.assert_called_once()
        self.assertTrue((self.folder / 'evidence.json').exists())

    def test_no_reproducer_never_applies_even_if_test_passes(self):
        installer = Mock()
        with self.assertRaises(UnsafeRepair): self.run_pipeline([Result(True, False, '')] * 2, installer)
        installer.apply.assert_not_called()

    def test_suite_failure_cannot_apply(self):
        installer = Mock()
        with self.assertRaises(UnsafeRepair):
            self.run_pipeline([Result(False, True, ''), Result(True, False, ''), Result(False, False, '')] * 2, installer)
        installer.apply.assert_not_called()

    def test_changed_test_snapshot_is_rejected(self):
        installer = Mock()
        def changed(work, *args):
            (work / 'api/tests/unexpected.py').write_text('tampered = True')
            return Result(True, False, '')
        with self.assertRaises(UnsafeRepair):
            self.run_pipeline([Result(False, True, ''), Result(True, False, ''), changed], installer)
        installer.apply.assert_not_called()

    def test_protected_paths_traversal_test_edits_and_imports_are_rejected(self):
        originals = files(self.roots['api'], 'api')
        for name in ('../oops.py', 'app/agents/brain.py', 'tests/test_existing.py', '/tmp/oops.py'):
            proposal = self.proposal.model_copy(deep=True); proposal.edits[0].path = name
            with self.assertRaises(UnsafeRepair): validate('api', proposal, {**originals, name: b'pass\n'})
        proposal = self.proposal.model_copy(deep=True); proposal.edits[0].content = 'import os\n' + proposal.edits[0].content
        with self.assertRaises(UnsafeRepair): validate('api', proposal, originals)

    def installer(self):
        installer = LocalInstaller(self.roots['api'], 'api', self.folder)
        installer.active = Mock(return_value=['dispatra-local-api.service'])
        installer.service = Mock(); installer.healthy = Mock(side_effect=[False, True])
        return installer

    def test_failed_health_rolls_back_exact_originals(self):
        installer = self.installer(); original = files(self.roots['api'], 'api')
        with self.assertRaises(UnsafeRepair):
            installer.apply(original, {'app/formatting.py': b'changed = True\n'}, Mock())
        self.assertEqual(files(self.roots['api'], 'api'), original)
        self.assertIn('ROLLED_BACK', installer.journal.read_text())

    def test_concurrent_local_change_is_not_overwritten(self):
        installer = self.installer(); original = files(self.roots['api'], 'api')
        (self.roots['api'] / 'app/formatting.py').write_text('user_change = True\n')
        with self.assertRaises(UnsafeRepair): installer.apply(original, {'app/formatting.py': b'fix\n'}, Mock())
        self.assertEqual((self.roots['api'] / 'app/formatting.py').read_text(), 'user_change = True\n')
        installer.service.assert_not_called()

    def test_interrupted_install_restores_original_files_before_retry(self):
        import base64
        installer = self.installer(); installer.healthy = Mock(return_value=True)
        original = (self.roots['api'] / 'app/formatting.py').read_bytes()
        installer.save({'state':'INSTALLING', 'services':['dispatra-local-api.service'], 'files':{
            'app/formatting.py': {'old':base64.b64encode(original).decode(), 'new':base64.b64encode(b'changed').decode()}}})
        (self.roots['api'] / 'app/formatting.py').write_bytes(b'changed')
        installer.recover()
        self.assertEqual((self.roots['api'] / 'app/formatting.py').read_bytes(), original)
        self.assertIn('ROLLED_BACK', installer.journal.read_text())

    def test_failed_rollback_preserves_user_edits_and_restarts_service(self):
        import base64
        installer = self.installer()
        record = {'state':'INSTALLING', 'services':['dispatra-local-api.service'], 'files':{
            'app/formatting.py': {'old':base64.b64encode(b'original').decode(), 'new':base64.b64encode(b'candidate').decode()}}}
        (self.roots['api'] / 'app/formatting.py').write_bytes(b'user edit')
        with self.assertRaises(UnsafeRepair): installer.rollback(record)
        self.assertEqual((self.roots['api'] / 'app/formatting.py').read_bytes(), b'user edit')
        installer.service.assert_any_call('start', 'dispatra-local-api.service')

    def test_source_symlinks_cannot_read_outside_checkout(self):
        (self.roots['api'] / 'app/elsewhere').symlink_to(self.roots['client'], target_is_directory=True)
        with self.assertRaises(UnsafeRepair): files(self.roots['api'], 'api')


if __name__ == '__main__': unittest.main()
