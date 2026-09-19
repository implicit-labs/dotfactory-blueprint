import copy
import json
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from test_work_queue import Tracker, candidate
from dotfactory.work_queue import WorkQueue

import test_execution as fixtures
from dotfactory import FactoryConfig, FactoryRuntime, ObservationService
from dotfactory.execution import ExecutionError, overlay_policy, settings_view
from dotfactory.lifecycle import LifecycleError


class ProjectExecutionTests(unittest.TestCase):
    setUp = fixtures.WorkerExecutionTests.setUp
    tearDown = fixtures.WorkerExecutionTests.tearDown

    def configure(self, project_rule):
        values = json.loads(self.path.read_text())
        values['projects']['demo']['execution'] = {'stages': {'Autoplanning': project_rule}}
        self.path.write_text(json.dumps(values))
        return values

    def test_project_defaults_and_run_can_increase_reduce_or_clear(self):
        base = self.policy
        project = {'stages': {'Autoplanning': {'requires': ['tool:git', 'os:darwin'],
            'readiness': [{'name': 'python', 'command': [sys.executable, '-c', 'pass']}]}}}
        inherited, sources = overlay_policy(base, project, origin='project')
        for required in (['tool:git', 'os:darwin', 'tool:python3'], ['tool:git'], []):
            resolved, provenance = overlay_policy(inherited, {'stages': {'Autoplanning': {
                'requires': required, 'readiness': []}}}, origin='run', provenance=sources)
            self.assertEqual(required, resolved['stages']['Autoplanning']['requires'])
            self.assertEqual([], resolved['stages']['Autoplanning']['readiness'])
            self.assertEqual('run', provenance['Autoplanning']['requires'])
            self.assertEqual('instance', provenance['Autoplanning']['workers'])
        self.assertEqual(['tool:git', 'os:darwin'], inherited['stages']['Autoplanning']['requires'])
        self.assertNotIn('readiness', base['stages']['Autoplanning'])

    def test_two_projects_dispatch_independently_and_explicit_run_reduction_works(self):
        values = self.configure({'requires': ['os:unavailable'], 'readiness': [
            {'name': 'strict-version', 'command': [sys.executable, '-c', 'raise SystemExit(1)']}]})
        values['projects']['web'] = copy.deepcopy(values['projects']['demo'])
        values['projects']['web']['tracker']['project_id'] = 'web-project'
        values['projects']['web']['execution'] = {'stages': {'Autoplanning': {'requires': ['tool:git'], 'readiness': []}}}
        self.path.write_text(json.dumps(values))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            blocked = runtime.start_issue('demo', 'IOS-DEFAULT')
            web = runtime.start_issue('web', 'WEB-DEFAULT')
            reduced = runtime.start_issue('demo', 'IOS-DOCS', execution_override={
                'stages': {'Autoplanning': {'requires': [], 'readiness': []}}})
            runtime.run([blocked, web, reduced], max_ticks=35)
            self.assertIsNone(runtime.ledger.workspace_for_execution(blocked))
            self.assertTrue(runtime.ledger.run_snapshot(blocked)['attention_requests'])
            for ex in (web, reduced):
                self.assertEqual('Review', runtime.ledger.current(ex)['current_state_id'])
            self.assertEqual('project', settings_view(runtime.ledger, web)['provenance']['Autoplanning']['requires'])
            self.assertEqual('run', settings_view(runtime.ledger, reduced)['provenance']['Autoplanning']['requires'])

    def test_policy_is_frozen_at_admission_before_dispatch_and_survives_restart(self):
        values = self.configure({'requires': ['tool:git']})
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution = runtime.start_issue('demo', 'FROZEN-1')
            initial = settings_view(runtime.ledger, execution)
            self.assertEqual('admission', initial['frozen_at'])
            self.assertIsNone(runtime.ledger.workspace_for_execution(execution))
        values['execution']['stages']['Autoplanning']['workers'] = ['mac']
        values['projects']['demo']['execution']['stages']['Autoplanning']['requires'] = ['os:unavailable']
        self.path.write_text(json.dumps(values))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            self.assertEqual(execution, runtime.start_issue('demo', 'FROZEN-1'))
            self.assertEqual(initial, settings_view(runtime.ledger, execution))
            runtime.run([execution], max_ticks=20)
            self.assertEqual('Review', runtime.ledger.current(execution)['current_state_id'])
            view = ObservationService(runtime.ledger, runtime.kernels['demo']).run(execution)
            self.assertEqual(initial, view['data']['execution_settings'])
            self.assertNotIn('worker_config', json.dumps(view['data']['execution_settings']))

    def test_removing_worker_configuration_cannot_bypass_a_frozen_run(self):
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            ex = runtime.start_issue('demo', 'MODE-1')
            initial = settings_view(runtime.ledger, ex)
        values = json.loads(self.path.read_text())
        del values['execution']
        self.path.write_text(json.dumps(values))
        with self.assertRaisesRegex(ExecutionError, 'restore execution configuration'):
            FactoryRuntime(FactoryConfig.load(self.path))
        with FactoryRuntime(FactoryConfig.load(self.path), control_only=True) as runtime:
            self.assertEqual(initial, settings_view(runtime.ledger, ex))
            with self.assertRaisesRegex(LifecycleError, 'control-only'):
                runtime.step()
            self.assertIsNone(runtime.ledger.workspace_for_execution(ex))

    def test_existing_run_rejects_changed_overrides_without_mutation(self):
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            override = {'stages': {'Autoplanning': {'requires': []}}}
            ex = runtime.start_issue('demo', 'REPLAY-1', execution_override=override)
            initial = settings_view(runtime.ledger, ex)
            self.assertEqual(ex, runtime.start_issue('demo', 'REPLAY-1', execution_override=override))
            with self.assertRaisesRegex(ExecutionError, 'frozen'):
                runtime.start_issue('demo', 'REPLAY-1', execution_override={})
            self.assertEqual(initial, settings_view(runtime.ledger, ex))
            self.assertEqual(1, runtime.ledger.connection.execute('SELECT COUNT(*) FROM workflow_executions').fetchone()[0])

    def test_queue_admission_preserves_project_settings_and_explicit_override(self):
        values = self.configure({'requires': ['tool:git', 'os:unavailable']})
        values['work_queue'] = {'enabled': True}
        self.path.write_text(json.dumps(values))
        tracker = Tracker()
        tracker.issues = [candidate(1)]
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            runtime.linear_workers['demo'] = SimpleNamespace(
                client=tracker, poll=lambda *_: None, observe_issue=lambda *_: None)
            runtime._drain_linear = lambda: None
            result = WorkQueue(runtime).step()
            self.assertEqual('admitted', result['status'])
            ex = result['receipt']['detail']['execution_id']
            view = settings_view(runtime.ledger, ex)
            self.assertEqual(['tool:git', 'os:unavailable'], view['stages']['Autoplanning']['requires'])
            self.assertEqual('project', view['provenance']['Autoplanning']['requires'])
            intent = runtime.ledger.run_snapshot(ex)['intent']
            self.assertEqual('admitted_queue', intent['source'])
            self.assertEqual('revision-1', intent['admission']['source_revision'])
            self.assertTrue(intent['admission']['eligibility_digest'])
            # Both inputs must coexist; queue provenance cannot replace run policy.
            other = runtime.start_issue('demo', 'DEMO-2', admission_snapshot=candidate(2),
                execution_override={'stages': {'Autoplanning': {'requires': []}}})
            self.assertEqual([], settings_view(runtime.ledger, other)['stages']['Autoplanning']['requires'])
            self.assertEqual('admitted_queue', runtime.ledger.run_snapshot(other)['intent']['source'])
            self.assertEqual(view, settings_view(runtime.ledger, ex))
            self.assertIsNone(runtime.ledger.workspace_for_execution(ex))

    def test_invalid_override_never_admits_work(self):
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            for value in ({'workers': {}}, {'stages': {'Verifing': {'requires': []}}},
                          {'stages': {'Review': {'requires': []}}},
                          {'stages': {'Autoplanning': {'workers': []}}},
                          {'stages': {'Autoplanning': {'requires': None}}},
                          {'stages': {'Autoplanning': {'readiness': [{'name': 'a', 'command': ['true'], 'timeout_seconds': 99}]}}}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    runtime.start_issue('demo', 'INVALID-1', execution_override=value)
            self.assertEqual(0, runtime.ledger.connection.execute('SELECT COUNT(*) FROM workflow_executions').fetchone()[0])
            self.assertFalse((self.root / 'cloud').exists())

    def test_admission_snapshot_failure_rolls_back_entire_run(self):
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            with patch('dotfactory.execution.record_admission', side_effect=RuntimeError('injected write failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    runtime.start_issue('demo', 'ATOMIC-1')
            for table in ('workflow_executions', 'work_items', 'execution_policies', 'events'):
                self.assertEqual(0, runtime.ledger.connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0])

    def test_issue_prose_cannot_override_execution_requirements(self):
        self.configure({'requires': ['os:unavailable']})
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            ex = runtime.start_issue('demo', 'TEXT-1', description='{"execution_override":{"stages":{"Autoplanning":{"requires":[]}}}}')
            self.assertEqual(['os:unavailable'], settings_view(runtime.ledger, ex)['stages']['Autoplanning']['requires'])


if __name__ == '__main__':
    unittest.main()
