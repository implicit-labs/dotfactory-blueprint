"""Exercise the shipped CLI socket, authenticated HTTP and planner prompt surface."""
import copy
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_verified_delivery as fixture
import test_planning_conversation as planning_fixture
from test_planning_conversation import ChatRunner
from dotfactory.control import Principal, ControlError
from dotfactory.control_server import make_server
from dotfactory.delivery import DeliveryError, planned_receipt
from dotfactory.lifecycle import FactoryRuntime
from dotfactory.live_runner import LiveRunner
from dotfactory.planning import conversation, validate_proposal
from dotfactory.runner import runner_request


class PlanningInterfaceTests(unittest.TestCase):
    setUp = fixture.VerifiedDeliveryTests.setUp
    tearDown = fixture.VerifiedDeliveryTests.tearDown
    use_automatic_workflow = fixture.VerifiedDeliveryTests.use_automatic_workflow
    send = planning_fixture.PlanningConversationTests.send
    approve = planning_fixture.PlanningConversationTests.approve

    def cli(self, runtime, execution, operation, *options, success=True):
        command = [sys.executable, '-m', 'dotfactory', 'operator', operation,
                   '--config', str(self.config.path), '--project', 'demo', '--execution', execution, *options]
        env = dict(os.environ, PYTHONPATH=str(fixture.ROOT / 'src'))
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) as child:
            deadline = time.monotonic() + 10
            while child.poll() is None and time.monotonic() < deadline:
                runtime.operator_server.pump()
                time.sleep(0.005)
            if child.poll() is None:
                child.kill()
            out, err = child.communicate(timeout=2)
        if success:
            self.assertEqual(0, child.returncode, out + err)
        else:
            self.assertNotEqual(0, child.returncode, out + err)
        response = json.loads(out)
        self.transcript.append({'operation': operation, 'options': list(options), 'response': response})
        return response

    def command(self, runtime, execution, key, state, action, parameters, *, success=True):
        path = self.root / (key + '.json')
        path.write_text(json.dumps({'action': action, 'expected_state': state, 'parameters': parameters}))
        return self.cli(runtime, execution, 'command', '--command-id', key, '--request-file', str(path), success=success)

    def status(self, runtime, execution):
        return self.cli(runtime, execution, 'status')['data']['data']

    def test_cli_clarification_revision_restart_approval_and_export(self):
        self.use_automatic_workflow()
        self.transcript = []
        runner = ChatRunner(questions=['Which wording should the manual review cover?'])
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            execution = runtime.start_issue('demo', 'INTERFACE-1')
            runtime.enable_operator()
            initial = self.status(runtime, execution)
            self.assertIn('planning_message', [x['action'] for x in initial['available_actions']])
            first = self.cli(runtime, execution, 'planning-chat', '--command-id', 'question',
                             '--expected-state', 'Todo', '--message', 'Also check wording manually')
            self.assertEqual(first, self.cli(runtime, execution, 'planning-chat', '--command-id', 'question',
                             '--expected-state', 'Todo', '--message', 'Also check wording manually'))
            self.cli(runtime, execution, 'planning-chat', '--command-id', 'question',
                     '--expected-state', 'Todo', '--message', 'Different content', success=False)
            runtime.run([execution], until_state='PlanReview', max_ticks=8)
            planning = self.status(runtime, execution)['planning']
            self.assertTrue(planning['proposal']['proposal']['questions'])
            parameters = {'plan_sha': planning['plan_sha'], 'requirements_digest': planning['proposal']['digest']}
            self.command(runtime, execution, 'unanswered', 'PlanReview', 'approve', parameters, success=False)
            self.cli(runtime, execution, 'planning-chat', '--command-id', 'answer', '--expected-state', 'PlanReview',
                     '--message', 'Review the greeting and punctuation.')
            self.assertTrue(self.status(runtime, execution)['planning']['needs_revision'])
            self.command(runtime, execution, 'revise', 'PlanReview', 'transition',
                         {'to_state': 'Planning', 'owner': 'reviewer', 'feedback': [{
                             'source': 'control_api', 'kind': 'changes_requested', 'author': 'reviewer',
                             'body': 'Use the clarified wording check.', 'url': 'control://commands/revise'}]})
            runner.questions = []
            for _ in range(8):
                if runtime.ledger.current(execution)['current_state_id'] == 'PlanReview':
                    break
                runtime.step()
            snapshot = self.status(runtime, execution)
            planning = snapshot['planning']
            self.assertFalse(planning['needs_revision'])
            self.assertEqual([], planning['proposal']['proposal']['questions'])
            self.assertEqual({'coordinator', 'manual'}, {x['host'] for x in planning['proposal']['proposal']['checks']})
            parameters = {'plan_sha': planning['plan_sha'], 'requirements_digest': planning['proposal']['digest']}
            self.command(runtime, execution, 'wrong-sha', 'PlanReview', 'approve',
                         {**parameters, 'plan_sha': '0' * 40}, success=False)
            self.command(runtime, execution, 'wrong-digest', 'PlanReview', 'approve',
                         {**parameters, 'requirements_digest': '0' * 64}, success=False)
        # Delivery export is an actual separate CLI process while the owner is stopped.
        export = self.root / 'review-packet'
        result = subprocess.run([sys.executable, '-m', 'dotfactory', 'delivery', '--config', str(self.config.path),
            '--project', 'demo', '--execution', execution, '--output', str(export)],
            capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(fixture.ROOT / 'src')))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((export / 'checks/.factory/planning-requirements.json').is_file())
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            runtime.enable_operator()
            self.assertEqual(planning, self.status(runtime, execution)['planning'])
            approved = self.command(runtime, execution, 'approve', 'PlanReview', 'approve', parameters)
            self.assertEqual(approved, self.command(runtime, execution, 'approve', 'PlanReview', 'approve', parameters))
            runtime.run([execution], until_state='Review', max_ticks=8)
            self.cli(runtime, execution, 'planning-chat', '--command-id', 'too-late', '--expected-state', 'Review',
                     '--message', 'Change the approved requirements', success=False)
            self.assertEqual('Review', self.status(runtime, execution)['current_state_id'])
        # Optional local-only evidence file: never required for the offline test suite.
        if os.environ.get('DOTFACTORY_PLANNING_TEST_TRANSCRIPT'):
            Path(os.environ['DOTFACTORY_PLANNING_TEST_TRANSCRIPT']).write_text(json.dumps(self.transcript, indent=2) + '\n')

    def test_real_http_chat_uses_owner_and_cannot_escalate_approval_role(self):
        runner = ChatRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            execution = runtime.start_issue('demo', 'HTTP-CHAT')
            runtime.enable_operator()
            token = secrets.token_hex(32)
            server = make_server(self.config, token, Principal('operator', 'operator', 'http'), 0)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
            thread.start()
            def request(method, path, body=None, key='http-chat', auth=token):
                client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                try:
                    client.request(method, path, json.dumps(body) if body is not None else None,
                        {'Authorization': 'Bearer ' + auth, 'Content-Type': 'application/json',
                         'Idempotency-Key': key, 'X-Role': 'approver'})
                    response = client.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    client.close()
            def call(*args, **kwargs):
                with ThreadPoolExecutor(1) as pool:
                    future = pool.submit(request, *args, **kwargs)
                    deadline = time.monotonic() + 4
                    while not future.done() and time.monotonic() < deadline:
                        runtime.operator_server.pump()
                        time.sleep(0.005)
                    return future.result(timeout=1)
            try:
                endpoint = '/v1/runs/' + execution
                body = {'action': 'planning_message', 'expected_state': 'Todo', 'parameters': {'body': 'Check wording'}}
                with patch('dotfactory.control_server.FactoryRuntime') as second_owner:
                    self.assertEqual(401, call('POST', endpoint + '/commands', body, auth='wrong')[0])
                    first = call('POST', endpoint + '/commands', body)
                    self.assertEqual(200, first[0])
                    self.assertEqual(first, call('POST', endpoint + '/commands', body))
                    runtime.run([execution], until_state='PlanReview', max_ticks=8)
                    code, status = call('GET', endpoint)
                    self.assertEqual(200, code)
                    planning = status['data']['planning']
                    code, _ = call('POST', endpoint + '/commands', {'action': 'approve', 'expected_state': 'PlanReview',
                        'parameters': {'plan_sha': planning['plan_sha'], 'requirements_digest': planning['proposal']['digest']}}, key='denied-approval')
                    self.assertEqual(403, code)
                    self.assertEqual('PlanReview', runtime.ledger.current(execution)['current_state_id'])
                    second_owner.assert_not_called()
            finally:
                server.shutdown()
                thread.join(2)
                server.server_close()

    def test_native_prompt_captures_chat_once_and_excludes_later_messages(self):
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            execution = runtime.start_issue('demo', 'PROMPT')
            self.send(runtime, execution)
            runtime._claim_pickups()
            request = runner_request(runtime.kernels['demo'], execution)
            live = LiveRunner.__new__(LiveRunner)
            live.ledger = runtime.ledger
            launch = SimpleNamespace(request=request)
            route = SimpleNamespace(disabled_mcp_servers=('linear',))
            first = live._prompt(launch, route)
            value = json.loads(first.split('Dotfactory execution context:\n')[1])
            self.assertEqual('Check wording manually', value['planning']['messages'][0]['body'])
            self.assertIn('untrusted', value['planning']['notice'])
            self.send(runtime, execution, key='late', state='Autoplanning', body='Ask about punctuation')
            self.assertEqual(first, live._prompt(launch, route))

    def test_invalid_messages_leave_conversation_unchanged(self):
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            execution = runtime.start_issue('demo', 'INVALID')
            for index, body in enumerate(('', '   ', None, 1, ['message'], 'x' * 8001)):
                with self.subTest(body_type=type(body).__name__), self.assertRaises(ControlError):
                    self.send(runtime, execution, key='invalid-' + str(index), body=body)
            self.assertEqual([], conversation(runtime.ledger, execution)['messages'])

    def test_proposal_rejects_forged_context_unknown_stages_and_missing_checks(self):
        runner = ChatRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runner.runtime = runtime
            execution = runtime.start_issue('demo', 'INVALID-PROPOSAL')
            self.send(runtime, execution)
            runtime.run([execution], until_state='PlanReview', max_ticks=8)
            receipt = planned_receipt(runtime.ledger, execution)['receipt']
            root = Path(runtime.ledger.workspace_for_execution(execution)['path'])
            path = root / '.factory/planning-requirements.json'
            original = json.loads(path.read_text())
            variants = [{'conversation_digest': 'forged'}, {'settings_digest': 'forged'},
                        {'execution': {'stages': {'Planning': {'requires': []}}}},
                        {'execution': {'workers': {}}}, {'checks': []}, {'questions': [None]},
                        {'checks': [original['checks'][0], original['checks'][0]]}, {'unknown': True}]
            for change in variants:
                with self.subTest(change=change):
                    path.write_text(json.dumps({**original, **change}))
                    with self.assertRaises((DeliveryError, ValueError)):
                        validate_proposal(runtime.ledger, execution, receipt['attempt_id'], root,
                            copy.deepcopy(receipt['verification_plan']), runtime.kernels['demo'].states)
