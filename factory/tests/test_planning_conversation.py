"""Real Git/ledger/control approval path; no live model or hosted service."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_verified_delivery as fixture
from dotfactory.cli import _git
from dotfactory.control import Principal, ControlError, ObservationService
from dotfactory.delivery import planned_receipt, DeliveryError
from dotfactory.execution import settings_view
from dotfactory.lifecycle import FactoryRuntime
from dotfactory.planning import context, conversation, effective_settings, validate_proposal
from dotfactory.worker import digest


class ChatRunner(fixture.EditingRunner):
    def __init__(self, runtime=None, questions=None, override=None):
        super().__init__()
        self.runtime = runtime
        self.questions = questions or []
        self.override = override or {'stages': {}}

    def run(self, launch):
        result = super().run(launch)
        if launch.request.state_id in ('Autoplanning', 'Planning'):
            ledger = self.runtime.ledger
            captured = context(ledger, launch.request.execution_id, launch.request.attempt_id)
            root = Path(launch.workspace_path)
            value = {'schema_version': 1, 'conversation_digest': captured['digest'],
                     'settings_digest': captured['settings_digest'], 'execution': self.override,
                     'checks': [{'criterion': 'greeting', 'stage': 'Verifying', 'host': 'coordinator'},
                                {'criterion': 'review', 'stage': 'Verifying', 'host': 'manual'}],
                     'questions': self.questions}
            (root / '.factory/planning-requirements.json').write_text(json.dumps(value))
            _git(root, 'add', '.')
            _git(root, 'commit', '-m', 'typed planning proposal')
        return result


class PlanningConversationTests(unittest.TestCase):
    setUp = fixture.VerifiedDeliveryTests.setUp
    tearDown = fixture.VerifiedDeliveryTests.tearDown
    use_automatic_workflow = fixture.VerifiedDeliveryTests.use_automatic_workflow

    def send(self, runtime, execution, key='chat', body='Check wording manually', state='Todo'):
        return runtime.control_service('demo').execute(execution, command_id=key,
            principal=Principal('reviewer', 'operator', 'test'),
            request={'action': 'planning_message', 'expected_state': state, 'parameters': {'body': body}})

    def approve(self, runtime, execution, key='approve', wrong=False):
        receipt = planned_receipt(runtime.ledger, execution)['receipt']
        return runtime.control_service('demo').execute(execution, command_id=key,
            principal=Principal('reviewer', 'approver', 'test'),
            request={'action': 'approve', 'expected_state': 'PlanReview', 'parameters': {
                'plan_sha': receipt['source']['head_sha'],
                'requirements_digest': 'wrong' if wrong else receipt['planning_requirements']['digest']}})

    def test_chat_review_approval_and_implementation(self):
        self.use_automatic_workflow()
        runner = ChatRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            execution = runtime.start_issue('demo', 'CHAT-1')
            first = self.send(runtime, execution)
            self.assertEqual(first, self.send(runtime, execution))
            runtime.run([execution], until_state='PlanReview', max_ticks=8)
            self.assertEqual('PlanReview', runtime.ledger.current(execution)['current_state_id'])
            self.assertEqual(['Autoplanning'], runner.calls)
            with self.assertRaises(ControlError):
                self.approve(runtime, execution, key='bad', wrong=True)
            self.approve(runtime, execution)
            runtime.run([execution], until_state='Review', max_ticks=8)
            self.assertEqual('Review', runtime.ledger.current(execution)['current_state_id'])
            self.assertEqual(['Autoplanning', 'Implementing', 'Verifying'], runner.calls)

    def test_late_message_and_clarification_block_approval(self):
        for question in (False, True):
            runner = ChatRunner(questions=['Which simulator?'] if question else [])
            with FactoryRuntime(self.config, runner=runner) as runtime:
                runner.runtime = runtime
                execution = runtime.start_issue('demo', 'CHAT-' + str(question))
                self.send(runtime, execution, key='chat-' + str(question))
                runtime.run([execution], until_state='PlanReview', max_ticks=8)
                if not question:
                    self.send(runtime, execution, key='late', state='PlanReview', body='Also check iOS')
                with self.assertRaises(ControlError):
                    self.approve(runtime, execution, key='approve-' + str(question))
                self.assertEqual('PlanReview', runtime.ledger.current(execution)['current_state_id'])

    def test_project_scope_viewer_and_restart_replay(self):
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            execution = runtime.start_issue('demo', 'SCOPED')
            self.send(runtime, execution)
            other = runtime.start_issue('demo', 'OTHER')
            self.assertEqual([], conversation(runtime.ledger, other)['messages'])
            from dotfactory.control import ControlService
            service = ControlService(runtime.ledger, runtime.kernels['demo'], project_key='different')
            with self.assertRaises(ControlError):
                service.execute(execution, command_id='cross-project', principal=Principal('viewer', 'approver', 'test'),
                    request={'action': 'planning_message', 'expected_state': 'Todo', 'parameters': {'body': 'leak'}})
            denied = runtime.control_service('demo').execute(execution, command_id='viewer', principal=Principal('viewer', 'viewer', 'test'),
                    request={'action': 'planning_message', 'expected_state': 'Todo', 'parameters': {'body': 'no'}})
            self.assertEqual('denied', denied['status'])
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            self.send(runtime, execution)
            self.assertEqual(1, len(conversation(runtime.ledger, execution)['messages']))

    def test_approved_amendment_is_durable_and_original_is_unchanged(self):
        runner = ChatRunner(override={'stages': {'Verifying': {'requires': [], 'coordinator': {'python_min_version': '999.0'}}}})
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            execution = runtime.start_issue('demo', 'AMENDMENT')
            # Fixture admission binding; custom runner avoids a live worker/model.
            policy = {'workers': {'local': {'transport': 'local', 'root': '/tmp/worker', 'billing': 'subscription'}},
                      'stages': {s: {'workers': ['local'], 'scope': 'portable', 'requires': ['tool:git'], 'checks': []}
                                 for s in ('Implementing', 'Verifying')}}
            original = {'policy': policy, 'routes': {}, 'provenance': {}, 'run_overrides': {}, 'frozen_at': 'admission'}
            runtime.ledger.connection.execute('CREATE TABLE IF NOT EXISTS execution_policies(execution_id TEXT PRIMARY KEY,policy_json TEXT)')
            runtime.ledger.connection.execute('INSERT INTO execution_policies VALUES (?,?)', (execution, json.dumps(original)))
            runtime.ledger.connection.commit()
            self.send(runtime, execution)
            runtime.run([execution], until_state='PlanReview', max_ticks=8)
            self.assertEqual(original, effective_settings(runtime.ledger, execution))
            view = ObservationService(runtime.ledger, runtime.kernels['demo']).run(execution)['data']['planning']
            self.assertEqual(2, len(view['proposal']['changes']))
            def crash(boundary):
                if boundary == 'after_event_recorded':
                    raise RuntimeError('simulated approval crash')
            runtime.ledger.fault_hook = crash
            with self.assertRaisesRegex(RuntimeError, 'approval crash'):
                self.approve(runtime, execution)
            runtime.ledger.fault_hook = None
            self.assertEqual('PlanReview', runtime.ledger.current(execution)['current_state_id'])
            self.assertEqual(original, effective_settings(runtime.ledger, execution))
            self.approve(runtime, execution)
            self.assertEqual([], settings_view(runtime.ledger, execution)['stages']['Verifying']['requires'])
            self.assertEqual(original, json.loads(runtime.ledger.connection.execute('SELECT policy_json FROM execution_policies WHERE execution_id=?', (execution,)).fetchone()[0]))
            ledger_path = runtime.ledger.path
            runtime.run([execution], max_ticks=5)
            self.assertNotEqual('Review', runtime.ledger.current(execution)['current_state_id'])
            # Actual coordinator prerequisite rejection prevents verification success.
            rows = runtime.ledger.connection.execute("SELECT payload_json FROM events WHERE execution_id=? AND event_type='delivery_checked'", (execution,)).fetchall()
            self.assertTrue(any('coordinator' in row[0] and '999.0' in row[0] for row in rows))
        from dotfactory.ledger import SQLiteLedger
        from dotfactory.execution import ExecutionManager
        from types import SimpleNamespace
        ledger = SQLiteLedger(ledger_path)
        try:
            self.assertEqual('plan-approval', settings_view(ledger, execution)['frozen_at'])
            manager = ExecutionManager.__new__(ExecutionManager)
            manager.ledger = ledger
            binding = manager._policy(SimpleNamespace(execution_id=execution, state_id="Verifying"))
            self.assertEqual('999.0', binding['policy']['stages']['Verifying']['coordinator']['python_min_version'])
            self.assertEqual(original, manager._policy(SimpleNamespace(execution_id=execution, state_id='Planning')))
        finally:
            ledger.close()

    def test_later_request_requires_new_plan_approval(self):
        runner = ChatRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            execution = runtime.start_issue('demo', 'REVISION')
            self.send(runtime, execution)
            runtime.run([execution], until_state='PlanReview', max_ticks=8)
            self.approve(runtime, execution)
            self.send(runtime, execution, key='new-check', state='Ready', body='Also review punctuation')
            from dotfactory.delivery import approved_plan
            with self.assertRaisesRegex(DeliveryError, 'new planning messages'):
                approved_plan(runtime.ledger, execution)
            runtime.control_service('demo').execute(execution, command_id='revise',
                principal=Principal('reviewer', 'approver', 'test'), request={
                    'action': 'transition', 'expected_state': 'Ready',
                    'parameters': {'to_state': 'Planning', 'owner': 'reviewer'}})
            for _ in range(8):
                if runtime.ledger.current(execution)['current_state_id'] == 'PlanReview':
                    break
                runtime.step()
            self.approve(runtime, execution, key='approve-revision')
            runtime.run([execution], until_state='Review', max_ticks=8)
            self.assertEqual('Review', runtime.ledger.current(execution)['current_state_id'])

    def test_captured_context_is_immutable_and_invalid_hosts_fail_closed(self):
        runner = ChatRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            execution = runtime.start_issue('demo', 'CONTEXT')
            self.send(runtime, execution)
            runtime.run([execution], until_state='PlanReview', max_ticks=8)
            receipt = planned_receipt(runtime.ledger, execution)['receipt']
            captured = context(runtime.ledger, execution, receipt['attempt_id'])
            self.send(runtime, execution, key='follow-up', state='PlanReview', body='Use a cloud verifier')
            self.assertEqual(captured, context(runtime.ledger, execution, receipt['attempt_id']))
            root = Path(runtime.ledger.workspace_for_execution(execution)['path'])
            file = root / '.factory/planning-requirements.json'
            proposal = json.loads(file.read_text())
            proposal['checks'][0]['host'] = 'cloud'
            file.write_text(json.dumps(proposal))
            with self.assertRaisesRegex(DeliveryError, 'coordinator'):
                validate_proposal(runtime.ledger, execution, receipt['attempt_id'], root,
                                  receipt['verification_plan'], runtime.kernels['demo'].states)
