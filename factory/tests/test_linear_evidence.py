import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dotfactory import DurableKernel, ObservationService, SQLiteLedger
from dotfactory.linear_api import LinearAPIError, LinearGraphQLClient
from dotfactory.linear_evidence import (
    LinearEvidenceWorker, readable_incidents, render_linear_run_summary,
)
from dotfactory.lifecycle import FactoryRuntime


ROOT = Path(__file__).resolve().parents[1]


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, endpoint, headers, body, timeout):
        self.calls.append(json.loads(body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeComments:
    def __init__(self):
        self.comments = {}
        self.creates = 0
        self.updates = 0
        self.ambiguous_create = False
        self.ambiguous_update = False
        self.comment_error = None
        self.create_error = None

    def comment(self, comment_id):
        if self.comment_error:
            raise self.comment_error
        item = self.comments.get(comment_id)
        return dict(item) if item else None

    def create_comment(self, *, issue_id, comment_id, body):
        self.creates += 1
        if self.create_error:
            raise self.create_error
        if comment_id in self.comments:
            raise LinearAPIError(
                "ALREADY_EXISTS", "duplicate", retryable=False
            )
        item = {
            "id": comment_id, "body": body,
            "url": f"https://example.com/comment/{comment_id}",
            "issue": {"id": issue_id},
        }
        self.comments[comment_id] = item
        if self.ambiguous_create:
            self.ambiguous_create = False
            raise LinearAPIError(
                "transport_error", "unknown", retryable=True, ambiguous=True
            )
        return dict(item)

    def update_comment(self, *, comment_id, body):
        self.updates += 1
        if comment_id not in self.comments:
            raise LinearAPIError(
                "ENTITY_NOT_FOUND", "missing", retryable=False
            )
        self.comments[comment_id]["body"] = body
        if self.ambiguous_update:
            self.ambiguous_update = False
            raise LinearAPIError(
                "transport_error", "unknown", retryable=True, ambiguous=True
            )
        return dict(self.comments[comment_id])


class LinearEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "factory.db"
        self.ledger = SQLiteLedger(self.path)
        self.ledger.configure_factory("linear-evidence-test")
        self.ledger.register_project(
            "example-service", display_name="Example service", tracker_kind="linear",
            tracker_project_id="project-example",
        )
        self.kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "default.dot"
        )
        self.execution = self.kernel.begin(
            "example-service", "TASK-612",
            {"title": "evidence", "linear_issue_id": "issue-example"},
            command_id="begin-evidence",
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def stage(self, body="first"):
        return self.ledger.stage_linear_evidence(
            self.execution, issue_id="issue-example", body=body,
            digest=hashlib.sha256(body.encode()).hexdigest(),
        )

    def test_client_uses_supplied_id_and_updates_exact_comment(self):
        create = {"data": {"commentCreate": {
            "success": True, "comment": {
                "id": "comment-1", "body": "first", "url": "https://linear/1",
            },
        }}}
        update = {"data": {"commentUpdate": {
            "success": True, "comment": {
                "id": "comment-1", "body": "second", "url": "https://linear/1",
            },
        }}}
        transport = QueueTransport([create, update])
        client = LinearGraphQLClient("auth", transport=transport)
        client.create_comment(
            issue_id="issue-example", comment_id="comment-1", body="first"
        )
        client.update_comment(comment_id="comment-1", body="second")
        self.assertEqual("comment-1", transport.calls[0]["variables"]["input"]["id"])
        self.assertEqual("comment-1", transport.calls[1]["variables"]["id"])

    def test_client_distinguishes_rate_limit_from_ambiguous_server_failure(self):
        rate_transport = QueueTransport([
            LinearAPIError("RATELIMITED", "limited", retryable=True),
        ])
        with self.assertRaises(LinearAPIError) as rate:
            LinearGraphQLClient("auth", transport=rate_transport).create_comment(
                issue_id="issue", comment_id="comment", body="body"
            )
        self.assertFalse(rate.exception.ambiguous)
        server_transport = QueueTransport([
            LinearAPIError("INTERNAL_SERVER_ERROR", "failed", retryable=True),
        ])
        with self.assertRaises(LinearAPIError) as server:
            LinearGraphQLClient("auth", transport=server_transport).create_comment(
                issue_id="issue", comment_id="comment", body="body"
            )
        self.assertTrue(server.exception.ambiguous)

    def test_stage_persists_uuid4_and_same_digest_is_noop(self):
        first = self.stage()
        second = self.stage()
        self.assertEqual(first["comment_id"], second["comment_id"])
        self.assertEqual("4", first["comment_id"].split("-")[2][0])
        self.assertEqual("pending", second["status"])
        visible = ObservationService(self.ledger, self.kernel).run(
            self.execution
        )["data"]["linear_evidence"]
        self.assertEqual(first["comment_id"], visible["comment_id"])

    def test_create_then_update_same_comment(self):
        remote = FakeComments()
        worker = LinearEvidenceWorker(self.ledger, remote)
        first = self.stage()
        self.assertEqual("confirmed", worker.drain_one(first))
        changed = self.stage("second")
        self.assertEqual("confirmed", worker.drain_one(changed))
        evidence = self.ledger.linear_evidence(self.execution)
        self.assertEqual(first["comment_id"], evidence["comment_id"])
        self.assertEqual((1, 1), (remote.creates, remote.updates))

    def test_lifecycle_sync_creates_then_updates_one_comment(self):
        remote = FakeComments()
        runtime = object.__new__(FactoryRuntime)
        runtime.ledger = self.ledger
        runtime.kernels = {"example-service": self.kernel}
        runtime.linear_evidence_workers = {
            "example-service": LinearEvidenceWorker(self.ledger, remote),
        }
        runtime._sync_linear_evidence()
        first_id = self.ledger.linear_evidence(self.execution)["comment_id"]
        self.kernel.transition(
            self.execution, "Planning", actor="human",
            signal="linear_status_change", owner="planner",
            command_id="plan-evidence",
        )
        runtime._sync_linear_evidence()
        evidence = self.ledger.linear_evidence(self.execution)
        self.assertEqual("confirmed", evidence["status"])
        self.assertEqual(first_id, evidence["comment_id"])
        self.assertEqual((1, 1), (remote.creates, remote.updates))

    def test_ambiguous_create_reconciles_after_restart_without_duplicate(self):
        remote = FakeComments()
        remote.ambiguous_create = True
        worker = LinearEvidenceWorker(self.ledger, remote)
        self.assertEqual("ambiguous", worker.drain_one(self.stage()))
        comment_id = self.ledger.linear_evidence(self.execution)["comment_id"]
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        restarted = LinearEvidenceWorker(self.ledger, remote)
        item = self.ledger.linear_evidence(self.execution)
        self.assertEqual("confirmed", restarted.drain_one(item))
        self.assertEqual(1, remote.creates)
        self.assertEqual(comment_id, self.ledger.linear_evidence(
            self.execution
        )["comment_id"])
        attempt_status = self.ledger.connection.execute(
            "SELECT status FROM linear_evidence_attempts WHERE execution_id=?",
            (self.execution,),
        ).fetchone()[0]
        self.assertEqual("confirmed_after_read", attempt_status)

    def test_ambiguous_update_reads_applied_body_before_retry(self):
        remote = FakeComments()
        worker = LinearEvidenceWorker(self.ledger, remote)
        worker.drain_one(self.stage())
        remote.ambiguous_update = True
        self.assertEqual("ambiguous", worker.drain_one(self.stage("second")))
        self.assertEqual(
            "confirmed", worker.drain_one(self.ledger.linear_evidence(self.execution))
        )
        self.assertEqual(1, remote.updates)

    def test_deleted_confirmed_comment_fails_soft(self):
        remote = FakeComments()
        worker = LinearEvidenceWorker(self.ledger, remote)
        first = self.stage()
        worker.drain_one(first)
        remote.comments.clear()
        result = worker.drain_one(self.stage("second"))
        self.assertEqual("failed", result)
        self.assertEqual("failed", self.ledger.linear_evidence(
            self.execution
        )["status"])
        self.assertEqual("Todo", self.ledger.current(
            self.execution
        )["current_state_id"])

    def test_rate_limit_is_backed_off_and_auth_rejection_is_terminal(self):
        remote = FakeComments()
        remote.create_error = LinearAPIError(
            "RATELIMITED", "limited", retryable=True
        )
        worker = LinearEvidenceWorker(self.ledger, remote)
        self.assertEqual("pending", worker.drain_one(self.stage()))
        evidence = self.ledger.linear_evidence(self.execution)
        self.assertEqual("pending", evidence["status"])
        self.assertIsNotNone(evidence["next_attempt_at"])
        self.assertEqual([], self.ledger.pending_linear_evidence())

        other = self.kernel.begin(
            "example-service", "TASK-627",
            {"title": "auth", "linear_issue_id": "issue-auth"},
            command_id="begin-auth-evidence",
        )
        body = "auth"
        staged = self.ledger.stage_linear_evidence(
            other, issue_id="issue-auth", body=body,
            digest=hashlib.sha256(body.encode()).hexdigest(),
        )
        remote.create_error = LinearAPIError(
            "http_401", "rejected", retryable=False
        )
        self.assertEqual("failed", worker.drain_one(staged))
        self.assertEqual("Todo", self.ledger.current(other)["current_state_id"])

    def test_read_outage_does_not_trigger_another_create(self):
        remote = FakeComments()
        remote.ambiguous_create = True
        worker = LinearEvidenceWorker(self.ledger, remote)
        worker.drain_one(self.stage())
        remote.comment_error = LinearAPIError(
            "transport_error", "unknown", retryable=True, ambiguous=True
        )
        self.assertEqual(
            "ambiguous", worker.drain_one(self.ledger.linear_evidence(self.execution))
        )
        self.assertEqual(1, remote.creates)

    def test_renderer_is_readable_bounded_and_redacts_secret_shapes(self):
        claim = self.kernel.transition(
            self.execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner", command_id="claim-evidence",
        )
        preparation = self.ledger.begin_preparation(
            attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
            request_digest="request-evidence",
        )
        self.ledger.fail_preparation(
            str(preparation["id"]), fence_token=claim["fence_token"],
            status="failed", error={
                "code": "DOTFACTORY_RUNNER_FAILED", "category": "runner",
                "message": "authorization: lin_" + "api_supersecretvalue" + "x" * 20000,
                "safe_remedy": "Inspect the runner.",
            },
        )
        projection = ObservationService(
            self.ledger, self.kernel
        ).execution_projection(self.execution)
        body, digest = render_linear_run_summary(
            self.ledger.run_snapshot(self.execution), projection,
            self.ledger.run_history(self.execution),
        )
        self.assertIn("## Dotfactory run — Investigating", body)
        self.assertIn("### Nodes traversed", body)
        self.assertIn("**Path:** `Todo` → `Autoplanning`", body)
        self.assertIn("1. `Todo` — 0.0s · no model call", body)
        self.assertIn("2. `Autoplanning` (active)", body)
        self.assertIn(
            "**Attempts:** 1 total · 0 completed · 0 failed · 1 active", body
        )
        self.assertIn("### Active incident", body)
        self.assertIn("[REDACTED]", body)
        self.assertNotIn("supersecretvalue", body)
        self.assertLessEqual(len(body), 12000)
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), digest)

    def test_running_renderer_is_stable_without_new_facts(self):
        service = ObservationService(self.ledger, self.kernel)
        projection = service.execution_projection(self.execution)
        first = render_linear_run_summary(
            self.ledger.run_snapshot(self.execution), projection,
            self.ledger.run_history(self.execution),
        )
        second = render_linear_run_summary(
            self.ledger.run_snapshot(self.execution),
            service.execution_projection(self.execution),
            self.ledger.run_history(self.execution),
        )
        self.assertEqual(first, second)

    def test_renderer_shows_durable_time_and_token_coverage_per_node(self):
        self.kernel.transition(
            self.execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner", command_id="claim-node-metrics",
        )
        snapshot = self.ledger.run_snapshot(self.execution)
        projection = ObservationService(
            self.ledger, self.kernel
        ).execution_projection(self.execution)
        history = self.ledger.run_history(self.execution)
        current = str(snapshot["current_state_run_id"])
        history["state_token_usage"][current] = {
            "runner_runs": 2, "measured_runner_runs": 1,
            "total_tokens": 12345, "complete": False,
        }
        body, _digest = render_linear_run_summary(snapshot, projection, history)
        self.assertIn("12,345 tokens (partial)", body)
        history["state_token_usage"][current] = {
            "runner_runs": 1, "measured_runner_runs": 0,
            "total_tokens": 0, "complete": False,
        }
        body, _digest = render_linear_run_summary(snapshot, projection, history)
        self.assertIn("usage unavailable", body)

    def test_schema_eleven_migrates_linear_evidence_tables(self):
        path = Path(self.temp.name) / "schema-eleven.db"
        ledger = SQLiteLedger(path)
        ledger.connection.execute("DROP TABLE linear_evidence_attempts")
        ledger.connection.execute("DROP TABLE linear_evidence")
        ledger.connection.execute("PRAGMA user_version=11")
        ledger.close()
        migrated = SQLiteLedger(path)
        tables = {
            row[0] for row in migrated.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertEqual(12, migrated.connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0])
        self.assertTrue({
            "linear_evidence", "linear_evidence_attempts",
        }.issubset(tables))
        migrated.close()

    def test_incidents_nest_provider_cause_under_trusted_classification(self):
        def group(code, trust, trace_seq):
            return {
                "fingerprint": code.lower(), "code": code, "message": code,
                "safe_remedy": "inspect", "occurrence_count": 1,
                "trust_class": trust,
                "occurrences": [{
                    "trace_seq": trace_seq, "runner_run_id": "runner-1",
                    "attempt_id": "attempt-1", "responsible_span_id": "span-1",
                }],
            }

        incidents = readable_incidents([
            group("DOTFACTORY_ERROR", "untrusted-provider", 10),
            group("DOTFACTORY_RUNNER_FAILED", "trusted-runtime", 11),
        ], recovered=True)
        self.assertEqual(1, len(incidents))
        self.assertEqual("DOTFACTORY_RUNNER_FAILED", incidents[0]["primary"]["code"])
        self.assertEqual("DOTFACTORY_ERROR", incidents[0]["causes"][0]["code"])
        self.assertEqual("recovered", incidents[0]["status"])


if __name__ == "__main__":
    unittest.main()
