import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotfactory.linear_agent_canary import check_identity, main, run_canary

if __package__:
    from .test_linear_agent import FakeAgentAPI
else:
    from test_linear_agent import FakeAgentAPI


class CanaryAPI(FakeAgentAPI):
    def create_agent_activity(self, **kwargs):
        result = super().create_agent_activity(**kwargs)
        self.sessions[kwargs['session_id']]['status'] = (
            'complete' if kwargs['content']['type'] == 'response' else 'active')
        return result

    def execute(self, operation, query, variables):
        if operation == 'AgentCanaryIdentity':
            return {'viewer': {'id': 'app', 'app': True, 'organization': {'id': 'org'}},
                    'issue': {'id': 'issue', 'project': {'id': 'project'}}}
        session = copy.deepcopy(self.sessions[variables['id']])
        session['activities'] = {'nodes': [{'id': k} for k in self.activities],
                                 'pageInfo': {'hasNextPage': False}}
        return {'agentSession': session}


class AgentCanaryTests(unittest.TestCase):
    def test_live_protocol_fixture_verifies_restart_and_can_be_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = CanaryAPI()
            args = dict(issue_id='issue', project_id='project', marker_url='https://runs.example/1',
                        updated_url='https://runs.example/1/evidence')
            database = Path(directory) / 'canary.db'
            first = run_canary(database, remote, **args)
            counts = (remote.creates, remote.updates, remote.activity_creates)
            second = run_canary(database, remote, **args)
            self.assertEqual('passed', first['status'])
            self.assertEqual(first, second)
            self.assertEqual(counts, (remote.creates, remote.updates, remote.activity_creates))
            self.assertEqual((1, 1, 3), counts)
            self.assertFalse(first['webhook_receipt_verified'])

    def test_identity_mismatch_never_mutates(self):
        remote = CanaryAPI()
        with self.assertRaises(ValueError):
            check_identity(remote, issue_id='issue', project_id='project',
                           organization_id='wrong', app_user_id='app')
        self.assertEqual(0, remote.creates)

    def test_read_only_preflight_checks_receiver_without_mutating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = ['--issue-id', 'issue', '--project-id', 'project', '--organization-id', 'org',
                         '--app-user-id', 'app', '--marker-url', 'https://runs.example/1',
                         '--updated-url', 'https://runs.example/2', '--webhook-url', 'https://receiver.example',
                         '--database', str(root / 'canary.db'), '--receipt', str(root / 'receipt.json')]
            remote = CanaryAPI()
            with patch.dict('os.environ', {'LINEAR_DOTFACTORY_AGENT_TOKEN': 'synthetic-token'}), \
                 patch('dotfactory.linear_agent_canary.LinearGraphQLClient', return_value=remote), \
                 patch('urllib.request.urlopen') as http:
                response = http.return_value.__enter__.return_value
                response.status = 200
                response.read.return_value = b'{"status":"ready","mode":"receipt_only"}'
                self.assertEqual(0, main(arguments))
            self.assertEqual(0, remote.creates)
            self.assertFalse((root / 'canary.db').exists())
            self.assertEqual('ready', json.loads((root / 'receipt.json').read_text())['status'])

    def test_missing_token_reports_blocked_without_database_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = ['--issue-id', 'issue', '--project-id', 'project', '--organization-id', 'org',
                         '--app-user-id', 'app', '--marker-url', 'https://runs.example/1',
                         '--updated-url', 'https://runs.example/2', '--webhook-url', 'https://receiver.example',
                         '--database', str(root / 'canary.db'), '--receipt', str(root / 'receipt.json')]
            with patch.dict('os.environ', {}, clear=True), patch('urllib.request.urlopen') as http:
                self.assertEqual(2, main(arguments))
                http.assert_not_called()
            self.assertFalse((root / 'canary.db').exists())
            report = json.loads((root / 'receipt.json').read_text())
            self.assertEqual('blocked', report['status'])
            self.assertFalse(report['executed'])
            self.assertEqual(0o600, (root / 'receipt.json').stat().st_mode & 0o777)


if __name__ == '__main__':
    unittest.main()
