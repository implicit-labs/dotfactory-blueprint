"""Real local command/Git/evidence gates; fixture artifacts are not UI proof."""
import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotfactory import worker
from dotfactory.verification_contract import resolve, validate, assert_coverage, summary, frozen
from dotfactory.cli import _git
from dotfactory.instance import FactoryConfig
from dotfactory.lifecycle import FactoryRuntime
from dotfactory.delivery import planned_receipt, latest_check, require_receipt, DeliveryError
from test_planning_conversation import ChatRunner
import test_planning_conversation as chat_fixture
import test_verified_delivery as fixture


def method():
    return {'description': 'Compare the same greeting scenario before and after',
            'hosts': ['coordinator'], 'requires': ['tool:git'], 'readiness': [],
            'commands': {phase: ['{python}', '-c',
                "from pathlib import Path; import sys; Path(sys.argv[1], 'result.txt').write_text(Path('greeting.py').read_text())",
                '{artifacts}'] for phase in ('before', 'after')},
            'artifacts': [{'phase': phase, 'path': 'result.txt', 'kind': 'report', 'scenario': 'greeting source fixture'} for phase in ('before', 'after')],
            'timeout_seconds': 10, 'files': []}


def registry():
    regression = method()
    regression['files'] = ['.factory/verify.py']
    visual = method()
    for artifact in visual['artifacts']:
        artifact['kind'] = 'screenshot'
        artifact['path'] = 'screen.png'
    return {'methods': {'regression': regression, 'visual': visual}, 'defaults': ['regression'],
            'rules': [{'paths': ['web/*'], 'categories': ['frontend', 'ios'], 'methods': ['visual']}]}


def selection(paths=None, add=None, omit=None):
    return {'paths': paths or ['greeting.py'], 'categories': [], 'add': add or [], 'omit': omit or {}}


