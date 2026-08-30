import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory import DurableKernel, KernelError, SQLiteLedger  # noqa: E402
from dotfactory.instance import FactoryConfig, FactoryConfigError  # noqa: E402
from dotfactory.ledger import LedgerError, StaleAttempt  # noqa: E402
from dotfactory.projections import ProjectionWorker, RunProjection  # noqa: E402
from dotfactory.raw_stream import RawEventStream  # noqa: E402


class DurableKernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "factory.db"
        self.ledger = SQLiteLedger(self.path)
        self.ledger.configure_factory("test-local")
        self.ledger.register_project(
            "example-ios", display_name="Example iOS", tracker_kind="linear",
            tracker_project_id="linear-example",
        )
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="linear-dotfactory",
        )
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def begin(self, command_id="begin", project_key="example-ios", identifier="TASK-567"):
        return self.kernel.begin(
            project_key, identifier, {"title": "durable kernel"}, command_id=command_id
        )

    def move_to_review(self):
        execution = self.begin()
        planning = self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="claim-planning",
        )
        self.kernel.transition(
            execution, "Ready", actor="agent", signal="agent_handoff",
            attempt_id=planning["attempt_id"], fence_token=planning["fence_token"],
            outcome="succeeded", evidence=[{"kind": "plan", "uri": "local://plan"}],
            command_id="finish-planning",
        )
        implementing = self.kernel.transition(
            execution, "Implementing", actor="agent", signal="listener_claim",
            owner="builder-1", command_id="claim-implementation",
        )
        verifying = self.kernel.transition(
            execution, "Verifying", actor="agent", signal="agent_handoff",
            owner="verifier-1", attempt_id=implementing["attempt_id"],
            fence_token=implementing["fence_token"], outcome="succeeded",
            evidence=[{"kind": "commit", "uri": "git://commit"}],
            command_id="finish-implementation",
        )
        self.kernel.transition(
            execution, "Review", actor="agent", signal="agent_handoff",
            attempt_id=verifying["attempt_id"], fence_token=verifying["fence_token"],
            outcome="succeeded", evidence=[{"kind": "test", "uri": "local://tests"}],
            command_id="finish-verification",
        )
        return execution

    def test_vertical_slice_is_atomic_and_restarts(self):
        execution = self.begin()
        claim = self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="claim",
        )
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")
        ready = self.kernel.transition(
            execution, "Ready", actor="agent", signal="agent_handoff",
            attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
            outcome="succeeded", evidence=[{"kind": "plan", "uri": "local://plan"}],
            command_id="ready",
        )
        self.assertEqual("Ready", ready["to_state"])
        current = self.ledger.current(execution)
        self.assertEqual("Ready", current["current_state_id"])
        self.assertIsNone(current["attempt"])

    def test_process_kill_at_transition_boundaries_is_recoverable(self):
        boundaries = (
            "after_attempt_completed",
            "after_leases_released",
            "after_current_state_run_completed",
            "after_next_state_run_created",
            "after_artifacts_recorded",
            "after_event_recorded",
            "after_transition_decision_recorded",
            "after_execution_updated",
            "transition_committed",
        )
        child = textwrap.dedent(
            """
            import os
            import sys
            from dotfactory import DurableKernel, SQLiteLedger

            path, workflow, execution, attempt, fence, boundary = sys.argv[1:]
            def kill_at(name):
                if name == boundary:
                    os._exit(97)
            ledger = SQLiteLedger(path, fault_hook=kill_at)
            kernel = DurableKernel(ledger, workflow)
            kernel.transition(
                execution, "Ready", actor="agent", signal="agent_handoff",
                attempt_id=attempt, fence_token=fence, outcome="succeeded",
                evidence=[{"kind": "plan", "uri": "local://plan"}],
                command_id="finish-after-kill",
            )
            """
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "kill.db"
                ledger = SQLiteLedger(path)
                ledger.configure_factory("test-local")
                ledger.register_project(
                    "example-ios", display_name="Example iOS", tracker_kind="linear",
                    tracker_project_id="linear-example",
                )
                kernel = DurableKernel(ledger, ROOT / "workflows" / "default.dot")
                execution = kernel.begin(
                    "example-ios", "TASK-567", {"title": "kill test"}, command_id="begin"
                )
                claim = kernel.transition(
                    execution, "Autoplanning", actor="agent", signal="listener_claim",
                    owner="planner-1", command_id="claim",
                )
                ledger.close()
                environment = dict(os.environ)
                environment["PYTHONPATH"] = str(ROOT / "src")
                environment["PYTHONPYCACHEPREFIX"] = "/tmp/dotfactory-kill-pycache"
                process = subprocess.run(
                    [sys.executable, "-c", child, str(path), str(ROOT / "workflows" / "default.dot"),
                     execution, claim["attempt_id"], claim["fence_token"], boundary],
                    env=environment, check=False,
                )
                self.assertEqual(97, process.returncode)
                recovered = SQLiteLedger(path)
                recovered_kernel = DurableKernel(recovered, ROOT / "workflows" / "default.dot")
                recovered_kernel.transition(
                    execution, "Ready", actor="agent", signal="agent_handoff",
                    attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
                    outcome="succeeded", evidence=[{"kind": "plan", "uri": "local://plan"}],
                    command_id="finish-after-kill",
                )
                self.assertEqual("Ready", recovered.current(execution)["current_state_id"])
                key = f"execution:{execution}:transition:finish-after-kill"
                decisions = recovered.connection.execute(
                    "SELECT COUNT(*) FROM transition_decisions td JOIN events e "
                    "ON e.seq=td.event_seq WHERE e.idempotency_key=?", (key,),
                ).fetchone()[0]
                self.assertEqual(1, decisions)
                recovered.close()

    def test_process_kill_while_creating_an_attempt_is_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kill-attempt.db"
            ledger = SQLiteLedger(path)
            ledger.configure_factory("test-local")
            ledger.register_project(
                "example-ios", display_name="Example iOS", tracker_kind="linear",
                tracker_project_id="linear-example",
            )
            kernel = DurableKernel(ledger, ROOT / "workflows" / "default.dot")
            execution = kernel.begin(
                "example-ios", "TASK-567", {"title": "kill test"}, command_id="begin"
            )
            ledger.close()
            child = textwrap.dedent(
                """
                import os
                import sys
                from dotfactory import DurableKernel, SQLiteLedger

                path, workflow, execution = sys.argv[1:]
                ledger = SQLiteLedger(
                    path,
                    fault_hook=lambda name: os._exit(97)
                    if name == "after_next_attempt_created" else None,
                )
                DurableKernel(ledger, workflow).transition(
                    execution, "Autoplanning", actor="agent", signal="listener_claim",
                    owner="planner-1", command_id="claim-after-kill",
                )
                """
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(ROOT / "src")
            environment["PYTHONPYCACHEPREFIX"] = "/tmp/dotfactory-kill-pycache"
            process = subprocess.run(
                [sys.executable, "-c", child, str(path), str(ROOT / "workflows" / "default.dot"), execution],
                env=environment, check=False,
            )
            self.assertEqual(97, process.returncode)
            recovered = SQLiteLedger(path)
            result = DurableKernel(recovered, ROOT / "workflows" / "default.dot").transition(
                execution, "Autoplanning", actor="agent", signal="listener_claim",
                owner="planner-1", command_id="claim-after-kill",
            )
            self.assertEqual("Autoplanning", recovered.current(execution)["current_state_id"])
            self.assertIsNotNone(result["attempt_id"])
            recovered.close()

    def test_projects_scope_work_items(self):
        vivo = self.begin(command_id="vivo", project_key="example-ios")
        factory = self.begin(command_id="factory", project_key="dotfactory")
        self.assertNotEqual(vivo, factory)
        self.assertEqual("example-ios", self.ledger.current(vivo)["project_key"])
        self.assertEqual("dotfactory", self.ledger.current(factory)["project_key"])
        count = self.ledger.connection.execute(
            "SELECT COUNT(*) FROM work_items WHERE identifier='TASK-567'"
        ).fetchone()[0]
        self.assertEqual(2, count)
        with self.assertRaises(LedgerError):
            self.ledger.configure_factory("another-factory")

    def test_repeated_execution_gets_a_human_key_suffix(self):
        first = self.begin(command_id="run-1")
        second = self.kernel.begin(
            "example-ios", "TASK-567", {"title": "second execution"}, command_id="run-2"
        )
        retried_second = self.kernel.begin(
            "example-ios", "TASK-567", {"title": "ignored retry"}, command_id="run-2"
        )
        self.assertEqual(second, retried_second)
        self.assertEqual("TASK-567", self.ledger.current(first)["execution_key"])
        self.assertEqual(1, self.ledger.current(first)["execution_number"])
        self.assertEqual("TASK-567-2", self.ledger.current(second)["execution_key"])
        self.assertEqual(2, self.ledger.current(second)["execution_number"])
        self.assertEqual("durable kernel", self.ledger.run_history(first)["intent"]["title"])
        self.assertEqual("second execution", self.ledger.run_history(second)["intent"]["title"])
        stored_intent = self.ledger.connection.execute(
            "SELECT intent_json FROM work_items WHERE id=?",
            (self.ledger.current(second)["work_item_id"],),
        ).fetchone()[0]
        self.assertEqual("second execution", json.loads(stored_intent)["title"])

    def test_begin_command_identity_includes_the_work_item(self):
        first = self.begin(command_id="begin")
        second = self.begin(command_id="begin", identifier="TASK-568")
        self.assertNotEqual(first, second)
        self.assertEqual("TASK-568", self.ledger.current(second)["execution_key"])

    def test_project_tracker_identity_is_immutable_and_unique(self):
        self.ledger.register_project(
            "example-ios", display_name="Renamed Example", tracker_kind="linear",
            tracker_project_id="linear-example", tracker_project_slug="renamed",
        )
        project = self.ledger.connection.execute(
            "SELECT display_name,tracker_project_id FROM projects WHERE project_key='example-ios'"
        ).fetchone()
        self.assertEqual(("Renamed Example", "linear-example"), tuple(project))
        with self.assertRaises(LedgerError):
            self.ledger.register_project(
                "example-ios", display_name="Wrong", tracker_kind="linear",
                tracker_project_id="another-project",
            )
        with self.assertRaises(LedgerError):
            self.ledger.register_project(
                "duplicate", display_name="Duplicate", tracker_kind="linear",
                tracker_project_id="linear-example",
            )

    def test_idempotent_command_does_not_duplicate_transition(self):
        execution = self.begin()
        first = self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="same-command",
        )
        second = self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="same-command",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(first["fence_token"], second["fence_token"])
        count = self.ledger.connection.execute(
            "SELECT COUNT(*) FROM transition_decisions"
        ).fetchone()[0]
        self.assertEqual(1, count)

    def test_work_requires_owner_and_completion_evidence(self):
        execution = self.begin()
        with self.assertRaises(LedgerError):
            self.kernel.transition(
                execution, "Autoplanning", actor="agent", signal="listener_claim",
                command_id="ownerless",
            )
        self.assertEqual("Todo", self.ledger.current(execution)["current_state_id"])
        claim = self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="owned",
        )
        with self.assertRaises(LedgerError):
            self.kernel.transition(
                execution, "Ready", actor="agent", signal="agent_handoff",
                attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
                outcome="succeeded", command_id="no-evidence",
            )
        current_run = self.ledger.connection.execute(
            "SELECT status FROM state_runs WHERE id=?",
            (self.ledger.current(execution)["current_state_run_id"],),
        ).fetchone()
        self.assertEqual("active", current_run["status"])

    def test_late_completion_is_fenced(self):
        execution = self.begin()
        claim = self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="claim",
        )
        self.kernel.transition(
            execution, "Canceled", actor="human", signal="linear_status_change",
            attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
            outcome="canceled", evidence=[{"kind": "decision", "uri": "linear://cancel"}],
            command_id="cancel",
        )
        with self.assertRaises((LedgerError, StaleAttempt)):
            self.kernel.transition(
                execution, "Ready", actor="agent", signal="agent_handoff",
                attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
                outcome="succeeded", evidence=[{"kind": "plan", "uri": "local://late"}],
                command_id="late",
            )

    def test_resource_lease_is_fenced_and_released_with_attempt(self):
        execution = self.begin()
        claim = self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="claim",
        )
        lease_id = self.ledger.acquire_resource(
            "repo:example-ios", attempt_id=claim["attempt_id"],
            fence_token=claim["fence_token"], expires_at="2099-01-01T00:00:00+00:00",
            idempotency_key="execution:{}:lease:repo".format(execution),
        )
        self.kernel.transition(
            execution, "Ready", actor="agent", signal="agent_handoff",
            attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
            outcome="succeeded", evidence=[{"kind": "plan", "uri": "local://plan"}],
            command_id="ready",
        )
        lease = self.ledger.connection.execute(
            "SELECT status,released_at FROM resource_leases WHERE id=?", (lease_id,)
        ).fetchone()
        self.assertEqual("released", lease["status"])
        self.assertIsNotNone(lease["released_at"])

    def test_expired_resource_lease_is_reclaimed_and_old_fence_stays_stale(self):
        first_execution = self.begin(command_id="first")
        first = self.kernel.transition(
            first_execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="first-claim",
        )
        first_lease = self.ledger.acquire_resource(
            "repo:example-ios", attempt_id=first["attempt_id"],
            fence_token=first["fence_token"], expires_at="2099-01-01T00:00:00+00:00",
            idempotency_key="first-lease",
        )
        self.ledger.connection.execute(
            "UPDATE resource_leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (first_lease,),
        )
        second_execution = self.begin(command_id="second", identifier="TASK-568")
        second = self.kernel.transition(
            second_execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-2", command_id="second-claim",
        )
        second_lease = self.ledger.acquire_resource(
            "repo:example-ios", attempt_id=second["attempt_id"],
            fence_token=second["fence_token"], expires_at="2099-01-01T00:00:00+00:00",
            idempotency_key="second-lease",
        )
        self.assertNotEqual(first_lease, second_lease)
        first_status = self.ledger.connection.execute(
            "SELECT status FROM resource_leases WHERE id=?", (first_lease,)
        ).fetchone()[0]
        self.assertEqual("expired", first_status)
        with self.assertRaises(StaleAttempt):
            self.ledger.heartbeat_resource(
                first_lease, fence_token=first["fence_token"],
                expires_at="2099-02-01T00:00:00+00:00", idempotency_key="old-heartbeat",
            )
        with self.assertRaises(StaleAttempt):
            self.ledger.release_resource(
                first_lease, fence_token=first["fence_token"],
                idempotency_key="old-release",
            )
        heartbeat = self.ledger.heartbeat_resource(
            second_lease, fence_token=second["fence_token"],
            expires_at="2099-02-01T00:00:00+00:00", idempotency_key="new-heartbeat",
        )
        self.assertEqual(second_lease, heartbeat["lease_id"])

    def test_premature_human_status_is_recorded_but_rejected(self):
        execution = self.begin()
        result = self.kernel.observe_linear_status(
            execution, "Review", command_id="linear-event-1", source_event_id="webhook-1"
        )
        self.assertEqual("rejected", result["payload"]["disposition"])
        current = self.ledger.current(execution)
        self.assertEqual("Todo", current["current_state_id"])
        self.assertEqual("Todo", current["desired_linear_status"])
        self.assertEqual("Review", current["observed_linear_status"])
        replay = self.kernel.observe_linear_status(
            execution, "Review", command_id="linear-event-1", source_event_id="webhook-1"
        )
        self.assertEqual(result["event_seq"], replay["seq"])
        events = self.ledger.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='linear_status_observed'"
        ).fetchone()[0]
        self.assertEqual(1, events)
        returned = self.kernel.observe_linear_status(
            execution, "Todo", command_id="linear-event-2", source_event_id="webhook-2"
        )
        self.assertEqual("no_change", returned["payload"]["disposition"])
        self.assertEqual("Todo", self.ledger.current(execution)["observed_linear_status"])
        events = self.ledger.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='linear_status_observed'"
        ).fetchone()[0]
        self.assertEqual(2, events)

    def test_review_feedback_is_required_and_handed_to_rework(self):
        execution = self.move_to_review()
        with self.assertRaises(LedgerError):
            self.kernel.transition(
                execution, "Reworking", actor="human", signal="structured_comment",
                owner="rework-agent", command_id="missing-feedback",
            )
        rework = self.kernel.transition(
            execution, "Reworking", actor="human", signal="structured_comment",
            owner="rework-agent", command_id="review-changes",
            feedback=[{
                "source": "linear", "kind": "changes_requested",
                "author": "reviewer-123", "body": "Handle the empty-state case.",
                "url": "linear://comment/review-1",
            }],
        )
        feedback = self.ledger.feedback_for_attempt(rework["attempt_id"])
        self.assertEqual(1, len(feedback))
        self.assertEqual("Handle the empty-state case.", feedback[0]["body"]["body"])
        history = self.ledger.run_history(execution)
        self.assertEqual("example-ios", history["project_key"])
        self.assertEqual("dotfactory-default", history["policy"]["workflow_name"])
        self.assertTrue(history["artifacts"])
        self.assertEqual("changes_requested", history["feedback"][0]["body"]["kind"])

    def test_linear_rework_waits_for_an_agent_and_then_hands_over_feedback(self):
        execution = self.move_to_review()
        pending = self.kernel.observe_linear_status(
            execution, "Reworking", command_id="linear-rework",
            source_event_id="webhook-rework",
            feedback=[{
                "source": "linear", "kind": "changes_requested",
                "author": "reviewer-123", "body": "Fix the retry copy.",
                "url": "linear://comment/rework",
            }],
        )
        self.assertEqual("pending", pending["disposition"])
        current = self.ledger.current(execution)
        self.assertEqual("Review", current["current_state_id"])
        self.assertEqual("Reworking", current["observed_linear_status"])
        self.assertIsNone(current["attempt"])
        claim = self.kernel.claim_pending_transition(
            execution, owner="rework-agent", command_id="claim-rework"
        )
        retried = self.kernel.claim_pending_transition(
            execution, owner="rework-agent", command_id="claim-rework"
        )
        self.assertEqual("Reworking", claim["to_state"])
        self.assertEqual(claim["id"], retried["id"])
        handed_over = self.ledger.feedback_for_attempt(claim["attempt_id"])
        self.assertEqual("Fix the retry copy.", handed_over[0]["body"]["body"])
        request = self.ledger.connection.execute(
            "SELECT status FROM transition_requests WHERE id=?", (pending["request_id"],)
        ).fetchone()[0]
        self.assertEqual("consumed", request)

    def test_human_can_reverse_a_split_second_rework_move(self):
        execution = self.move_to_review()
        pending = self.kernel.observe_linear_status(
            execution, "Reworking", command_id="linear-rework",
            feedback=[{
                "source": "linear", "kind": "changes_requested", "author": "reviewer-123",
                "body": "Actually, hold on.", "url": "linear://comment/hold",
            }],
        )
        restored = self.kernel.observe_linear_status(
            execution, "Review", command_id="linear-restored"
        )
        self.assertEqual("no_change", restored["payload"]["disposition"])
        request = self.ledger.connection.execute(
            "SELECT status FROM transition_requests WHERE id=?", (pending["request_id"],)
        ).fetchone()[0]
        self.assertEqual("canceled", request)
        with self.assertRaises(KernelError):
            self.kernel.claim_pending_transition(
                execution, owner="rework-agent", command_id="claim-canceled"
            )

    def test_an_accepted_review_decision_supersedes_an_older_pending_handoff(self):
        execution = self.move_to_review()
        pending = self.kernel.observe_linear_status(
            execution, "Reworking", command_id="pending-rework",
            feedback=[{
                "source": "linear", "kind": "changes_requested", "author": "reviewer-123",
                "body": "Check the copy.", "url": "linear://comment/copy",
            }],
        )
        done = self.kernel.observe_linear_status(
            execution, "Done", command_id="approved-instead",
            feedback=[{
                "source": "linear", "kind": "approval", "author": "reviewer-123",
                "body": "Approved after another look.", "url": "linear://review/approved",
            }],
        )
        self.assertEqual("Done", done["to_state"])
        request = self.ledger.connection.execute(
            "SELECT status FROM transition_requests WHERE id=?", (pending["request_id"],)
        ).fetchone()[0]
        self.assertEqual("superseded", request)
        history = self.ledger.run_history(execution)
        self.assertEqual("superseded", history["transition_requests"][0]["status"])

    def test_review_comment_can_arrive_before_the_status_change(self):
        execution = self.move_to_review()
        self.kernel.observe_linear_status(
            execution, "Review", command_id="review-comment",
            feedback=[{
                "source": "linear", "kind": "changes_requested", "author": "reviewer-123",
                "body": "Cover the empty response.", "url": "linear://comment/empty",
            }],
        )
        pending = self.kernel.observe_linear_status(
            execution, "Reworking", command_id="status-after-comment"
        )
        self.assertEqual("pending", pending["disposition"])
        claim = self.kernel.claim_pending_transition(
            execution, owner="rework-agent", command_id="claim-comment"
        )
        handed_over = self.ledger.feedback_for_attempt(claim["attempt_id"])
        self.assertEqual("Cover the empty response.", handed_over[0]["body"]["body"])

    def test_human_acceptance_requires_a_review_record(self):
        execution = self.move_to_review()
        with self.assertRaises(KernelError):
            self.kernel.transition(
                execution, "Done", actor="agent", signal="agent_handoff",
                command_id="agent-done",
            )
        rejected = self.kernel.observe_linear_status(
            execution, "Done", command_id="human-done-without-review",
            source_event_id="webhook-done-1",
        )
        self.assertEqual("rejected", rejected["payload"]["disposition"])
        self.assertEqual("Review", self.ledger.current(execution)["current_state_id"])
        accepted = self.kernel.observe_linear_status(
            execution, "Done", command_id="human-done-with-review",
            source_event_id="webhook-done-2",
            feedback=[{
                "source": "linear", "kind": "approval", "author": "reviewer-123",
                "body": "Approved in Linear review.", "url": "linear://review/approval-1",
            }],
        )
        self.assertEqual("Done", accepted["to_state"])
        self.assertEqual("completed", self.ledger.current(execution)["status"])

    def test_projection_failure_never_changes_local_state(self):
        execution = self.begin()
        before = self.ledger.current(execution)
        worker = ProjectionWorker(
            self.ledger, "logfire",
            lambda _: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        self.assertEqual(0, worker.drain())
        after = self.ledger.current(execution)
        self.assertEqual(before["current_state_id"], after["current_state_id"])
        pending = self.ledger.pending("logfire")
        self.assertEqual(1, len(pending))
        self.assertEqual(1, pending[0]["delivery_attempts"])

    def test_projection_replays_after_restart(self):
        execution = self.begin()
        worker = ProjectionWorker(
            self.ledger, "logfire",
            lambda _: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        self.assertEqual(0, worker.drain())
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")
        delivered = []
        self.assertEqual(
            1, ProjectionWorker(self.ledger, "logfire", delivered.append).drain()
        )
        self.assertEqual(execution, delivered[0]["execution_id"])
        self.assertEqual([], self.ledger.pending("logfire"))

    def test_delivered_projection_can_be_rebuilt_from_the_full_ledger(self):
        execution = self.move_to_review()
        self.kernel.transition(
            execution, "Reworking", actor="human", signal="structured_comment",
            owner="rework-agent", command_id="request-rework",
            feedback=[{
                "source": "linear", "kind": "changes_requested",
                "author": "reviewer-123", "body": "Cover the empty response.",
                "url": "linear://comment/rebuild",
            }],
        )
        original = RunProjection()
        delivered = ProjectionWorker(self.ledger, "logfire", original.apply).drain()
        self.assertGreater(delivered, 1)
        self.assertEqual([], self.ledger.pending("logfire"))

        rebuilt = RunProjection()
        result = ProjectionWorker(self.ledger, "logfire", rebuilt.apply).rebuild(
            command_id="rebuild-logfire-1", requested_by="operator:operator", batch_size=1
        )
        self.assertEqual(delivered, result["queued"])
        self.assertEqual(delivered, result["delivered"])
        self.assertEqual("completed", result["status"])
        original_snapshot = original.run(execution)
        snapshot = rebuilt.run(execution)
        for key in (
            "project_key", "work_item_identifier", "execution_key", "intent", "policy",
            "current_state", "evidence", "outcomes", "feedback", "work",
        ):
            self.assertEqual(original_snapshot[key], snapshot[key])
        self.assertEqual(
            [item["event_id"] for item in original_snapshot["events"]],
            [item["event_id"] for item in snapshot["events"]],
        )
        self.assertEqual("example-ios", snapshot["project_key"])
        self.assertEqual("durable kernel", snapshot["intent"]["title"])
        self.assertEqual("dotfactory-default", snapshot["policy"]["workflow_name"])
        self.assertEqual("Reworking", snapshot["current_state"])
        self.assertEqual("changes_requested", snapshot["feedback"][0]["kind"])
        self.assertTrue(snapshot["evidence"])
        self.assertTrue(snapshot["outcomes"])
        self.assertEqual(4, len(snapshot["work"]))
        for attempt in snapshot["work"][:3]:
            self.assertEqual("completed", attempt["status"])
            self.assertTrue(attempt["evidence"])
            self.assertEqual("succeeded", attempt["outcome"])
        self.assertEqual("Reworking", snapshot["work"][3]["state"])
        self.assertEqual("active", snapshot["work"][3]["status"])
        self.assertEqual(
            "changes_requested", snapshot["work"][3]["feedback"][0]["kind"]
        )
        self.assertEqual(
            len(snapshot["events"]),
            len({item["event_id"] for item in snapshot["events"]}),
        )
        rebuilt.apply(snapshot["events"][0])
        self.assertEqual(delivered, len(rebuilt.run(execution)["events"]))
        self.assertEqual([], self.ledger.pending("logfire"))

        with self.assertRaises(LedgerError):
            self.ledger.start_projection_rebuild(
                "logfire", command_id="invalid", requested_by="operator:operator",
                from_event_seq=0,
            )

    def test_same_event_id_has_identical_content_after_execution_advances(self):
        execution = self.begin()
        original = []
        self.assertEqual(
            1, ProjectionWorker(self.ledger, "logfire", original.append).drain()
        )
        self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="claim",
        )
        rebuilt = []
        ProjectionWorker(self.ledger, "logfire", rebuilt.append).rebuild(
            command_id="stable-envelope", requested_by="operator:operator"
        )
        replayed_start = next(
            item for item in rebuilt if item["event_id"] == original[0]["event_id"]
        )
        self.assertEqual(original[0], replayed_start)

    def test_projection_rebuild_resumes_after_failure_without_redelivering(self):
        execution = self.begin()
        self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="claim",
        )
        ProjectionWorker(self.ledger, "logfire", lambda _: None).drain()
        seen = []

        def fail_second(envelope):
            if len(seen) == 1:
                raise RuntimeError("projection offline")
            seen.append(envelope["event_id"])

        first = ProjectionWorker(self.ledger, "logfire", fail_second).rebuild(
            command_id="rebuild-after-loss", requested_by="operator:operator", batch_size=10
        )
        self.assertEqual(1, first["delivered"])
        self.assertEqual(1, first["pending"])
        self.assertEqual("paused", first["status"])

        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")
        second = ProjectionWorker(
            self.ledger, "logfire", lambda envelope: seen.append(envelope["event_id"])
        ).rebuild(
            command_id="rebuild-after-loss", requested_by="operator:operator", batch_size=10
        )
        self.assertEqual(2, second["delivered"])
        self.assertEqual(0, second["pending"])
        self.assertEqual("completed", second["status"])
        self.assertEqual("operator:operator", second["requested_by"])
        self.assertEqual(2, len(seen))
        self.assertEqual(2, len(set(seen)))

        replayed = ProjectionWorker(
            self.ledger, "logfire", lambda envelope: seen.append(envelope["event_id"])
        ).rebuild(
            command_id="rebuild-after-loss", requested_by="operator:operator", batch_size=10
        )
        self.assertEqual(0, replayed["delivered_this_call"])
        self.assertEqual(2, len(seen))
        failed_item = self.ledger.connection.execute(
            "SELECT delivery_attempts,last_error FROM projection_replay_items "
            "WHERE replay_id=? ORDER BY event_seq DESC LIMIT 1", (second["id"],)
        ).fetchone()
        self.assertEqual(2, failed_item["delivery_attempts"])
        self.assertEqual("projection offline", failed_item["last_error"])
        with self.assertRaises(LedgerError):
            self.ledger.start_projection_rebuild(
                "logfire", command_id="rebuild-after-loss", requested_by="operator:other"
            )
        self.assertEqual(execution, self.ledger.current(execution)["id"])

    def test_export_is_ordered_and_contains_no_credentials(self):
        execution = self.begin()
        claim = self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-1", command_id="claim",
        )
        self.kernel.transition(
            execution, "Ready", actor="agent", signal="agent_handoff",
            attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
            outcome="succeeded",
            evidence=[{"kind": "plan", "uri": "local://plan", "api_token": "private"}],
            command_id="ready",
        )
        target = Path(self.temp.name) / "events.jsonl"
        self.ledger.export_jsonl(target)
        rows = [json.loads(line) for line in target.read_text().splitlines()]
        self.assertEqual(sorted(row["seq"] for row in rows), [row["seq"] for row in rows])
        self.assertNotIn("private", target.read_text())
        self.assertIn("[REDACTED]", target.read_text())

    def test_raw_provider_events_are_separate_and_redacted(self):
        stream = RawEventStream(Path(self.temp.name) / "raw")
        path = stream.append("execution", "attempt", {"message": "ok", "api_key": "private"})
        self.assertNotEqual(self.path, path)
        self.assertNotIn("private", path.read_text())
        self.assertIn("[REDACTED]", path.read_text())

    def test_ledger_uses_wal(self):
        mode = self.ledger.connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual("wal", mode)

    def test_public_factory_config_has_a_project_registry(self):
        config = FactoryConfig.load(ROOT / "factory.example.json")
        self.assertEqual("example-local", config.values["factory_id"])
        self.assertEqual(5, config.values["schema_version"])
        self.assertEqual(("example-ios", "example-service"), config.project_keys)
        workflow = config.resolve_workflow("example-service")
        self.assertEqual("three-step", workflow["name"])
        self.assertTrue(workflow["path"].endswith("factory/workflows/three-step.dot"))
        self.assertEqual(0, len(workflow["profile_paths"]))
        self.assertEqual(("example-ios",), config.selected_project_keys())
        self.assertEqual(("example-service",), config.selected_project_keys(["example-service"]))
        preparation = config.resolve_preparation("example-ios", environment={
            "DOTFACTORY_EXAMPLE_IOS_REPOSITORY": "/tmp/example-ios",
        })
        self.assertEqual("0.15.6", preparation["providers"]["portless"]["version"])
        self.assertEqual(
            str(Path("/tmp/example-ios/.worktrees").resolve()),
            preparation["workspace"]["root"],
        )
        self.assertEqual(("local-web",), config.validate_resource_names(
            "example-ios", ["local-web", "local-web"]
        ))
        with self.assertRaisesRegex(FactoryConfigError, "unknown capabilities"):
            config.validate_resource_names("example-ios", ["ios-simulator"])
        resolved = config.resolve_project("example-ios", environment={
            "DOTFACTORY_EXAMPLE_IOS_REPOSITORY": "/tmp/example-ios",
            "DOTFACTORY_EXAMPLE_IOS_LINEAR_PROJECT_ID": "linear-example-ios",
        })
        self.assertEqual(
            str(Path("/tmp/example-ios").resolve()), resolved["repository_path"]
        )
        self.assertEqual("linear-example-ios", resolved["tracker_project_id"])
        configured_ledger = SQLiteLedger(Path(self.temp.name) / "configured.db")
        config.configure_ledger(configured_ledger, environment={
            "DOTFACTORY_EXAMPLE_IOS_REPOSITORY": "/tmp/example-ios",
            "DOTFACTORY_EXAMPLE_IOS_LINEAR_PROJECT_ID": "linear-example-ios",
        })
        identity = configured_ledger.connection.execute(
            "SELECT factory_id FROM factory_identity"
        ).fetchone()[0]
        self.assertEqual("example-local", identity)
        self.assertEqual(
            1, configured_ledger.connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        )
        configured_ledger.close()
        with self.assertRaises(FactoryConfigError):
            config.selected_project_keys(["missing"])
        unsafe = Path(self.temp.name) / "unsafe.json"
        unsafe.write_text(json.dumps({
            "schema_version": 2, "factory_id": "bad", "ledger_path": "factory.db",
            "workflow_path": "workflow.json", "projects": {},
            "logfire_token": "private",
        }))
        with self.assertRaises(FactoryConfigError):
            FactoryConfig.load(unsafe)


