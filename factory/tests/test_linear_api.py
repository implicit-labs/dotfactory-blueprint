import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dotfactory import DurableKernel, SQLiteLedger
from dotfactory.instance import FactoryConfig, FactoryConfigError
from dotfactory.linear_api import (
    LinearAPIError, LinearConvergenceWorker, LinearGraphQLClient,
    LinearWebhookVerifier,
)
from dotfactory.linear_reconciliation import LinearStatusBindingV1


ROOT = Path(__file__).resolve().parents[1]


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, endpoint, headers, body, timeout):
        request = json.loads(body)
        self.calls.append({
            "endpoint": endpoint, "headers": dict(headers),
            "request": request, "timeout": timeout,
        })
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def issue(status_id="status-todo", status_name="Todo", updated="remote-1"):
    return {
        "id": "issue-574", "identifier": "TASK-574", "updatedAt": updated,
        "state": {"id": status_id, "name": status_name},
        "team": {"id": "team-implicit"}, "project": {"id": "project-dotfactory"},
    }


class LinearGraphQLTests(unittest.TestCase):
    def test_request_is_bounded_and_graphql_errors_are_generic(self):
        transport = QueueTransport([{
            "errors": [{"message": "token abc leaked", "extensions": {"code": "RATE_LIMITED"}}]
        }])
        client = LinearGraphQLClient("test-auth", timeout_seconds=12, transport=transport)
        with self.assertRaises(LinearAPIError) as raised:
            client.issue("TASK-574")
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("abc", str(raised.exception))
        self.assertEqual("test-auth", transport.calls[0]["headers"]["Authorization"])
        self.assertEqual(12.0, transport.calls[0]["timeout"])

    def test_preflight_resolves_exact_status_ids(self):
        transport = QueueTransport([{"data": {
            "team": {"id": "team-implicit", "states": {"nodes": [
                {"id": "status-todo", "name": "Todo", "type": "unstarted"},
                {"id": "status-ready", "name": "Ready", "type": "unstarted"},
            ]}},
            "project": {"id": "project-dotfactory"},
        }}])
        client = LinearGraphQLClient("test-auth", transport=transport)
        bindings = client.preflight(
            project_key="dotfactory", workflow_digest="a" * 64,
            team_id="team-implicit", project_id="project-dotfactory",
            required_status_names=["Todo", "Ready"],
        )
        self.assertEqual(["status-todo", "status-ready"], [item.status_id for item in bindings])

    def test_preflight_rejects_duplicate_status_names(self):
        transport = QueueTransport([{"data": {
            "team": {"id": "team-implicit", "states": {"nodes": [
                {"id": "one", "name": "Todo", "type": "unstarted"},
                {"id": "two", "name": "Todo", "type": "started"},
            ]}},
            "project": {"id": "project-dotfactory"},
        }}])
        with self.assertRaisesRegex(LinearAPIError, "resolve exactly once"):
            LinearGraphQLClient("test-auth", transport=transport).preflight(
                project_key="dotfactory", workflow_digest="a" * 64,
                team_id="team-implicit", project_id="project-dotfactory",
                required_status_names=["Todo"],
            )

    def test_eligible_issues_are_project_scoped_and_sorted_oldest_first(self):
        transport = QueueTransport([{"data": {"issues": {"nodes": [
            {"id": "two", "identifier": "TASK-2", "title": "Second",
             "createdAt": "2026-08-30T12:01:00Z"},
            {"id": "one", "identifier": "TASK-1", "title": "First",
             "createdAt": "2026-08-30T12:00:00Z"},
        ]}}}])
        client = LinearGraphQLClient("auth", transport=transport)
        issues = client.eligible_issues(
            project_id="project-dotfactory", status_names=["Ready", "Todo"]
        )
        self.assertEqual(["TASK-1", "TASK-2"], [item["identifier"] for item in issues])
        variables = transport.calls[0]["request"]["variables"]
        self.assertEqual("project-dotfactory", variables["projectId"])
        self.assertEqual(["Ready", "Todo"], variables["statusNames"])

    def test_webhook_signature_and_timestamp_are_verified(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        body = json.dumps({
            "webhookTimestamp": int(now.timestamp() * 1000), "type": "Issue"
        }).encode("utf-8")
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        payload = LinearWebhookVerifier("secret").verify(
            body, signature=signature, now=now
        )
        self.assertEqual("Issue", payload["type"])
        with self.assertRaisesRegex(LinearAPIError, "signature"):
            LinearWebhookVerifier("secret").verify(body, signature="bad", now=now)


class LinearWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "factory.db"
        self.ledger = SQLiteLedger(self.path)
        self.ledger.configure_factory("test-local")
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="project-dotfactory",
        )
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")
        self.execution = self.kernel.begin(
            "dotfactory", "TASK-574", {"title": "linear worker"}, command_id="begin"
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def bind(self, name, status_id, status_type="unstarted"):
        current = self.ledger.current(self.execution)
        self.ledger.bind_linear_statuses(
            "dotfactory", current["workflow_digest"], "team-implicit", [
                LinearStatusBindingV1(
                    project_key="dotfactory", workflow_digest=current["workflow_digest"],
                    team_id="team-implicit", status_id=status_id,
                    status_name=name, status_type=status_type,
                )
            ],
        )

    def test_matching_remote_status_confirms_without_write_and_records_observation(self):
        self.bind("Todo", "status-todo")
        transport = QueueTransport([{"data": {"issue": issue()}}])
        worker = LinearConvergenceWorker(
            self.ledger, self.kernel, LinearGraphQLClient("auth", transport=transport),
            self_actor_id="factory-actor",
        )
        result = worker.drain()[0]
        self.assertEqual("confirmed", result["status"])
        self.assertEqual(["FactoryIssue"], [call["request"]["operationName"] for call in transport.calls])
        self.assertEqual("Todo", self.ledger.current(self.execution)["observed_linear_status"])

    def test_signed_webhook_is_deduped_and_rechecked_against_current_issue(self):
        self.bind("Todo", "status-todo")
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        payload = {
            "webhookTimestamp": int(now.timestamp() * 1000),
            "actor": {"id": "human-1"}, "data": issue(),
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        transport = QueueTransport([
            {"data": {"issue": issue()}}, {"data": {"issue": issue()}},
        ])
        worker = LinearConvergenceWorker(
            self.ledger, self.kernel, LinearGraphQLClient("auth", transport=transport),
            self_actor_id="factory-actor",
        )
        for _ in range(2):
            worker.observe_webhook(
                self.execution, body, signature=signature, delivery_id="delivery-1",
                verifier=LinearWebhookVerifier("secret"), now=now,
            )
        count = self.ledger.connection.execute(
            "SELECT COUNT(*) FROM linear_observations WHERE delivery_id='delivery-1'"
        ).fetchone()[0]
        self.assertEqual(1, count)

    def test_self_authored_webhook_confirms_or_corrects_without_transition(self):
        self.bind("Planning", "status-planning", "started")
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        remote = issue("status-planning", "Planning", "remote-2")
        payload = {
            "webhookTimestamp": int(now.timestamp() * 1000),
            "actor": {"id": "factory-actor"}, "data": remote,
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        worker = LinearConvergenceWorker(
            self.ledger, self.kernel,
            LinearGraphQLClient(
                "auth", transport=QueueTransport([{"data": {"issue": remote}}])
            ),
            self_actor_id="factory-actor",
        )
        result = worker.observe_webhook(
            self.execution, body, signature=signature, delivery_id="delivery-self",
            verifier=LinearWebhookVerifier("secret"), now=now,
        )
        self.assertEqual("self_authored", result["disposition"])
        self.assertEqual("Todo", self.ledger.current(self.execution)["current_state_id"])

    def test_webhook_wrong_scope_is_rejected_before_ingestion(self):
        self.bind("Todo", "status-todo")
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        wrong = issue()
        wrong["project"] = {"id": "project-other"}
        payload = {
            "webhookTimestamp": int(now.timestamp() * 1000),
            "actor": {"id": "human-1"}, "data": wrong,
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        worker = LinearConvergenceWorker(
            self.ledger, self.kernel,
            LinearGraphQLClient("auth", transport=QueueTransport([])),
            self_actor_id="factory-actor",
        )
        with self.assertRaisesRegex(LinearAPIError, "different project"):
            worker.observe_webhook(
                self.execution, body, signature=signature,
                delivery_id="delivery-wrong", verifier=LinearWebhookVerifier("secret"),
                now=now,
            )
        self.assertEqual(0, self.ledger.connection.execute(
            "SELECT COUNT(*) FROM linear_observations"
        ).fetchone()[0])

    def test_webhook_requires_authenticated_actor_preflight(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        body = json.dumps({
            "webhookTimestamp": int(now.timestamp() * 1000), "data": issue(),
        }).encode("utf-8")
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        worker = LinearConvergenceWorker(
            self.ledger, self.kernel,
            LinearGraphQLClient("auth", transport=QueueTransport([])),
        )
        with self.assertRaisesRegex(LinearAPIError, "actor preflight"):
            worker.observe_webhook(
                self.execution, body, signature=signature,
                delivery_id="delivery-no-actor",
                verifier=LinearWebhookVerifier("secret"), now=now,
            )

    def test_viewer_identity_can_be_discovered_for_self_authored_filtering(self):
        worker = LinearConvergenceWorker(
            self.ledger, self.kernel,
            LinearGraphQLClient(
                "auth", transport=QueueTransport([{
                    "data": {"viewer": {"id": "factory-actor"}}
                }]),
            ),
        )
        self.assertEqual("factory-actor", worker.discover_self_actor())

    def test_write_uses_read_before_and_read_after_confirmation(self):
        self.bind("Todo", "status-todo")
        initial_transport = QueueTransport([{"data": {"issue": issue()}}])
        LinearConvergenceWorker(
            self.ledger, self.kernel,
            LinearGraphQLClient("auth", transport=initial_transport),
        ).drain()
        self.kernel.transition(
            self.execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner", command_id="claim",
        )
        self.bind("Autoplanning", "status-autoplanning", "started")
        transport = QueueTransport([
            {"data": {"issue": issue()}},
            {"data": {"issueUpdate": {"success": True, "issue": issue(
                "status-autoplanning", "Autoplanning", "remote-2"
            )}}},
            {"data": {"issue": issue(
                "status-autoplanning", "Autoplanning", "remote-2"
            )}},
        ])
        result = LinearConvergenceWorker(
            self.ledger, self.kernel, LinearGraphQLClient("auth", transport=transport)
        ).drain()[0]
        self.assertEqual("confirmed", result["status"])
        self.assertEqual(
            ["FactoryIssue", "FactoryIssueStatusUpdate", "FactoryIssue"],
            [call["request"]["operationName"] for call in transport.calls],
        )
        self.assertEqual(
            "Autoplanning", self.ledger.current(self.execution)["observed_linear_status"]
        )

    def test_transport_ambiguity_is_not_returned_to_pending(self):
        self.bind("Todo", "status-todo")
        transport = QueueTransport([
            {"data": {"issue": issue("status-other", "Other")}},
            LinearAPIError(
                "transport_error", "Linear GraphQL result is unknown",
                retryable=True, ambiguous=True,
            ),
        ])
        worker = LinearConvergenceWorker(
            self.ledger, self.kernel, LinearGraphQLClient("auth", transport=transport)
        )
        with self.assertRaises(LinearAPIError):
            worker.drain()
        self.assertEqual([], self.ledger.pending_linear_mutations())
        status = self.ledger.connection.execute(
            "SELECT status FROM linear_mutations ORDER BY event_seq LIMIT 1"
        ).fetchone()["status"]
        self.assertEqual("ambiguous", status)

    def test_wrong_project_issue_is_rejected_before_reconciliation(self):
        self.bind("Todo", "status-todo")
        wrong = issue()
        wrong["project"] = {"id": "project-other"}
        worker = LinearConvergenceWorker(
            self.ledger, self.kernel,
            LinearGraphQLClient("auth", transport=QueueTransport([])),
        )
        with self.assertRaisesRegex(LinearAPIError, "different project"):
            worker.observe_issue(self.execution, wrong)
        self.assertEqual(0, self.ledger.connection.execute(
            "SELECT COUNT(*) FROM linear_observations"
        ).fetchone()[0])


class LinearConfigTests(unittest.TestCase):
    def test_config_exposes_env_names_without_persisting_the_secret(self):
        config = FactoryConfig.load(ROOT / "factory.example.json")
        projection = config.resolve_linear_projection(environment={})
        self.assertFalse(projection["enabled"])
        self.assertEqual("LINEAR_API_KEY", projection["token_env"])
        self.assertNotIn("authorization", projection)
        with self.assertRaisesRegex(FactoryConfigError, "disabled"):
            config.linear_authorization(environment={"LINEAR_API_KEY": "secret"})


if __name__ == "__main__":
    unittest.main()