class ResolutionTests(unittest.TestCase):
    def test_default_inference_override_and_scope(self):
        config = registry()
        self.assertEqual(['regression'], list(resolve(config, selection())['methods']))
        for path in ('web/page.ts', 'src/Page.tsx', 'App/Screen.swift'):
            contract = resolve(config, selection([path]))
            self.assertEqual({'regression', 'visual'}, set(contract['methods']))
            self.assertIn('evidence:', summary(contract))
        reduced = resolve(config, selection(['Page.tsx'], omit={'visual': 'Only changes a comment; reviewed'}))
        self.assertEqual(['regression'], list(reduced['methods']))
        self.assertEqual(2, len(resolve(config, selection(add=['visual']))['methods']))
        isolated = registry()
        isolated['defaults'] = ['visual']
        self.assertEqual(['visual'], list(resolve(isolated, selection())['methods']))
        self.assertEqual(['regression'], list(resolve(config, selection())['methods']))

    def test_ui_and_interaction_cannot_silently_fall_back_to_unit_tests(self):
        config = registry()
        config['rules'] = []
        with self.assertRaisesRegex(ValueError, 'visual evidence'):
            resolve(config, selection(['Page.tsx']))
        value = selection(['Page.tsx'])
        value['categories'] = ['interaction']
        with self.assertRaisesRegex(ValueError, 'recording'):
            resolve(registry(), value)

    def test_actual_diff_cannot_evade_selection(self):
        config = registry()
        with self.assertRaisesRegex(ValueError, 'revised verification plan'):
            assert_coverage(resolve(config, selection()), config, ['src/Page.tsx'])

    def test_invalid_overrides_and_declarations(self):
        for value in (selection(omit={'regression': ''}), selection(add=['unknown']), selection(['../outside'])):
            with self.assertRaises(ValueError):
                resolve(registry(), value)
        config = registry()
        config['methods']['regression']['artifacts'][1]['scenario'] = 'different device'
        with self.assertRaisesRegex(ValueError, 'matching after'):
            validate(config)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _git(self.root, 'init', '-q')
        _git(self.root, 'config', 'user.email', 'test@example.invalid')
        _git(self.root, 'config', 'user.name', 'Test')
        (self.root / 'greeting.py').write_text('before')
        _git(self.root, 'add', '.')
        _git(self.root, 'commit', '-qm', 'base')
        self.sha = worker.git(self.root, 'rev-parse', 'HEAD')
        self.spec = {'op': 'verify-method', 'method': method(), 'phase': 'before',
                     'source_sha': self.sha, 'bundle': worker.bundle(self.root, self.sha)}

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_command_source_and_artifact_provenance(self):
        result = worker.dispatch(self.spec)
        self.assertTrue(result['passed'])
        self.assertEqual(b'before', base64.b64decode(result['artifacts'][0]['data_base64']))
        self.assertEqual(self.sha, result['source_sha'])
        self.assertEqual(worker.digest(method()), result['method_digest'])

    def test_missing_artifact_nonzero_wrong_media_and_source_mutation(self):
        commands = ["pass", "raise SystemExit(7)", "open('greeting.py','w').write('tampered')"]
        for command in commands:
            spec = copy.deepcopy(self.spec)
            spec['method']['commands']['before'] = ['{python}', '-c', command]
            if command.startswith('raise'):
                self.assertFalse(worker.dispatch(spec)['passed'])
            else:
                with self.assertRaises(worker.WorkerError):
                    worker.dispatch(spec)
        self.spec['method']['artifacts'][0]['kind'] = 'screenshot'
        self.spec['method']['artifacts'][1]['kind'] = 'screenshot'
        with self.assertRaisesRegex(worker.WorkerError, 'PNG or JPEG'):
            worker.dispatch(self.spec)

    def test_capability_and_deadline(self):
        self.spec['method']['requires'] = ['os:unavailable']
        self.assertFalse(worker.dispatch({'op': 'verification-probe', 'method': self.spec['method']})['available'])
        with self.assertRaisesRegex(worker.WorkerError, 'prerequisites'):
            worker.dispatch(self.spec)
        self.spec['method']['requires'] = []
        self.spec['method']['commands']['before'] = ['{python}', '-c', 'import time; time.sleep(10)']
        self.spec['method']['timeout_seconds'] = 1
        self.assertFalse(worker.dispatch(self.spec)['passed'])

    def test_changed_harness_cannot_manufacture_a_pass(self):
        import hashlib
        harness = self.root / 'check.py'
        harness.write_text('raise SystemExit(1)')
        _git(self.root, 'add', '.')
        _git(self.root, 'commit', '-qm', 'approved harness')
        pinned = {'check.py': hashlib.sha256(harness.read_bytes()).hexdigest()}
        harness.write_text('print("fake pass")')
        _git(self.root, 'add', '.')
        _git(self.root, 'commit', '-qm', 'weaken harness')
        sha = worker.git(self.root, 'rev-parse', 'HEAD')
        self.spec.update(source_sha=sha, bundle=worker.bundle(self.root, sha), pinned_files=pinned)
        self.spec['method']['commands']['before'] = ['{python}', 'check.py']
        self.spec['method']['files'] = ['check.py']
        with self.assertRaisesRegex(worker.WorkerError, 'harness changed'):
            worker.dispatch(self.spec)
        self.spec['method']['files'] = []
        with self.assertRaisesRegex(ValueError, 'declared in files'):
            worker.dispatch(self.spec)

    def test_local_transport_uses_worker_protocol(self):
        from dotfactory.execution import call
        result = call({'transport': 'local'}, self.spec)
        self.assertTrue(result['passed'])


class ContractRunner(ChatRunner):
    def run(self, launch):
        result = super().run(launch)
        if launch.request.state_id in ('Autoplanning', 'Planning'):
            root = Path(launch.workspace_path)
            path = root / '.factory/planning-requirements.json'
            value = json.loads(path.read_text())
            value.update(schema_version=2, verification=selection())
            path.write_text(json.dumps(value))
            _git(root, 'add', '.')
            _git(root, 'commit', '-qm', 'select verification methods')
        return result


