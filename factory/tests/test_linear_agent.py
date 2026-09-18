import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from dotfactory import (
    DurableKernel, FactoryConfig, FactoryConfigError, ObservationService,
    SQLiteLedger,
)
from dotfactory.linear_agent import (
    LinearAgentSessionWorker, build_agent_projection,
)
from dotfactory.linear_api import LinearAPIError, LinearGraphQLClient
from dotfactory.linear_evidence import LinearEvidenceWorker
from dotfactory.lifecycle import FactoryRuntime


ROOT = Path(__file__).resolve().parents[1]


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, endpoint, headers, body, timeout):
        self.calls.append(json.loads(body))
        return self.responses.pop(0)


class FakeAgentAPI:
    def __init__(self):
        self.sessions = {}
        self.activities = {}
        self.creates = 0
        self.updates = 0
        self.activity_creates = 0
        self.create_error = None
        self.activity_error = None
        self.ambiguous_create = False
        self.ambiguous_activity = False

    def create_agent_session(self, *, issue_id, external_urls):
        self.creates += 1
        if self.create_error:
            raise self.create_error
        session_id = f"session-{self.creates}"
        session = {
            "id": session_id,
            "url": f"https://linear.example/session/{session_id}",
            "status": "pending",
            "issue": {"id": issue_id},
            "externalLinks": copy.deepcopy(external_urls),
        }
        self.sessions[session_id] = session
        if self.ambiguous_create:
            self.ambiguous_create = False
            raise LinearAPIError(
                "transport_error", "unknown", retryable=True, ambiguous=True
            )
        return copy.deepcopy(session)

    def update_agent_session(self, *, session_id, external_urls):
        self.updates += 1
        self.sessions[session_id]["externalLinks"] = copy.deepcopy(external_urls)
        return copy.deepcopy(self.sessions[session_id])

    def issue_agent_sessions(self, issue_id):
        return [
            copy.deepcopy(item) for item in self.sessions.values()
            if item["issue"]["id"] == issue_id
        ]

    def agent_session(self, session_id):
        item = self.sessions.get(session_id)
        return copy.deepcopy(item) if item else None

    def create_agent_activity(self, *, session_id, activity_id, content):
        self.activity_creates += 1
        if self.activity_error:
            raise self.activity_error
        if activity_id in self.activities:
            raise LinearAPIError("ALREADY_EXISTS", "duplicate", retryable=False)
        activity = {
            "id": activity_id, "agentSession": {"id": session_id},
            "content": copy.deepcopy(content),
        }
        self.activities[activity_id] = activity
        if self.ambiguous_activity:
            self.ambiguous_activity = False
            raise LinearAPIError(
                "transport_error", "unknown", retryable=True, ambiguous=True
            )
        return copy.deepcopy(activity)

    def agent_activity(self, activity_id):
        item = self.activities.get(activity_id)
        return copy.deepcopy(item) if item else None


class FakeComments:
    def __init__(self):
        self.comments = {}

    def comment(self, comment_id):
        return self.comments.get(comment_id)

    def create_comment(self, *, issue_id, comment_id, body):
        item = {
            "id": comment_id, "body": body,
            "url": f"https://linear.example/comment/{comment_id}",
            "issue": {"id": issue_id},
        }
        self.comments[comment_id] = item
        return item

    def update_comment(self, *, comment_id, body):
        self.comments[comment_id]["body"] = body
        return self.comments[comment_id]


class LinearAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "factory.db"
        self.ledger = SQLiteLedger(self.path)
        self.ledger.configure_factory("linear-agent-test")
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="project-dotfactory",
        )
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")
        self.execution = self.kernel.begin(
            "dotfactory", "EXAMPLE-574",
            {"title": "agent sessions", "linear_issue_id": "issue-574"},
            command_id="begin-agent-session",
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def projection(self, marker="https://runs.example/EXAMPLE-574"):
        snapshot = self.ledger.run_snapshot(self.execution)
        projection = ObservationService(
            self.ledger, self.kernel
        ).execution_projection(self.execution)
        history = self.ledger.run_history(self.execution)
        urls, activities = build_agent_projection(
            snapshot, projection, history, marker_url=marker,
        )
        return urls, activities

    def test_client_uses_preview_mutations_and_caller_activity_id(self):
        transport = QueueTransport([
            {"data": {"agentSessionCreateOnIssue": {
                "success": True, "agentSession": {
                    "id": "session-1", "url": "https://linear/session-1",
                    "status": "pending", "issue": {"id": "issue-1"},
                    "externalLinks": [{
                        "label": "Dotfactory run", "url": "https://runs/1",
                    }],
                },
            }}},
            {"data": {"agentActivityCreate": {
                "success": True, "agentActivity": {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "agentSession": {"id": "session-1"},
                },
            }}},
        ])
        client = LinearGraphQLClient("auth", transport=transport)
        client.create_agent_session(
            issue_id="issue-1", external_urls=[{
                "label": "Dotfactory run", "url": "https://runs/1",
            }],
        )
        activity_id = "11111111-1111-4111-8111-111111111111"
        client.create_agent_activity(
            session_id="session-1", activity_id=activity_id,
            content={"type": "thought", "body": "Started."},
        )
        self.assertEqual(
            "FactoryAgentSessionCreate", transport.calls[0]["operationName"]
        )
        self.assertEqual(
            activity_id, transport.calls[1]["variables"]["input"]["id"]
        )

    def test_session_reconciliation_reads_all_pages_and_rejects_truncation(self):
        def page(nodes, more, cursor):
            return {"data": {"issue": {"agentSessions": {"nodes": nodes,
                    "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}
        transport = QueueTransport([page([{"id": "other"}], True, "next"),
                                    page([{"id": "matching"}], False, "last")])
        client = LinearGraphQLClient("auth", transport=transport)
        self.assertEqual(["other", "matching"], [s["id"] for s in client.issue_agent_sessions("issue")])
        self.assertEqual("next", transport.calls[1]["variables"]["after"])
        broken = LinearGraphQLClient("auth", transport=QueueTransport([page([], True, None)]))
        with self.assertRaises(LinearAPIError):
            broken.issue_agent_sessions("issue")

    def test_one_session_and_sparse_activities_are_idempotent(self):
        remote = FakeAgentAPI()
        worker = LinearAgentSessionWorker(self.ledger, remote)
        urls, activities = self.projection()
        self.assertEqual("active", worker.sync(
            self.execution, issue_id="issue-574",
            marker_url="https://runs.example/EXAMPLE-574",
            external_urls=urls, activities=activities,
        ))
        first_ids = set(remote.activities)
        self.assertEqual("active", worker.sync(
            self.execution, issue_id="issue-574",
            marker_url="https://runs.example/EXAMPLE-574",
            external_urls=urls, activities=activities,
        ))
        self.assertEqual(1, remote.creates)
        self.assertEqual(first_ids, set(remote.activities))
        self.assertEqual(len(activities), remote.activity_creates)
        self.assertTrue(all(
            activity["content"]["type"] != "action"
            for activity in activities
        ))
        visible = ObservationService(self.ledger, self.kernel).run(
            self.execution
        )["data"]["linear_agent_session"]
        self.assertEqual("active", visible["status"])
        self.assertEqual(
            {"confirmed": len(activities)}, visible["activity_statuses"]
        )

    def test_ambiguous_session_create_reconciles_by_unique_marker(self):
        remote = FakeAgentAPI()
        remote.ambiguous_create = True
        worker = LinearAgentSessionWorker(self.ledger, remote)
        urls, activities = self.projection()
        result = worker.sync(
            self.execution, issue_id="issue-574",
            marker_url="https://runs.example/EXAMPLE-574",
            external_urls=urls, activities=activities,
        )
        self.assertEqual("active", result)
        self.assertEqual(1, remote.creates)
        self.assertEqual("active", worker.session(self.execution)["status"])
        attempt = self.ledger.connection.execute(
            "SELECT status FROM linear_agent_session_attempts"
        ).fetchone()[0]
        self.assertEqual("confirmed_after_read", attempt)

    def test_restart_after_remote_create_recovers_without_second_session(self):
        remote = FakeAgentAPI()
        worker = LinearAgentSessionWorker(self.ledger, remote)
        marker = "https://runs.example/EXAMPLE-574"
        urls, activities = self.projection(marker)
        staged = worker._stage_session(
            self.execution, issue_id="issue-574", marker_url=marker,
            external_urls=urls,
        )
        worker._begin_session_attempt(staged, "create")
        remote.create_agent_session(issue_id="issue-574", external_urls=urls)
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        restarted = LinearAgentSessionWorker(self.ledger, remote)
        self.assertEqual("active", restarted.sync(
            self.execution, issue_id="issue-574", marker_url=marker,
            external_urls=urls, activities=activities,
        ))
        self.assertEqual(1, remote.creates)

    def test_unreconciled_or_unsupported_create_falls_back_without_retry(self):
        remote = FakeAgentAPI()
        remote.create_error = LinearAPIError(
            "GRAPHQL_VALIDATION_FAILED", "preview unavailable", retryable=False
        )
        worker = LinearAgentSessionWorker(self.ledger, remote)
        urls, activities = self.projection()
        self.assertEqual("fallback", worker.sync(
            self.execution, issue_id="issue-574",
            marker_url="https://runs.example/EXAMPLE-574",
            external_urls=urls, activities=activities,
        ))
        self.assertEqual("fallback", worker.sync(
            self.execution, issue_id="issue-574",
            marker_url="https://runs.example/EXAMPLE-574",
            external_urls=urls, activities=activities,
        ))
        self.assertEqual(1, remote.creates)

    def test_lifecycle_uses_owned_comment_after_agent_api_fallback(self):
        remote = FakeAgentAPI()
        remote.create_error = LinearAPIError(
            "FORBIDDEN", "agent actor unavailable", retryable=False
        )
        comments = FakeComments()

        class Config:
            def resolve_linear_projection(self, *, environment):
                return {
                    "agent_session_url_template": (
                        "https://runs.example/{execution_id}"
                    )
                }

        runtime = object.__new__(FactoryRuntime)
        runtime.ledger = self.ledger
        runtime.kernels = {"dotfactory": self.kernel}
        runtime.config = Config()
        runtime.environment = {}
        runtime.linear_agent_workers = {
            "dotfactory": LinearAgentSessionWorker(self.ledger, remote),
        }
        runtime.linear_evidence_workers = {
            "dotfactory": LinearEvidenceWorker(self.ledger, comments),
        }
        runtime._sync_linear_evidence()
        self.assertEqual(1, len(comments.comments))
        self.assertEqual(
            "confirmed", self.ledger.linear_evidence(self.execution)["status"]
        )

    def test_ambiguous_activity_is_read_before_reuse_of_stable_id(self):
        remote = FakeAgentAPI()
        remote.ambiguous_activity = True
        worker = LinearAgentSessionWorker(self.ledger, remote)
        urls, activities = self.projection()
        worker.sync(
            self.execution, issue_id="issue-574",
            marker_url="https://runs.example/EXAMPLE-574",
            external_urls=urls, activities=activities,
        )
        first_count = remote.activity_creates
        worker.sync(
            self.execution, issue_id="issue-574",
            marker_url="https://runs.example/EXAMPLE-574",
            external_urls=urls, activities=activities,
        )
        self.assertEqual(first_count, remote.activity_creates)
        self.assertEqual(
            len(activities), self.ledger.connection.execute(
                "SELECT COUNT(*) FROM linear_agent_activities "
                "WHERE status='confirmed'"
            ).fetchone()[0],
        )

    def test_new_external_links_update_the_same_session(self):
        remote = FakeAgentAPI()
        worker = LinearAgentSessionWorker(self.ledger, remote)
        urls, activities = self.projection()
        worker.sync(
            self.execution, issue_id="issue-574",
            marker_url="https://runs.example/EXAMPLE-574",
            external_urls=urls, activities=activities,
        )
        session_id = worker.session(self.execution)["session_id"]
        updated = urls + [{"label": "Pull request", "url": "https://github.com/a/b/pull/1"}]
        worker.sync(
            self.execution, issue_id="issue-574",
            marker_url="https://runs.example/EXAMPLE-574",
            external_urls=updated, activities=activities,
        )
        self.assertEqual(session_id, worker.session(self.execution)["session_id"])
        self.assertEqual(1, remote.updates)

    def test_worker_location_is_per_attempt_and_replay_safe(self):
        snapshot = self.ledger.run_snapshot(self.execution)
        projection = ObservationService(self.ledger, self.kernel).execution_projection(self.execution)
        history = self.ledger.run_history(self.execution)
        snapshot["worker_handoffs"] = [
            {"attempt_id": "a", "state": "Implementing", "worker": "render", "location": "cloud"},
            {"attempt_id": "b", "state": "Verifying", "worker": "mac", "location": "local"},
            {"attempt_id": "c", "state": "Reworking", "worker": "legacy"},
        ]
        urls, activities = build_agent_projection(snapshot, projection, history,
                                                 marker_url="https://runs.example/EXAMPLE-574")
        location_activities = [a for a in activities if a["semantic_key"].startswith("worker-location:")]
        self.assertEqual([
            "☁️ Cloud · `Implementing` · worker `render`.",
            "💻 Local · `Verifying` · worker `mac`.",
            "Location unknown · `Reworking` · worker `legacy`.",
        ], [a["content"]["body"] for a in location_activities])
        remote = FakeAgentAPI()
        worker = LinearAgentSessionWorker(self.ledger, remote)
        for status in ("selected", "accepted"):
            for handoff in snapshot["worker_handoffs"]:
                handoff["status"] = status
            urls, activities = build_agent_projection(snapshot, projection, history,
                                                     marker_url="https://runs.example/EXAMPLE-574")
            worker.sync(self.execution, issue_id="issue-574", marker_url="https://runs.example/EXAMPLE-574",
                        external_urls=urls, activities=activities)
            worker = LinearAgentSessionWorker(self.ledger, remote)
        self.assertEqual(len(activities), remote.activity_creates)

    def test_projection_redacts_secrets_and_terminal_activity_is_final(self):
        snapshot = self.ledger.run_snapshot(self.execution)
        projection = ObservationService(
            self.ledger, self.kernel
        ).execution_projection(self.execution)
        history = self.ledger.run_history(self.execution)
        snapshot["attention_requests"] = [{
            "id": "attention-1", "reason": "authorization: secret-sentinel",
        }]
        _urls, activities = build_agent_projection(
            snapshot, projection, history,
            marker_url="https://runs.example/EXAMPLE-574",
        )
        self.assertEqual("elicitation", activities[-1]["content"]["type"])
        self.assertNotIn("secret-sentinel", json.dumps(activities))

        snapshot["status"] = "completed"
        snapshot["current_state_id"] = "Done"
        snapshot["completed_at"] = snapshot["created_at"]
        _urls, activities = build_agent_projection(
            snapshot, projection, history,
            marker_url="https://runs.example/EXAMPLE-574",
        )
        self.assertEqual("response", activities[-1]["content"]["type"])

    def test_equal_clock_preserves_activity_order_through_restart(self):
        self.ledger.clock = lambda: "2026-09-16T00:00:00Z"
        remote = FakeAgentAPI()
        worker = LinearAgentSessionWorker(self.ledger, remote)
        marker = "https://runs.example/EXAMPLE-574"
        urls = [{"label": "Run", "url": marker}]
        activities = [{"semantic_key": key, "content": {"type": kind, "body": key}}
                      for key, kind in [("start", "thought"), ("progress", "thought"), ("final", "response")]]
        with patch("dotfactory.linear_agent.uuid.uuid4", side_effect=["z", "y", "x"]):
            worker._stage_session(self.execution, issue_id="issue-574", marker_url=marker, external_urls=urls)
            for item in activities:
                worker._stage_activity(self.execution, item["semantic_key"], item["content"])
                if item["semantic_key"] == "start":
                    self.ledger.connection.execute(
                        "UPDATE linear_agent_activities SET created_at='2026-09-16T00:00:00Z' WHERE semantic_key='start'")
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        worker = LinearAgentSessionWorker(self.ledger, remote)
        worker.sync(self.execution, issue_id="issue-574", marker_url=marker, external_urls=urls, activities=activities)
        self.assertEqual(["start", "progress", "final"],
                         [a["content"]["body"] for a in remote.activities.values()])

    def test_retrying_activity_blocks_later_terminal_response(self):
        self.ledger.clock = lambda: "2026-09-16T00:00:00+00:00"
        remote = FakeAgentAPI()
        remote.activity_error = LinearAPIError("RATE_LIMITED", "later", retryable=True)
        worker = LinearAgentSessionWorker(self.ledger, remote)
        marker = "https://runs.example/EXAMPLE-574"
        urls = [{"label": "Run", "url": marker}]
        activities = [{"semantic_key": key, "content": {"type": kind, "body": key}}
                      for key, kind in [("start", "thought"), ("final", "response")]]
        def sync():
            return worker.sync(self.execution, issue_id="issue-574", marker_url=marker,
                               external_urls=urls, activities=activities)
        self.assertEqual("pending", sync())
        remote.activity_error = None
        self.assertEqual("pending", sync())
        self.assertEqual(1, remote.activity_creates)
        self.assertEqual({}, remote.activities)
        self.ledger.clock = lambda: "2026-09-16T00:01:00+00:00"
        self.assertEqual("active", sync())
        self.assertEqual(["thought", "response"], [a["content"]["type"] for a in remote.activities.values()])

    def test_terminal_enrichment_preserves_first_response_and_updates_links(self):
        remote = FakeAgentAPI()
        worker = LinearAgentSessionWorker(self.ledger, remote)
        marker = "https://runs.example/EXAMPLE-574"
        urls = [{"label": "Run", "url": marker}]
        activities = [{"semantic_key": "terminal:done-run", "content": {"type": "response", "body": "Done"}}]
        def sync():
            return worker.sync(self.execution, issue_id="issue-574", marker_url=marker,
                               external_urls=urls, activities=activities)
        self.assertEqual("active", sync())
        urls.append({"label": "Later evidence", "url": "https://runs.example/evidence"})
        activities[0]["content"]["body"] = "Done with additional trace evidence"
        self.assertEqual("active", sync())
        self.assertEqual(1, remote.activity_creates)
        self.assertEqual("Done", next(iter(remote.activities.values()))["content"]["body"])
        self.assertEqual(1, remote.updates)

    def test_new_links_do_not_resend_an_ambiguous_session_create(self):
        remote = FakeAgentAPI()
        worker = LinearAgentSessionWorker(self.ledger, remote)
        marker = "https://runs.example/EXAMPLE-574"
        urls, activities = self.projection(marker)
        staged = worker._stage_session(self.execution, issue_id="issue-574", marker_url=marker, external_urls=urls)
        worker._begin_session_attempt(staged, "create")
        remote.create_agent_session(issue_id="issue-574", external_urls=urls)
        updated = urls + [{"label": "Evidence", "url": "https://runs.example/evidence"}]
        self.assertEqual("active", worker.sync(self.execution, issue_id="issue-574", marker_url=marker,
                                               external_urls=updated, activities=activities))
        self.assertEqual(1, remote.creates)
        self.assertEqual(1, remote.updates)
        self.assertEqual("session-1", worker.session(self.execution)["session_id"])

    def test_runtime_separates_agent_credentials_and_missing_token_falls_back(self):
        class Config:
            def resolve_linear_projection(self, *, environment):
                return {"enabled": True, "endpoint": "https://api.linear.app/graphql", "timeout_seconds": 15,
                        "agent_sessions_enabled": True, "agent_token_env": "LINEAR_AGENT_TOKEN"}
            def linear_authorization(self, *, environment):
                return "personal-api-key"
            def resolve_project(self, key, *, environment):
                return {"tracker_team_id": "team", "tracker_project_id": "project"}
        for environment in [{}, {"LINEAR_AGENT_TOKEN": "synthetic-app-token"}]:
            runtime = object.__new__(FactoryRuntime)
            runtime.config, runtime.environment, runtime.ledger = Config(), environment, self.ledger
            runtime.kernels = {"dotfactory": self.kernel}
            runtime.linear_workers, runtime.linear_evidence_workers, runtime.linear_agent_workers = {}, {}, {}
            runtime.preflights = []
            with patch("dotfactory.lifecycle.LinearConvergenceWorker.preflight_project", return_value=[]):
                runtime._build_linear()
            self.assertEqual("personal-api-key", runtime.linear_evidence_workers["dotfactory"].client.authorization)
            self.assertEqual(bool(environment), bool(runtime.linear_agent_workers))
            if environment:
                self.assertEqual("Bearer synthetic-app-token", runtime.linear_agent_workers["dotfactory"].client.authorization)
            else:
                self.assertFalse(runtime.preflights[0]["available"])

    def test_schema_twelve_migrates_agent_projection_tables(self):
        self.ledger.connection.execute("DROP TABLE linear_agent_activity_attempts")
        self.ledger.connection.execute("DROP TABLE linear_agent_activities")
        self.ledger.connection.execute("DROP TABLE linear_agent_session_attempts")
        self.ledger.connection.execute("DROP TABLE linear_agent_sessions")
        self.ledger.connection.execute("PRAGMA user_version=12")
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        tables = {
            row[0] for row in self.ledger.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertEqual(13, self.ledger.connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0])
        self.assertTrue({
            "linear_agent_sessions", "linear_agent_session_attempts",
            "linear_agent_activities", "linear_agent_activity_attempts",
        }.issubset(tables))

    def test_config_requires_explicit_https_run_url_for_agent_sessions(self):
        values = json.loads((ROOT / "factory.example.json").read_text())
        values["projections"]["linear"]["agent_sessions_enabled"] = True
        values["projections"]["linear"].pop("agent_session_url_template")
        path = Path(self.temp.name) / "invalid-agent-config.json"
        path.write_text(json.dumps(values))
        with self.assertRaisesRegex(FactoryConfigError, "url_template"):
            FactoryConfig.load(path)

        values["projections"]["linear"]["agent_session_url_template"] = (
            "https://runs.example/{execution_id}"
        )
        path.write_text(json.dumps(values))
        config = FactoryConfig.load(path)
        resolved = config.resolve_linear_projection(environment={})
        self.assertTrue(resolved["agent_sessions_enabled"])


if __name__ == "__main__":
    unittest.main()
