import tempfile
import unittest
from pathlib import Path

from dotfactory.control import ControlService, Principal
from dotfactory.kernel import DurableKernel
from dotfactory.ledger import LedgerError, SQLiteLedger
from dotfactory.runner import FakeRunner, RunnerResult, run_fake_attempt
from dotfactory.workflow import WorkflowError


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows" / "three-step-advanced.dot"
EVIDENCE = ({"kind": "receipt", "uri": "fake://runner/receipt"},)


class ThreeStepWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "factory.db"
        self.ledger = SQLiteLedger(self.path)
        self.ledger.configure_factory("three-step-test")
        self.ledger.register_project(
            "proof", display_name="Proof", tracker_kind="fake",
            tracker_project_id="proof",
        )
        self.kernel = DurableKernel(self.ledger, WORKFLOW)
        self.control = ControlService(self.ledger, self.kernel)
        self.approver = Principal("human-1", "approver", "test")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def begin(self, command_id="begin"):
        return self.kernel.begin(
            "proof", "TASK-PROOF", {"title": "Prove custom graph"},
            command_id=command_id, owner="fake-builder",
        )

    def ready_for_review(self, execution):
        runner = FakeRunner([RunnerResult("completed", "ready", EVIDENCE)])
        result = run_fake_attempt(
            self.kernel, execution, runner, command_id="build-ready"
        )
        self.assertEqual("review", result["to_state"])
        return runner

    def approve(self, execution, command_id="approve"):
        return self.control.execute(
            execution, command_id=command_id, principal=self.approver,
            request={
                "action": "approve", "expected_state": "review",
                "parameters": {"note": "The proof is sufficient."},
            },
        )

    def test_success_path_records_resolved_binding_and_exact_graph_edges(self):
        execution = self.begin()
        current = self.ledger.current(execution)
        binding = current["attempt"]["binding"]
        self.assertEqual(self.kernel.definition.digest, current["workflow_digest"])
        self.assertEqual("gpt-5.6-sol", binding["resolved"]["model"])
        self.assertEqual(3, binding["resolved"]["max_retries"])
        runner = self.ready_for_review(execution)
        self.assertEqual(self.kernel.definition.digest, runner.requests[0].workflow_digest)
        receipt = self.approve(execution)
        self.assertEqual("completed", receipt["status"])
        history = self.ledger.run_history(execution)
        self.assertEqual(
            ["build", "review", "done"],
            [item["state_id"] for item in history["state_runs"]],
        )
        allowed = {
            edge["id"] for edge in self.kernel.edges
        }
        executed = {
            item["payload"]["edge_id"] for item in history["events"]
            if item["event_type"] == "transition_accepted"
        }
        self.assertLessEqual(executed, allowed)
        self.assertEqual(
            self.kernel.definition.digest,
            history["workflow_snapshot"]["digest"],
        )

    def test_revision_returns_to_build_with_feedback(self):
        execution = self.begin()
        self.ready_for_review(execution)
        receipt = self.control.execute(
            execution, command_id="revise", principal=self.approver,
            request={
                "action": "transition", "expected_state": "review",
                "parameters": {
                    "to_state": "build", "owner": "fake-builder-2",
                    "feedback": [{
                        "source": "test", "kind": "changes_requested",
                        "author": "human-1", "body": "Add the missing proof.",
                        "url": "test://review/revise",
                    }],
                },
            },
        )
        self.assertEqual("completed", receipt["status"])
        current = self.ledger.current(execution)
        self.assertEqual("build", current["current_state_id"])
        self.assertEqual("fake-builder-2", current["attempt"]["owner"])
        handed_over = self.ledger.feedback_for_attempt(current["attempt"]["id"])
        self.assertEqual("Add the missing proof.", handed_over[0]["body"]["body"])

    def test_restart_resumes_at_checkpoint_without_repeating_build(self):
        execution = self.begin()
        self.ready_for_review(execution)
        digest = self.kernel.definition.digest
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        self.kernel = DurableKernel(self.ledger, WORKFLOW)
        self.control = ControlService(self.ledger, self.kernel)
        self.approve(execution, command_id="approve-after-restart")
        history = self.ledger.run_history(execution)
        self.assertEqual(digest, history["workflow_snapshot"]["digest"])
        self.assertEqual(1, sum(
            item["state_id"] == "build" for item in history["state_runs"]
        ))

    def test_running_execution_keeps_its_snapshotted_policy_after_graph_edit(self):
        execution = self.begin()
        self.ready_for_review(execution)
        original_digest = self.kernel.definition.digest
        changed = Path(self.temp.name) / "changed.dot"
        changed.write_text(
            WORKFLOW.read_text(encoding="utf-8").replace(
                "on=approve",
                "on=accept",
            ),
            encoding="utf-8",
        )
        self.kernel = DurableKernel(self.ledger, changed)
        self.control = ControlService(self.ledger, self.kernel)
        self.assertNotEqual(original_digest, self.kernel.definition.digest)
        receipt = self.approve(execution, command_id="approve-old-snapshot")
        self.assertEqual("completed", receipt["status"])
        self.assertEqual(
            original_digest,
            self.ledger.workflow_snapshot(execution)["digest"],
        )

    def test_retry_exhaustion_follows_declared_edge(self):
        execution = self.begin()
        runner = FakeRunner([
            RunnerResult("failed", "retry", EVIDENCE) for _ in range(4)
        ])
        for index in range(4):
            run_fake_attempt(
                self.kernel, execution, runner, command_id=f"retry-{index}"
            )
        history = self.ledger.run_history(execution)
        self.assertEqual("canceled", history["execution"]["current_state_id"])
        self.assertEqual(4, sum(
            item["state_id"] == "build" for item in history["state_runs"]
        ))
        self.assertEqual(
            "build.exhausted",
            [
                item["payload"]["edge_id"] for item in history["events"]
                if item["event_type"] == "transition_accepted"
            ][-1],
        )

    def test_empty_runner_evidence_cannot_complete_work(self):
        execution = self.begin()
        runner = FakeRunner([RunnerResult("completed", "ready", ())])
        with self.assertRaisesRegex(LedgerError, "leaving work requires outcome and evidence"):
            run_fake_attempt(self.kernel, execution, runner, command_id="false-success")

    def test_declared_global_cancel_edge_terminates_work(self):
        execution = self.begin()
        receipt = self.control.execute(
            execution, command_id="cancel", principal=self.approver,
            request={
                "action": "cancel", "expected_state": "build", "confirmed": True,
                "parameters": {"reason": "Proof canceled intentionally."},
            },
        )
        self.assertEqual("completed", receipt["status"])
        history = self.ledger.run_history(execution)
        self.assertEqual("canceled", history["execution"]["current_state_id"])
        self.assertEqual(
            "open.cancel",
            [
                item["payload"]["edge_id"] for item in history["events"]
                if item["event_type"] == "transition_accepted"
            ][-1],
        )

    def test_minimal_one_file_graph_executes_without_labeled_agent_edge(self):
        kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "three-step.dot"
        )
        execution = kernel.begin(
            "proof", "TASK-MINIMAL", {"title": "Minimal"},
            command_id="minimal", owner="fake-builder",
        )
        runner = FakeRunner([RunnerResult("completed", "ready", EVIDENCE)])
        result = run_fake_attempt(
            kernel, execution, runner, command_id="minimal-ready"
        )
        self.assertEqual("review", result["to_state"])
        control = ControlService(self.ledger, kernel)
        receipt = control.execute(
            execution, command_id="minimal-approve", principal=self.approver,
            request={
                "action": "approve", "expected_state": "review",
                "parameters": {"note": "Approved."},
            },
        )
        self.assertEqual("completed", receipt["status"])

    def test_unlabeled_success_convention_does_not_swallow_retry(self):
        kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "three-step.dot"
        )
        execution = kernel.begin(
            "proof", "TASK-NO-RETRY", {"title": "No retry edge"},
            command_id="no-retry", owner="fake-builder",
        )
        runner = FakeRunner([RunnerResult("failed", "retry", EVIDENCE)])
        with self.assertRaisesRegex(
            LedgerError, "no unique agent edge for outcome label retry"
        ):
            run_fake_attempt(
                kernel, execution, runner, command_id="retry-not-declared"
            )
        self.assertEqual(
            "build", self.ledger.current(execution)["current_state_id"]
        )

    def test_many_to_one_linear_projection_rejects_ambiguous_reverse_route(self):
        workflow = Path(self.temp.name) / "ambiguous.dot"
        workflow.write_text(
            """digraph Ambiguous {
              start [shape=Mdiamond]
              review [type=human, linear_status=Review]
              accepted [shape=Msquare, linear_status=Done]
              archived [shape=Msquare, linear_status=Done]
              start -> review
              review -> accepted [evocations="human:linear_status_change"]
              review -> archived [evocations="human:linear_status_change"]
            }
            """,
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            WorkflowError, "ambiguous human Linear reverse route"
        ):
            DurableKernel(self.ledger, workflow)


if __name__ == "__main__":
    unittest.main()
