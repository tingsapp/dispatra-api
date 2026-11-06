"""The only route from an AI proposal to local source goes through all test gates."""
import json
from pathlib import Path
from .policy import UnsafeRepair, validate
from .workspace import files, materialize, digest


def run(project, roots, folder, fault, planner, sandbox, installer, stage):
    originals = {name: files(root, name) for name, root in roots.items()}
    selected = planner.context(project, fault, originals[project])
    feedback = None
    regression = 'tests/test_technical_generated.py' if project == 'api' else 'tests/technicalGenerated.test.ts'
    if regression in originals[project]:
        # Every incident owns a unique regression; never replace previous tests.
        regression = regression.replace('.py', '_' + folder.name.replace('-', '') + '.py') if project == 'api' else regression.replace('.test.ts', folder.name.replace('-', '') + '.test.ts')
    if regression in originals[project]: raise UnsafeRepair('The regression test already exists.')
    for attempt in range(1, 3):
        stage('ANALYZING', f'Preparing code repair attempt {attempt} of 2.')
        proposal = validate(project, planner.propose(project, fault, selected, feedback), originals[project])
        if any(edit.path not in selected for edit in proposal.edits):
            raise UnsafeRepair('The proposed fix edits a file that was not inspected.')
        planner.review(project, fault, originals[project], proposal)
        (folder / f'proposal-{attempt}.json').write_text(proposal.model_dump_json(indent=2))
        updates = {edit.path: edit.content.encode() for edit in proposal.edits}
        updates[regression] = proposal.regression.encode()
        base = folder / f'baseline-{attempt}'
        candidate = folder / f'candidate-{attempt}'
        for name, source in originals.items():
            materialize(source, base / name); materialize(source, candidate / name)
        materialize({regression: updates[regression]}, base / project)
        materialize(updates, candidate / project)
        expected = {name: {**source, **(updates if name == project else {})} for name, source in originals.items()}
        stage('REPRODUCING', 'Checking that the new test reproduces the reported fault on the original code.')
        before = sandbox.run(base, project, 'regression', regression, folder / f'baseline-{attempt}.log')
        if not before.assertion_failed:
            feedback = 'The regression did not reproduce an assertion failure on the original code. ' + before.summary
            continue
        stage('TESTING', 'Running the regression against the fix, then the existing tests and build checks.')
        after = sandbox.run(candidate, project, 'regression', regression, folder / f'regression-{attempt}.log')
        if not after.passed:
            feedback = 'The regression still fails after the proposed fix. ' + after.summary
            continue
        suite = sandbox.run(candidate, project, 'suite', regression, folder / f'suite-{attempt}.log')
        if not suite.passed:
            feedback = 'Existing tests or build checks failed. ' + suite.summary
            continue
        # Generated tests/builds must not edit source, configuration or existing test files.
        for name, source in expected.items():
            if digest(files(candidate / name, name)) != digest(source):
                raise UnsafeRepair('The test run modified the source snapshot.')
        # Reject concurrent changes anywhere in either project, including API/client contracts.
        for name, root in roots.items():
            if digest(files(root, name)) != digest(originals[name]):
                raise UnsafeRepair('Application source changed during testing; no fix was applied.')
        (folder / 'evidence.json').write_text(json.dumps({'project': project, 'attempt': attempt,
            'base': digest(originals[project]), 'candidate': digest(expected[project]),
            'regression_failed_before': True, 'regression_passed_after': True, 'suite_passed': True}))
        prepare = getattr(installer, 'prepare', None)
        if prepare: prepare(candidate)
        installer.apply(originals[project], updates, stage)
        return
    raise UnsafeRepair('Two repair attempts could not reproduce and safely fix the fault with passing tests.')
