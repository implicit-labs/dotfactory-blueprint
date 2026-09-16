import json
import tempfile
import unittest
from pathlib import Path

from dotfactory.cli import _demo_config, _git
from dotfactory.control import ObservationService, Principal
from dotfactory.instance import FactoryConfig
from dotfactory.lifecycle import FactoryRuntime, LifecycleError, fixture_runner
from dotfactory.linear_api import LinearConvergenceWorker
from dotfactory.linear_reconciliation import LinearStatusBindingV1
from dotfactory.live_runner import LiveRunner, RunnerRoute
from dotfactory.runner import runner_request


class TrackerFixture:
    def __init__(self, state="Ready"):
        self.remote = {
            "id": "issue-demo", "identifier": "DEMO-1", "title": "Build greeting",
            "description": "REQUIREMENT_MARKER: greet in Japanese",
            "url": "https://example.invalid/DEMO-1", "updatedAt": "2026-09-07T00:00:00Z",
            "state": {"id": "s-" + state, "name": state},
            "project": {"id": "demo-project"}, "team": {"id": "demo-team"},
        }
        self.writes = []

    def issue(self, identifier):
        return dict(self.remote)

    def update_issue_status(self, issue_id, status_id):
        self.writes.append(status_id)
        self.remote = {**self.remote, "state": {"id": status_id, "name": status_id[2:]},
                       "updatedAt": f"2026-09-07T00:00:{len(self.writes):02d}Z"}
        return {}


class SupervisedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = _demo_config(self.root)
        self.config = FactoryConfig.load(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def tracker(self, runtime, state="Ready"):
        client = TrackerFixture(state)
        kernel = runtime.kernels["demo"]
        runtime.ledger.bind_linear_statuses("demo", kernel.definition.digest, "demo-team", [
            LinearStatusBindingV1(
                project_key="demo", workflow_digest=kernel.definition.digest,
                team_id="demo-team", status_id="s-" + name, status_name=name,
                status_type="unstarted",
            ) for name in kernel.states
        ])
        runtime.linear_workers["demo"] = LinearConvergenceWorker(
            runtime.ledger, kernel, client, self_actor_id="factory",
        )
        return client

    def test_ready_adoption_preserves_status_and_full_requirements(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            client = self.tracker(runtime)
            execution = runtime.start_issue("demo", "DEMO-1")
            current = runtime.ledger.current(execution)
            self.assertEqual("Ready", current["current_state_id"])
            self.assertEqual([], client.writes)
            self.assertIn("REQUIREMENT_MARKER", current["intent_snapshot_json"])
            runtime.step()
            self.assertNotIn("s-Todo", client.writes)
            self.assertNotIn("s-Autoplanning", client.writes)

    def test_terminal_and_foreign_issues_are_rejected_before_creating_execution(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            client = self.tracker(runtime, "Done")
            with self.assertRaises(LifecycleError):
                runtime.start_issue("demo", "DEMO-1")
            client.remote["state"] = {"id": "s-Todo", "name": "Todo"}
            client.remote["project"] = {"id": "foreign"}
            with self.assertRaises(LifecycleError):
                runtime.start_issue("demo", "DEMO-1")
            self.assertFalse(runtime.ledger.list_runs())
            self.assertEqual([], client.writes)

    def feedback(self):
        return {"source": "control_api", "kind": "changes_requested", "author": "reviewer",
                "body": "REVIEW_MARKER: formal Japanese", "url": "control://review-marker"}

    def test_actual_rework_prompt_has_immutable_requirements_outcomes_and_feedback(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1", description="REQUIREMENT_MARKER")
            runtime.run([execution], max_ticks=20)
            kernel = runtime.kernels["demo"]
            kernel.transition(execution, "Reworking", actor="human", signal="control_command",
                              owner="reviewer", command_id="revise", feedback=[self.feedback()])
            request = runner_request(kernel, execution)
            launch = runtime.projects["demo"].preparation.prepare(request).launch
            route = RunnerRoute(name="codex", kind="codex", command="codex",
                                minimum_version="0.1.0", permission_mode="approve-for-me",
                                disabled_mcp_servers=("linear",))
            live = LiveRunner(runtime.ledger, routes={"codex": route})
            prompt = live._prompt(launch, route)
            self.assertIn("REQUIREMENT_MARKER", prompt)
            self.assertIn("REVIEW_MARKER", prompt)
            self.assertIn("prior_outcomes", prompt)
            self.assertIn("local://demo", prompt)
            self.assertEqual(prompt, live._prompt(launch, route))
            self.assertEqual(1, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='attempt_input_snapshotted'",
            ).fetchone()[0])

    def test_pending_rework_is_claimed_once_with_feedback(self):
        with FactoryRuntime(self.config, runner=fixture_runner(6)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.run([execution], max_ticks=20)
            result = runtime.kernels["demo"].observe_linear_status(
                execution, "Reworking", command_id="human-rework", feedback=[self.feedback()],
            )
            self.assertEqual("pending", result["disposition"])
            runtime.step()
            self.assertIsNone(runtime.ledger.pending_transition(execution))
            states = [s["state_id"] for s in runtime.ledger.run_history(execution)["state_runs"]]
            self.assertEqual(1, states.count("Reworking"))

    def test_verification_keeps_the_review_request_with_original_target(self):
        with FactoryRuntime(self.config, runner=fixture_runner(6)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.run([execution], until_state="Review")
            runtime.kernels["demo"].transition(
                execution, "Reworking", actor="human", signal="control_command",
                owner="reviewer", command_id="verify-feedback", feedback=[self.feedback()],
            )
            rework_attempt = runtime.ledger.current(execution)["attempt"]["id"]
            runtime.step()
            self.assertEqual("Verifying", runtime.ledger.current(execution)["current_state_id"])
            attempt = runtime.ledger.current(execution)["attempt"]["id"]
            context = runtime.ledger.attempt_input_context(attempt)
            self.assertIn("REVIEW_MARKER", json.dumps(context["feedback"]))
            self.assertEqual(rework_attempt, context["feedback"][0]["target_id"])
            self.assertFalse(context["feedback"][0]["target_is_current_attempt"])

    def test_pending_planning_takes_precedence_over_automatic_pickup(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.kernels["demo"].observe_linear_status(
                execution, "Planning", command_id="human-plan",
            )
            runtime._claim_pickups()
            self.assertEqual("Planning", runtime.ledger.current(execution)["current_state_id"])
            self.assertIsNone(runtime.ledger.pending_transition(execution))

    def test_retention_survives_restart_and_clean_files_until_explicit_release(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.run([execution], max_ticks=20)
            workspace = Path(runtime.ledger.workspace_for_execution(execution)["path"])
            (workspace / "keep.txt").write_text("preserve this")
            runtime.control_service("demo").execute(
                execution, command_id="cancel", principal=Principal("tester", "operator", "test"),
                request={"action": "cancel", "expected_state": "Review", "confirmed": True},
            )
            runtime._cleanup_terminal_workspaces()
            view = ObservationService(runtime.ledger, runtime.kernels["demo"]).run(execution)["data"]
            self.assertTrue(view["available_actions"])
            attention = view["attention_requests"][0]
            runtime.control_service("demo").execute(
                execution, command_id="retain", principal=Principal("tester", "operator", "test"),
                request={"action": "attention", "expected_state": "Canceled",
                         "parameters": {"attention_id": attention["id"], "remedy": "retain"}},
            )
            _git(workspace, "config", "user.name", "Test")
            _git(workspace, "config", "user.email", "test@example.invalid")
            _git(workspace, "add", "keep.txt")
            _git(workspace, "commit", "-m", "preserve")
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            runtime._cleanup_terminal_workspaces()
            self.assertTrue(workspace.exists())
            held = runtime.ledger.run_snapshot(execution)["attention_requests"][0]
            receipt = runtime.control_service("demo").execute(
                execution, command_id="release", principal=Principal("tester", "approver", "test"),
                request={"action": "attention", "expected_state": "Canceled", "confirmed": True,
                         "parameters": {"attention_id": held["id"], "remedy": "release"}},
            )
            self.assertEqual("completed", receipt["status"])
            self.assertFalse(workspace.exists())

    def test_terminal_cleanup_does_not_touch_unselected_project(self):
        values = json.loads(self.path.read_text())
        values["projects"]["other"] = {**values["projects"]["demo"],
                                         "tracker": {"kind": "linear", "project_id": "other"}}
        self.path.write_text(json.dumps(values))
        config = FactoryConfig.load(self.path)
        with FactoryRuntime(config, runner=fixture_runner(), project_keys=["demo"]) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.run([execution], max_ticks=20)
            workspace = Path(runtime.ledger.workspace_for_execution(execution)["path"])
            runtime.control_service("demo").execute(
                execution, command_id="cancel", principal=Principal("tester", "operator", "test"),
                request={"action": "cancel", "expected_state": "Review", "confirmed": True},
            )
        with FactoryRuntime(config, runner=fixture_runner(), project_keys=["other"]) as runtime:
            self.assertEqual([], runtime.step()["cleanup"])
            self.assertTrue(workspace.exists())


if __name__ == "__main__":
    unittest.main()