class LifecycleTests(unittest.TestCase):
    setUp = fixture.VerifiedDeliveryTests.setUp
    tearDown = fixture.VerifiedDeliveryTests.tearDown
    use_automatic_workflow = fixture.VerifiedDeliveryTests.use_automatic_workflow
    approve = chat_fixture.PlanningConversationTests.approve

    def configure(self, config=None):
        values = copy.deepcopy(self.config.values)
        values['projects']['demo']['verification'] = config or registry()
        path = self.root / 'methods.json'
        path.write_text(json.dumps(values))
        self.config = FactoryConfig.load(path)
        self.use_automatic_workflow()

    def test_review_restart_real_before_after_and_tamper_gate(self):
        self.configure()
        runner = ContractRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            execution = runtime.start_issue('demo', 'METHOD-1')
            self.config.values['projects']['demo']['verification']['defaults'].append('visual')
            self.assertEqual(['regression'], frozen(runtime.ledger, execution)['defaults'])
            runtime.run([execution], until_state='PlanReview', max_ticks=8)
            receipt = planned_receipt(runtime.ledger, execution)['receipt']
            self.assertIn('verification_summary', receipt['planning_requirements'])
            self.assertIn('.factory/verify.py', receipt['planning_requirements']['verification']['pinned_files'])
            self.assertEqual(['Autoplanning'], runner.calls)
            self.approve(runtime, execution)
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            runtime.run([execution], until_state='Review', max_ticks=10)
            self.assertEqual('Review', runtime.ledger.current(execution)['current_state_id'])
            receipt = latest_check(runtime.ledger, execution)
            before = receipt['verification_baseline']['methods']['regression']['result']
            after = receipt['verification_methods']['methods']['regression']['result']
            self.assertNotEqual(before['source_sha'], after['source_sha'])
            from types import SimpleNamespace
            from dotfactory.verification_contract import run_phase
            launch = SimpleNamespace(request=SimpleNamespace(execution_id=execution, attempt_id=receipt['attempt_id']))
            with patch('dotfactory.worker.dispatch', side_effect=AssertionError('must reuse durable evidence')):
                self.assertTrue(run_phase(runtime.ledger, launch, 'after', after['source_sha'])['passed'])
            with self.assertRaisesRegex(DeliveryError, 'different source'):
                run_phase(runtime.ledger, launch, 'after', before['source_sha'])
            self.assertIn('hello', Path(before['artifacts'][0]['uri']).read_text())
            self.assertIn('こんにちは', Path(after['artifacts'][0]['uri']).read_text())
            from dotfactory.delivery import export_review
            destination = self.root / 'review-export'
            export_review(runtime.ledger, execution, str(destination))
            self.assertEqual(Path(after['artifacts'][0]['uri']).read_bytes(),
                             (destination / 'verification-artifacts' / after['artifacts'][0]['sha256']).read_bytes())
            Path(after['artifacts'][0]['uri']).write_text('tampered')
            with self.assertRaisesRegex(DeliveryError, 'artifact missing or changed'):
                require_receipt(runtime.ledger, receipt['attempt_id'], receipt['contract'])

    def test_incompatible_workflow_cannot_silently_ignore_methods(self):
        from dotfactory.lifecycle import LifecycleError
        self.configure()
        with FactoryRuntime(self.config, runner=ContractRunner()) as runtime:
            runtime.kernels['demo'].states['Verifying']['execution'].pop('exit_contract')
            with self.assertRaisesRegex(LifecycleError, 'verification workflow'):
                runtime.start_issue('demo', 'METHOD-INCOMPATIBLE')

    def test_unavailable_host_blocks_implementation(self):
        config = registry()
        config['methods']['regression']['requires'] = ['os:unavailable']
        self.configure(config)
        runner = ContractRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            execution = runtime.start_issue('demo', 'METHOD-BLOCKED')
            runtime.run([execution], until_state='PlanReview', max_ticks=8)
            self.approve(runtime, execution)
            runtime.run([execution], max_ticks=3)
            self.assertEqual(['Autoplanning'], runner.calls)
            self.assertNotEqual('Review', runtime.ledger.current(execution)['current_state_id'])


if __name__ == '__main__':
    unittest.main()
