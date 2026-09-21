"""Native Linear UI adapter against real Git, durable ledger, control and delivery gates."""
import copy
import json
import threading
import time
import queue
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dotfactory.instance import FactoryConfig
from dotfactory.lifecycle import FactoryRuntime
from dotfactory.linear_agent import LinearAgentSessionWorker
from dotfactory.linear_api import LinearGraphQLClient, LinearAPIError
from dotfactory.linear_planning import _async_read, activities, discover, events, paused, poll, present, receive, validate, plan_preview
from dotfactory.planning import conversation
from dotfactory.ledger import LedgerError
from test_linear_agent import FakeAgentAPI, QueueTransport
from test_planning_conversation import ChatRunner
import test_verified_delivery as fixture
ROOT = fixture.ROOT

POLICY = {'enabled': True, 'organization_id': 'org', 'app_user_id': 'app',
          'participants': {'reviewer': 'approver', 'collaborator': 'operator'}, 'quiet_seconds': 0,
          'reviewer_urls': ['https://linear' + '.app/example/profiles/reviewer']}


class LinearPlanningTests(unittest.TestCase):
    setUp = fixture.VerifiedDeliveryTests.setUp
    tearDown = fixture.VerifiedDeliveryTests.tearDown

    def test_preview_preserves_plan_and_falls_back_for_delimiters(self):
        plan = '# Navigation\n\nAnimate the selected pill.\n\n## Scope\n\nCompact dock only.'
        rendered = plan_preview(plan)
        self.assertTrue(rendered.startswith('Animate the selected pill.\n\n+++'))
        self.assertIn('\n\n' + plan + '\n\n+++', rendered)
        for delimiter in ('+++', '  +++ Nested', '++++'):
            unsafe = plan + '\n' + delimiter + '\nExtra details'
            self.assertEqual('**Full plan and scope (Markdown)**\n\n```\n' + unsafe + '\n```', plan_preview(unsafe))
        for fence in ('```python', '~~~~'):
            malformed = plan + '\n\n' + fence + '\nunfinished'
            self.assertIn('(Markdown)**', plan_preview(malformed))
            self.assertNotIn('+++ Full plan', plan_preview(malformed))
        balanced = plan + '\n\n```python\nprint(1)\n```'
        self.assertIn('+++ Full plan', plan_preview(balanced))
        long_plan = '# Scope\n\n' + 'A' * 401
        self.assertTrue(plan_preview(long_plan).startswith('Expand the full plan'))
        self.assertIn(long_plan, plan_preview(long_plan))


    def test_product_review_discloses_details_without_extra_permission_gate(self):
        runtime, runner = self.configured()
        with runtime:
            execution = self.setup_run(runtime, runner)
            runtime.run([execution], until_state='PlanReview')
            from dotfactory.planning import view
            value = view(runtime.ledger, execution)
            value['plan_markdown'] = ('# Navigation\n\nExpand the selected navigation pill.\n\n'
                '## Verification summary\n\nCheck motion and keyboard navigation; capture before/after recordings.\n\n'
                '## Setup\n\nInstall the browser fixture before implementation.')
            value['verification_summary'] = 'Chromium at 390x844; trusted fixture command; recordings required.'
            value['proposal']['changes'] = [{'stage': 'Verifying', 'field': 'requires',
                'before': [], 'after': ['browser']}]
            with patch('dotfactory.linear_planning.view', return_value=value):
                review = present(runtime.ledger, execution, POLICY)
            body = review['body']
            self.assertFalse(review['questions'])
            self.assertIn('ready for your review', body)
            self.assertIn('**Verification**\n\nCheck motion', body)
            self.assertIn('+++ Verification requirements and evidence\n\nChromium', body)
            self.assertIn('+++ Changes to project defaults', body)
            self.assertNotIn('**Decisions needed**', body)
            self.assertIn('Reply **approve plan v1**', body.rsplit('+++', 1)[1])
            # Re-rendering cannot rewrite the already published exact revision.
            self.assertEqual(review, present(runtime.ledger, execution, POLICY))

    def test_technical_details_cannot_swallow_review_actions(self):
        rendered = plan_preview('fixture\n+++ nested\ncontent',
            title='Verification requirements and evidence', include_preview=False)
        self.assertIn('(Markdown)', rendered)
        self.assertNotIn('+++ Verification', rendered)

    def setup_run(self, runtime, runner):
        runtime.config.values['projects']['demo']['linear_planning'] = POLICY
        execution = runtime.kernels['demo'].begin('demo', 'LINEAR-1', {
            'title': 'Navigation motion', 'linear_issue_id': 'issue', 'linear_planning': POLICY},
            command_id='linear-fixture', adopted_state='PlanQueued')
        runner.runtime = runtime
        self.api = FakeAgentAPI()
        self.agent = LinearAgentSessionWorker(runtime.ledger, self.api)
        runtime.linear_agent_workers['demo'] = self.agent
        self.api.planning_session = lambda _: self.session(runtime, execution, [])
        runtime.linear_workers['demo'] = SimpleNamespace(poll=lambda *a: None, client=SimpleNamespace(queue_issues=lambda **k: []))
        runtime._drain_linear = lambda: None
        self.agent.sync(execution, issue_id='issue', marker_url='https://example.test/run',
                        external_urls=[{'label': 'Run', 'url': 'https://example.test/run'}], activities=[])
        return execution

    def configured(self, questions=None):
        values = copy.deepcopy(self.config.values)
        values['workflows']['default']['path'] = str(ROOT / 'workflows/linear-planning.dot')
        path = self.root / 'linear.json'
        path.write_text(json.dumps(values))
        runner = ChatRunner(questions=questions)
        runtime = FactoryRuntime(FactoryConfig.load(path), runner=runner)
        return runtime, runner

    def session(self, runtime, execution, replies):
        project = runtime.config.resolve_project('demo', environment=runtime.environment)
        return {'id': 'session-1', 'appUser': {'id': 'app'}, 'organization': {'id': 'org'},
                'issue': {'id': 'issue', 'project': {'id': project['tracker_project_id']},
                          'team': {'id': project.get('tracker_team_id')}}, 'activities': replies}

    def reply(self, identity, body, author='reviewer', **extra):
        return {'id': identity, 'content': {'type': 'prompt', 'body': body}, 'user': {'id': author},
                'createdAt': (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), 'queued': False, **extra}

    def publish(self, runtime, execution):
        value = present(runtime.ledger, execution, POLICY)
        self.agent.sync(execution, issue_id='issue', marker_url='https://example.test/run',
                        external_urls=[{'label': 'Run', 'url': 'https://example.test/run'}],
                        activities=activities(runtime, execution, []))
        return value

    def test_question_reply_replans_same_run_then_exact_approval(self):
        runtime, runner = self.configured(['Compact dock only, or desktop too? I recommend the dock to keep this change focused.'])
        with runtime:
            execution = self.setup_run(runtime, runner)
            runtime.run([execution], until_state='PlanReview')
            first = self.publish(runtime, execution)
            self.assertEqual(1, first['revision'])
            self.assertIn('Compact dock', first['body'])
            self.assertIn('+++ Full plan and scope', first['body'])
            self.assertIn('**Decisions needed**', first['body'].rsplit('+++', 1)[1])
            self.assertEqual(first, present(runtime.ledger, execution, POLICY))
            receive(runtime, execution, self.session(runtime, execution, [self.reply('answer', 'Compact dock only')]))
            self.assertEqual('Planning', runtime.ledger.current(execution)['current_state_id'])
            runner.questions = []
            runtime.target_state = None
            runtime.step()
            self.assertEqual('PlanReview', runtime.ledger.current(execution)['current_state_id'])
            second = self.publish(runtime, execution)
            self.assertEqual(2, second['revision'])
            receive(runtime, execution, self.session(runtime, execution, [self.reply('stale', 'approve plan v1')]))
            self.assertEqual('PlanReview', runtime.ledger.current(execution)['current_state_id'])
            reply = self.reply('approve', 'approve plan v2')
            receive(runtime, execution, self.session(runtime, execution, [reply]))
            self.assertEqual('Ready', runtime.ledger.current(execution)['current_state_id'])
            receive(runtime, execution, self.session(runtime, execution, [reply]))
            self.assertEqual(1, len(conversation(runtime.ledger, execution)['messages']))
            runtime.run([execution], until_state='Review')
            self.assertEqual(['Planning', 'Planning', 'Implementing', 'Verifying'], runner.calls)

    def test_wrong_actor_ambiguous_approval_and_replay(self):
        runtime, runner = self.configured()
        with runtime:
            execution = self.setup_run(runtime, runner)
            runtime.run([execution], until_state='PlanReview')
            self.publish(runtime, execution)
            for identity, body, author in [('outsider', 'approve plan v1', 'outsider'),
                                           ('operator', 'approve plan v1', 'collaborator'),
                                           ('ambiguous', 'approve it', 'reviewer')]:
                receive(runtime, execution, self.session(runtime, execution, [self.reply(identity, body, author)]))
                self.assertEqual('PlanReview', runtime.ledger.current(execution)['current_state_id'])
            answer = self.reply('yes', 'yes')
            receive(runtime, execution, self.session(runtime, execution, [answer, answer]))
            self.assertEqual('Planning', runtime.ledger.current(execution)['current_state_id'])
            self.assertEqual(1, len(conversation(runtime.ledger, execution)['messages']))

    def test_session_scope_and_unpublished_approval(self):
        runtime, runner = self.configured()
        with runtime:
            execution = self.setup_run(runtime, runner)
            runtime.run([execution], until_state='PlanReview')
            present(runtime.ledger, execution, POLICY)
            session = self.session(runtime, execution, [self.reply('early', 'approve plan v1')])
            receive(runtime, execution, session)
            self.assertEqual('PlanReview', runtime.ledger.current(execution)['current_state_id'])
            for field in ('id', 'issue', 'appUser', 'organization'):
                wrong = copy.deepcopy(session)
                wrong[field] = 'other' if field == 'id' else {'id': 'other'}
                with self.assertRaises(LedgerError):
                    receive(runtime, execution, wrong)

    def test_pause_resume_stop_and_frozen_authority(self):
        runtime, runner = self.configured()
        with runtime:
            execution = self.setup_run(runtime, runner)
            receive(runtime, execution, self.session(runtime, execution, [self.reply('pause', 'pause')]))
            self.assertTrue(paused(runtime.ledger, execution))
            runtime.run([execution], max_ticks=2)
            self.assertEqual([], runner.calls)
            receive(runtime, execution, self.session(runtime, execution, [self.reply('resume', 'resume')]))
            runtime.ledger.connection.execute("UPDATE scheduler_dispatches SET available_at=NULL WHERE execution_id=?", (execution,))
            runtime.ledger.connection.commit()
            runtime.run([execution], until_state='PlanReview')
            self.assertEqual(['Planning'], runner.calls)
            receive(runtime, execution, self.session(runtime, execution, [self.reply('stop', 'stop')]))
            self.assertEqual('Canceled', runtime.ledger.current(execution)['current_state_id'])

    def test_late_answer_invalidates_approval_in_same_snapshot(self):
        runtime, runner = self.configured()
        with runtime:
            execution = self.setup_run(runtime, runner)
            runtime.run([execution], until_state='PlanReview')
            self.publish(runtime, execution)
            answer = self.reply('answer', 'Also check keyboard navigation')
            approval = self.reply('approval', 'approve plan v1')
            receive(runtime, execution, self.session(runtime, execution, [approval, answer]))
            self.assertNotEqual('Ready', runtime.ledger.current(execution)['current_state_id'])
            self.assertEqual('rejected', events(runtime.ledger, execution, 'linear_planning_reply')[-1]['status'])

    def test_display_is_immutable_and_activity_is_sent_once(self):
        runtime, runner = self.configured()
        with runtime:
            execution = self.setup_run(runtime, runner)
            runtime.run([execution], until_state='PlanReview')
            first = self.publish(runtime, execution)
            count = self.api.activity_creates
            root = Path(runtime.ledger.workspace_for_execution(execution)['path'])
            (root / '.factory/plan.md').write_text('unreviewed mutation')
            self.assertEqual(first, self.publish(runtime, execution))
            self.assertEqual(count, self.api.activity_creates)
            self.assertNotIn('unreviewed mutation', first['body'])

    def test_policy_validation(self):
        validate(POLICY)
        for change in ({'participants': {'a': 'operator'}}, {'quiet_seconds': -1}, {'reviewer_urls': ['https://evil.test']}, {'app_user_id': ''}):
            with self.assertRaises(ValueError):
                validate(dict(POLICY, **change))

    def test_approval_recovers_after_crash_before_reply_receipt(self):
        import dotfactory.linear_planning as adapter
        runtime, runner = self.configured()
        config = runtime.config
        with runtime:
            execution = self.setup_run(runtime, runner)
            runtime.run([execution], until_state='PlanReview')
            self.publish(runtime, execution)
            response = self.reply('approve-crash', 'approve plan v1')
            real_event = adapter.event
            def crash(ledger, execution, kind, key, payload):
                if kind == 'linear_planning_reply':
                    raise RuntimeError('simulated crash after approval')
                return real_event(ledger, execution, kind, key, payload)
            with patch.object(adapter, 'event', side_effect=crash):
                with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                    receive(runtime, execution, self.session(runtime, execution, [response]))
            self.assertEqual('Ready', runtime.ledger.current(execution)['current_state_id'])
        with FactoryRuntime(config, runner=ChatRunner()) as restarted:
            receive(restarted, execution, self.session(restarted, execution, [response]))
            self.assertEqual('Ready', restarted.ledger.current(execution)['current_state_id'])
            self.assertEqual('accepted', events(restarted.ledger, execution, 'linear_planning_reply')[-1]['status'])
            count = restarted.ledger.connection.execute("SELECT COUNT(*) FROM control_commands WHERE action='approve'").fetchone()[0]
            self.assertEqual(1, count)

    def test_queued_prompt_waits_and_native_stop_cancels(self):
        runtime, runner = self.configured()
        with runtime:
            execution = self.setup_run(runtime, runner)
            receive(runtime, execution, self.session(runtime, execution, [self.reply('queued', 'Change scope', queued=True)]))
            self.assertEqual([], conversation(runtime.ledger, execution)['messages'])
            receive(runtime, execution, self.session(runtime, execution, [self.reply('native-stop', '', signal='stop')]))
            self.assertEqual('Canceled', runtime.ledger.current(execution)['current_state_id'])

    @patch('dotfactory.linear_planning._async_read', side_effect=lambda runtime, key, read, consume: consume(read()))
    def test_discovery_scopes_and_rereads_before_admission(self, _read):
        runtime, runner = self.configured()
        with runtime:
            runtime.config.values['projects']['demo']['linear_planning'] = POLICY
            project = runtime.config.resolve_project('demo', environment=runtime.environment)
            issue = {'id': 'issue', 'identifier': 'LINEAR-NEW', 'description': '',
                     'project': {'id': project['tracker_project_id']}, 'team': {'id': project.get('tracker_team_id')},
                     'state': {'name': 'Planning'}, 'labels': {'nodes': [], 'pageInfo': {'hasNextPage': False}},
                     'inverseRelations': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}
            calls = []
            self.api = FakeAgentAPI()
            runtime.linear_agent_workers['demo'] = LinearAgentSessionWorker(runtime.ledger, self.api)
            runtime.linear_workers['demo'] = SimpleNamespace(observe_issue=lambda *a: None,
                client=SimpleNamespace(queue_issues=lambda **k: [issue], queue_issue=lambda _: dict(issue, state={'name': 'Todo'})))
            discover(runtime)
            self.assertEqual([], runtime._owned_runs())
            runtime.linear_workers['demo'].client.queue_issue = lambda _: issue
            discover(runtime)
            discover(runtime)
            self.assertEqual(1, len(runtime._owned_runs()))
            self.assertEqual('PlanQueued', runtime.ledger.current(runtime._owned_runs()[0]['id'])['current_state_id'])
            runtime.config.values['projects']['demo']['linear_planning'] = {'enabled': False}
            runtime.linear_workers['demo'].client.queue_issues = lambda **k: self.fail('disabled project was queried')
            discover(runtime)

    def test_plan_approval_never_approves_code_review(self):
        runtime, runner = self.configured()
        with runtime:
            execution = self.setup_run(runtime, runner)
            runtime.run([execution], until_state='PlanReview')
            self.publish(runtime, execution)
            receive(runtime, execution, self.session(runtime, execution, [self.reply('approve', 'approve plan v1')]))
            runtime.run([execution], until_state='Review')
            receive(runtime, execution, self.session(runtime, execution, [self.reply('approve-again', 'approve plan v1')]))
            self.assertEqual('Review', runtime.ledger.current(execution)['current_state_id'])
            original = [{'semantic_key': 'human-input:review', 'content': {'type': 'elicitation', 'body': 'Review implementation'}}]
            self.assertIn(original[0], activities(runtime, execution, original))

    def test_active_stop_and_pending_answers(self):
        runtime, runner = self.configured()
        with runtime:
            execution = self.setup_run(runtime, runner)
            runtime._claim_pickups()
            answer = self.reply('during-planning', 'Only the selected page should expand')
            receive(runtime, execution, self.session(runtime, execution, [answer]))
            self.assertEqual([], conversation(runtime.ledger, execution)['messages'])
            receive(runtime, execution, self.session(runtime, execution, [self.reply('active-stop', '', signal='stop')]))
            self.assertEqual('Canceled', runtime.ledger.current(execution)['current_state_id'])
            self.assertEqual([], runner.calls)

    @patch('dotfactory.linear_planning._async_read', side_effect=lambda runtime, key, read, consume, **kw: consume(read()))
    def test_scoped_network_and_identity_diagnostics(self, _read):
        runtime, runner = self.configured()
        with runtime:
            execution = self.setup_run(runtime, runner)
            def fail(**kw):
                raise LinearAPIError('HTTP_503', 'Temporarily unavailable', retryable=True)
            runtime.linear_workers['demo'].client.queue_issues = fail
            discover(runtime)
            self.assertEqual('PlanQueued', runtime.ledger.current(execution)['current_state_id'])
            runtime.config.values['projects']['demo']['linear_planning'] = dict(POLICY, organization_id='other')
            poll(runtime)
            self.assertTrue(runtime.ledger.operating_receipts('demo'))


