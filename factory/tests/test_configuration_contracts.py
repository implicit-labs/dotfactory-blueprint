"""Cross-host admission, scoped inspection and frozen configuration regressions."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import test_execution as fixtures
from dotfactory import FactoryConfig, FactoryRuntime
from dotfactory.cli import main
from dotfactory.configuration import preview
from dotfactory.execution import settings_view
from dotfactory.verification_host import inspect, environment


class ConfigurationContractTests(unittest.TestCase):
    setUp = fixtures.WorkerExecutionTests.setUp
    tearDown = fixtures.WorkerExecutionTests.tearDown

    def configure(self, contract):
        values = json.loads(self.path.read_text())
        values['projects']['demo']['execution'] = {'stages': {'Verifying': {'coordinator': contract}}}
        self.path.write_text(json.dumps(values))
        return FactoryConfig.load(self.path)

    def test_unsuitable_coordinator_blocks_before_any_worker_or_model_work(self):
        config = self.configure({'python_min_version': '999.0'})
        with FactoryRuntime(config) as runtime:
            execution = runtime.start_issue('demo', 'COORDINATOR-BLOCKED')
            with patch('dotfactory.execution.call') as remote:
                runtime.run([execution], max_ticks=3)
            remote.assert_not_called()
            self.assertIsNone(runtime.ledger.workspace_for_execution(execution))
            self.assertEqual(0, runtime.ledger.connection.execute('SELECT COUNT(*) FROM runner_runs').fetchone()[0])
            self.assertIn('coordinator verification prerequisites', json.dumps(runtime.ledger.run_snapshot(execution)))

    def test_coordinator_probe_uses_delivery_interpreter_and_environment(self):
        contract = {'python_min_version': '3.9', 'readiness': [{'name': 'isolation', 'command': [
            '{python}', '-I', '-c', 'import os,sys; from pathlib import Path; '
            'assert "CONFIG_TEST_SECRET" not in os.environ; '
            'assert os.environ["PATH"] == os.defpath; '
            'assert Path.home().name.startswith("dotfactory-host-"); '
            'assert sys.flags.isolated == 1']} ]}
        with patch.dict(os.environ, {'CONFIG_TEST_SECRET': 'never-export'}):
            report = inspect(contract, execute=True)
        self.assertTrue(report['available'], report)
        self.assertEqual(sys.executable, report['interpreter'])
        self.assertEqual({'PATH': os.defpath, 'HOME': '/isolated', 'PYTHONDONTWRITEBYTECODE': '1'}, environment('/isolated'))
        self.assertEqual('not_checked', inspect(contract)['readiness'][0]['status'])
        self.assertIsNone(inspect(contract)['available'])

    def test_preview_and_admission_agree_and_restart_freezes_coordinator(self):
        config = self.configure({'python_min_version': '999.0'})
        override = {'stages': {'Verifying': {'coordinator': {}}}}
        before_files = set(self.root.rglob('*'))
        with patch('subprocess.Popen', side_effect=AssertionError('preview executed a command')):
            resolved = preview(config, 'demo', override)
        self.assertEqual(before_files, set(self.root.rglob('*')))
        self.assertEqual('run', resolved['provenance']['Verifying']['coordinator'])
        with FactoryRuntime(config) as runtime:
            execution = runtime.start_issue('demo', 'FROZEN-HOST', execution_override=override)
            frozen = settings_view(runtime.ledger, execution)
            self.assertEqual(resolved['stages'], frozen['stages'])
            self.assertEqual(resolved['provenance'], frozen['provenance'])
        config = self.configure({'requires': ['os:unavailable']})
        with FactoryRuntime(config) as runtime:
            runtime.run([execution], max_ticks=20)
            self.assertEqual('Review', runtime.ledger.current(execution)['current_state_id'])
            self.assertEqual(frozen, settings_view(runtime.ledger, execution))

    def test_scoped_worker_check_uses_project_and_run_readiness(self):
        config = self.configure({})
        values = config.values
        values['projects']['demo']['execution']['stages']['Autoplanning'] = {
            'readiness': [{'name': 'project-probe', 'command': [sys.executable, '-c', 'pass']}]}
        self.path.write_text(json.dumps(values))
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['worker-check', '--config', str(self.path), '--project', 'demo',
                         '--stage', 'Autoplanning', '--worker', 'cloud'])
        self.assertEqual(0, code, out.getvalue())
        self.assertEqual('project-probe', json.loads(out.getvalue())['readiness'][0]['name'])
        override = self.root / 'override.json'
        override.write_text(json.dumps({'stages': {'Autoplanning': {'readiness': []}}}))
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['worker-check', '--config', str(self.path), '--project', 'demo',
                         '--stage', 'Autoplanning', '--worker', 'cloud', '--execution-config', str(override)])
        self.assertEqual(0, code, out.getvalue())
        self.assertEqual([], json.loads(out.getvalue())['readiness'])

    def test_doctor_and_cli_preview_are_read_only_and_scope_overrides(self):
        marker = self.root / 'must-not-exist'
        config = self.configure({'readiness': [{'name': 'not-executed', 'command': [
            sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(marker)) + ').touch()']}]})
        from dotfactory.doctor import inspect as doctor
        before = set(self.root.rglob('*'))
        result = doctor(str(self.path), project='demo')
        check = next(item for item in result['checks'] if item['id'] == 'demo:Verifying:coordinator')
        self.assertEqual('not_checked', check['status'])
        self.assertFalse(marker.exists())
        self.assertEqual(before, set(self.root.rglob('*')))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0, main(['config-preview', '--config', str(self.path), '--project', 'demo']))
        self.assertFalse(json.loads(out.getvalue())['probes_executed'])
        self.assertEqual(result['execution_settings']['demo'], json.loads(out.getvalue()))

    def test_doctor_does_not_require_worker_runner_on_coordinator(self):
        config = self.configure({})
        config.values['runners']['codex']['command'] = '/worker-only/bin/codex'
        config.values['runners']['codex']['environment_envs'] = ['WORKER_ONLY_AUTH']
        self.path.write_text(json.dumps(config.values))
        from dotfactory.doctor import inspect as doctor
        result = doctor(str(self.path), environment={}, project='demo')
        self.assertFalse(any(item['id'].startswith('runner:') for item in result['checks']))
        self.assertTrue(any(item['id'].endswith(':worker') and item['status'] == 'not_checked'
                            for item in result['checks']))

    def test_worker_check_rejects_missing_reports_and_old_runner(self):
        self.configure({})
        values = json.loads(self.path.read_text())
        values['projects']['demo']['execution']['stages']['Autoplanning'] = {
            'readiness': [{'name': 'required', 'command': ['true']}]}
        self.path.write_text(json.dumps(values))
        from dotfactory.execution import ExecutionError, validate_report
        for report in ({'available': True, 'version': 'codex-cli 1.0.0'},
                       {'available': True, 'version': 'codex-cli 0.0.1', 'readiness': [
                           {'name': 'required', 'passed': True, 'exit_code': 0}]}):
            with self.assertRaises(ExecutionError):
                validate_report(report, values['projects']['demo']['execution']['stages']['Autoplanning']['readiness'], '1.0.0')

    def test_unused_instance_stage_cannot_block_selected_workflow(self):
        config = self.configure({})
        config.values['execution']['stages']['OtherWorkflowOnly'] = {
            **config.values['execution']['stages']['Verifying'],
            'coordinator': {'python_min_version': '999.0'}}
        self.path.write_text(json.dumps(config.values))
        config = FactoryConfig.load(self.path)
        self.assertNotIn('OtherWorkflowOnly', preview(config, 'demo')['stages'])
        with FactoryRuntime(config) as runtime:
            execution = runtime.start_issue('demo', 'WORKFLOW-SCOPE')
            self.assertNotIn('OtherWorkflowOnly', settings_view(runtime.ledger, execution)['stages'])
            runtime.run([execution], max_ticks=20)
            self.assertEqual('Review', runtime.ledger.current(execution)['current_state_id'])

    def test_unknown_configuration_fields_fail_before_runtime(self):
        original = json.loads(self.path.read_text())
        for scope in ('instance', 'project'):
            values = json.loads(json.dumps(original))
            target = values if scope == 'instance' else values['projects']['demo']
            target['exectuion'] = {}
            self.path.write_text(json.dumps(values))
            with self.assertRaisesRegex(ValueError, 'unknown fields'):
                FactoryConfig.load(self.path)


if __name__ == '__main__':
    unittest.main()