class LedgerMigrationTests(unittest.TestCase):
    def test_schema_five_adds_immutable_workflow_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema-five.db"
            db = sqlite3.connect(path)
            db.execute("PRAGMA user_version=5")
            db.close()
            ledger = SQLiteLedger(path)
            tables = {
                row[0] for row in ledger.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue({
                "workflow_snapshots", "execution_workflow_snapshots", "attempt_bindings"
            }.issubset(tables))
            self.assertEqual(
                8, ledger.connection.execute("PRAGMA user_version").fetchone()[0]
            )
            ledger.close()

    def test_schema_three_adds_audited_projection_rebuild_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema-three.db"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE outbox (id TEXT PRIMARY KEY);
                PRAGMA user_version=3;
                """
            )
            db.close()
            ledger = SQLiteLedger(path)
            columns = {
                row["name"] for row in ledger.connection.execute(
                    "PRAGMA table_info(projection_replays)"
                )
            }
            self.assertIn("requested_by", columns)
            self.assertIn("through_event_seq", columns)
            self.assertEqual(8, ledger.connection.execute("PRAGMA user_version").fetchone()[0])
            ledger.close()

    def test_schema_one_gains_project_execution_and_exact_status_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE work_items (
                    id TEXT PRIMARY KEY, identifier TEXT NOT NULL UNIQUE,
                    intent_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE workflow_executions (
                    id TEXT PRIMARY KEY, work_item_id TEXT NOT NULL REFERENCES work_items(id),
                    workflow_name TEXT NOT NULL, workflow_version INTEGER NOT NULL,
                    status TEXT NOT NULL, current_state_id TEXT NOT NULL,
                    desired_linear_status TEXT NOT NULL, observed_linear_status TEXT,
                    current_state_run_id TEXT, created_at TEXT NOT NULL, completed_at TEXT
                );
                CREATE TABLE state_runs (
                    id TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
                    state_id TEXT NOT NULL, state_kind TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL, resume_state_id TEXT, started_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE transition_decisions (
                    id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, edge_id TEXT NOT NULL,
                    from_state TEXT NOT NULL, to_state TEXT NOT NULL, actor TEXT NOT NULL,
                    signal TEXT NOT NULL, desired_linear_status TEXT NOT NULL,
                    event_seq INTEGER NOT NULL, decided_at TEXT NOT NULL
                );
                CREATE TABLE resource_leases (
                    id TEXT PRIMARY KEY, resource_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, fence_token TEXT NOT NULL,
                    status TEXT NOT NULL, acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL, released_at TEXT
                );
                PRAGMA user_version=1;
                """
            )
            db.execute("INSERT INTO work_items VALUES('work','TASK-567','{}','2026-01-01')")
            db.execute(
                "INSERT INTO workflow_executions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("execution", "work", "default", 1, "running", "auto_planning",
                 "Autoplanning", None, "run", "2026-01-01", None),
            )
            db.execute(
                "INSERT INTO state_runs VALUES(?,?,?,?,?,?,?,?,?)",
                ("run", "execution", "auto_planning", "work", 1, "active", None,
                 "2026-01-01", None),
            )
            db.commit()
            db.close()

            ledger = SQLiteLedger(path)
            row = ledger.connection.execute(
                "SELECT execution_number,execution_key,current_state_id "
                "FROM workflow_executions WHERE id='execution'"
            ).fetchone()
            self.assertEqual((1, "TASK-567", "Autoplanning"), tuple(row))
            foreign_key_targets = {
                row["table"] for row in ledger.connection.execute(
                    "PRAGMA foreign_key_list(workflow_executions)"
                )
            }
            self.assertIn("work_items", foreign_key_targets)
            project = ledger.connection.execute(
                "SELECT project_key FROM work_items WHERE id='work'"
            ).fetchone()[0]
            self.assertEqual("legacy", project)
            ledger.configure_factory("migrated-test")
            ledger.register_project(
                "second", display_name="Second", tracker_kind="linear",
                tracker_project_id="linear-second",
            )
            ledger.connection.execute(
                "INSERT INTO work_items(id,project_key,identifier,intent_json,created_at) "
                "VALUES(?,?,?,?,?)",
                ("second-work", "second", "TASK-567", "{}", "2026-01-02"),
            )
            count = ledger.connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE identifier='TASK-567'"
            ).fetchone()[0]
            self.assertEqual(2, count)
            self.assertEqual(8, ledger.connection.execute("PRAGMA user_version").fetchone()[0])
            ledger.close()


if __name__ == "__main__":
    unittest.main()