class AsyncPlanningReadTests(unittest.TestCase):
    def test_blocked_read_does_not_block_owner_or_launch_another_read(self):
        runtime = SimpleNamespace()
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        received = []
        def read():
            entered.set()
            release.wait(3)
            done.set()
            return 'reply'
        _async_read(runtime, 'first', read, received.append)
        self.assertTrue(entered.wait(1))
        _async_read(runtime, 'second', lambda: self.fail('concurrent read'), received.append)
        self.assertEqual([], received)
        release.set()
        self.assertTrue(done.wait(1))
        # Wait on the result queue itself, not on timing assumptions about thread exit.
        value = runtime._planning_reads['first']['results'].get(timeout=1)
        runtime._planning_reads['first']['results'].put(value)
        _async_read(runtime, 'first', lambda: self.fail('read was not throttled'), received.append)
        self.assertEqual(['reply'], received)
        _async_read(runtime, 'first', lambda: self.fail('completion cooldown was ignored'), received.append)

    def test_slow_read_yields_slot_to_next_scope_after_completion(self):
        runtime = SimpleNamespace(_planning_reads={})
        results = queue.Queue()
        results.put(('done', None, time.monotonic()))
        runtime._planning_reads['slow'] = {'inflight': True, 'last': 0, 'results': results}
        received, second = [], threading.Event()
        _async_read(runtime, 'slow', lambda: self.fail('slow scope reacquired the slot'), received.append)
        _async_read(runtime, 'next', lambda: second.set(), received.append)
        self.assertTrue(second.wait(1))
        self.assertEqual(['done'], received)

    def test_old_background_snapshot_is_refetched_before_admission(self):
        runtime = SimpleNamespace(_planning_reads={})
        results = queue.Queue()
        results.put(('old candidate', None, time.monotonic() - 60))
        runtime._planning_reads['discovery:demo'] = {'inflight': True, 'last': 0, 'results': results}
        _async_read(runtime, 'discovery:demo', lambda: None, lambda value: self.fail('stale snapshot was admitted'))
        self.assertIsNone(runtime._planning_reads['discovery:demo']['last'])


class PlanningAPITests(unittest.TestCase):
    def page(self, nodes, more=False, cursor=None):
        return {'data': {'organization': {'id': 'org'}, 'agentSession': {'id': 's', 'appUser': {'id': 'app'},
                'issue': {'id': 'issue'}, 'activities': {'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}

    def test_all_pages_are_collected_before_return(self):
        transport = QueueTransport([self.page([{'id': 'b', 'createdAt': '2026-09-20T12:00:00Z'}], True, 'next'),
                                    self.page([{'id': 'a', 'createdAt': '2026-09-20T11:00:00Z'}])])
        client = LinearGraphQLClient('test-token', transport=transport)
        result = client.planning_session('s')
        self.assertEqual(2, len(result['activities']))
        self.assertEqual('next', transport.calls[1]['variables']['after'])

    def test_incomplete_or_changing_identity_fails_closed(self):
        broken = self.page([], True, 'same')
        changed = self.page([])
        changed['data']['agentSession']['appUser']['id'] = 'other'
        for pages in ([broken, broken], [self.page([], True, 'next'), changed]):
            client = LinearGraphQLClient('test-token', transport=QueueTransport(pages))
            with self.assertRaises(LinearAPIError):
                client.planning_session('s')
