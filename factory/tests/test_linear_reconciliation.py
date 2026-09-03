import tempfile
import unittest
from pathlib import Path

from dotfactory import DurableKernel, SQLiteLedger
from dotfactory.ledger import LedgerError
from dotfactory.linear_reconciliation import (
    LinearContractError,
    LinearObservationV1,
    LinearReconciler,
    LinearStatusBindingV1,
    LinearTrackerPolicyV1,
    content_hash,
    poll_observation_key,
)


ROOT = Path(__file__).resolve().parents[1]


class LinearReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "factory.db"
        self.ledger = SQLiteLedger(self.path)
        self.ledger.configure_factory("test-local")
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="linear-dotfactory",
        )
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")
        self.execution = self.kernel.begin(
            "dotfactory", "TASK-574", {"title": "reconcile Linear"},
            command_id="begin",
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def bind(self, status_name="Todo", status_id="status-todo"):
        digest = self.ledger.current(self.execution)["workflow_digest"]
        binding = LinearStatusBindingV1(
            project_key="dotfactory", workflow_digest=digest,
            team_id="team-implicit", status_id=status_id,
            status_name=status_name, status_type="unstarted",
        )
        return self.ledger.bind_linear_statuses(
            "dotfactory", digest, "team-implicit", [binding]
        )[0]

    def observation(self, *, status_name="Todo", status_id="status-todo"):
        payload = {"issue": "issue-574", "status": status_id, "updated": "remote-1"}
        return LinearObservationV1(
            execution_id=self.execution, project_key="dotfactory",
            issue_id="issue-574", issue_identifier="TASK-574",
            status_id=status_id, status_name=status_name,
            remote_updated_at="remote-1", observed_at="2026-08-30T12:00:00+00:00",
            payload_hash=content_hash(payload), source="poll",
            observation_key=poll_observation_key("issue-574", "remote-1", status_id),
        )

    def test_begin_queues_exact_status_mutation_atomically(self):
        mutations = self.ledger.pending_linear_mutations()
        self.assertEqual(1, len(mutations))
        self.assertEqual("Todo", mutations[0]["desired_status"])
        self.assertEqual("TASK-574", mutations[0]["request"]["issue_identifier"])
        self.assertNotIn("token", mutations[0]["request"])

    def test_binding_and_observation_are_idempotent_but_identity_reuse_is_rejected(self):
        first = self.bind()
        self.assertEqual(first, self.bind())
        digest = self.ledger.current(self.execution)["workflow_digest"]
        with self.assertRaisesRegex(LedgerError, "missing Linear status bindings: Ready"):
            self.ledger.require_linear_status_bindings(
                "dotfactory", digest, ["Todo", "Ready"]
            )
        reconciler = LinearReconciler(self.ledger, self.kernel)
        observation = self.observation()
        ingested = reconciler.ingest(observation)
        self.assertEqual(ingested["id"], reconciler.ingest(observation)["id"])
        changed = dict(observation.as_dict())
        changed["status_name"] = "Planning"
        with self.assertRaisesRegex(LedgerError, "reused with new content"):
            self.ledger.record_linear_observation_input(changed)

    def test_reconciler_accepts_bound_no_change_and_marks_observation(self):
        self.bind()
        observation = LinearReconciler(self.ledger, self.kernel).ingest(self.observation())
        result = LinearReconciler(self.ledger, self.kernel).reconcile(
            observation["id"], current_status_id="status-todo",
            current_remote_updated_at="remote-1",
        )
        self.assertEqual("no_change", result["payload"]["disposition"])
        stored = self.ledger.linear_observation(observation["id"])
        self.assertEqual("no_change", stored["disposition"])
        self.assertIsNotNone(stored["event_seq"])
        mutation = self.ledger.connection.execute(
            "SELECT status FROM linear_mutations ORDER BY event_seq LIMIT 1"
        ).fetchone()
        self.assertEqual("confirmed", mutation["status"])

    def test_transition_source_is_distinct_from_edge_actor(self):
        result = self.kernel.transition(
            self.execution, "Autoplanning", actor="agent", signal="listener_claim",
            source_kind="system", owner="planner", command_id="scheduler-claim",
        )
        decision = self.ledger.connection.execute(
            "SELECT actor,source_kind FROM transition_decisions WHERE id=?",
            (result["id"],),
        ).fetchone()
        self.assertEqual("agent", decision["actor"])
        self.assertEqual("system", decision["source_kind"])

    def test_stale_observation_is_recorded_and_corrective_mutation_is_durable(self):
        observation = LinearReconciler(self.ledger, self.kernel).ingest(
            self.observation(status_name="Planning", status_id="status-planning")
        )
        result = LinearReconciler(self.ledger, self.kernel).reconcile(
            observation["id"], current_status_id="status-ready",
            current_remote_updated_at="remote-2",
        )
        self.assertEqual("stale", result["disposition"])
        rows = self.ledger.connection.execute(
            "SELECT desired_status,status FROM linear_mutations ORDER BY event_seq"
        ).fetchall()
        self.assertEqual(["Todo", "Todo"], [row["desired_status"] for row in rows])

    def test_ambiguous_send_recovers_without_automatic_resend(self):
        mutation = self.ledger.pending_linear_mutations()[0]
        attempt = self.ledger.start_linear_mutation_attempt(mutation["id"])
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        self.assertEqual(1, self.ledger.recover_linear_mutations())
        self.assertEqual([], self.ledger.pending_linear_mutations())
        status = self.ledger.connection.execute(
            "SELECT status FROM linear_mutation_attempts WHERE id=?",
            (attempt["attempt_id"],),
        ).fetchone()["status"]
        self.assertEqual("ambiguous", status)
        exported = self.ledger.linear_reconciliation_records(self.execution)
        self.assertEqual(1, len(exported["mutations"]))
        self.assertEqual(1, len(exported["attempts"]))

    def test_confirmation_requires_exact_remote_status_and_redacts_errors(self):
        mutation = self.ledger.pending_linear_mutations()[0]
        attempt = self.ledger.start_linear_mutation_attempt(mutation["id"])
        result = self.ledger.confirm_linear_mutation(
            mutation["id"], attempt["attempt_id"], observed_status="Planning",
            response={"status": "Planning", "authorization": "secret"},
        )
        self.assertEqual("conflict", result["status"])
        response_hash = self.ledger.connection.execute(
            "SELECT response_hash FROM linear_mutation_attempts WHERE id=?",
            (attempt["attempt_id"],),
        ).fetchone()["response_hash"]
        self.assertEqual(
            content_hash({"authorization": "[REDACTED]", "status": "Planning"}),
            response_hash,
        )

    def test_tracker_policy_keeps_native_fields_out_of_labels(self):
        with self.assertRaisesRegex(LinearContractError, "native field status"):
            LinearTrackerPolicyV1(allowed_labels={"status": ("Ready",)}, runner_overrides={})
        policy = LinearTrackerPolicyV1(
            allowed_labels={"runner": ("runner:claude",)},
            runner_overrides={"runner:claude": "claude"},
        )
        self.assertEqual(
            "claude", policy.runner_for_labels(
                ["runner:claude"], configured_runners={"claude"}
            )
        )


class LinearAtomicityTests(unittest.TestCase):
    def test_fault_after_mutation_enqueue_rolls_back_the_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factory.db"
            ledger = SQLiteLedger(path)
            ledger.configure_factory("test-local")
            ledger.register_project(
                "dotfactory", display_name="dotfactory", tracker_kind="linear",
                tracker_project_id="linear-dotfactory",
            )
            kernel = DurableKernel(ledger, ROOT / "workflows" / "default.dot")
            ledger.fault_hook = lambda boundary: (
                (_ for _ in ()).throw(RuntimeError("fault"))
                if boundary == "after_linear_mutation_queued" else None
            )
            with self.assertRaisesRegex(RuntimeError, "fault"):
                kernel.begin(
                    "dotfactory", "TASK-574", {"title": "atomic"},
                    command_id="begin",
                )
            self.assertEqual(0, ledger.connection.execute(
                "SELECT COUNT(*) FROM workflow_executions"
            ).fetchone()[0])
            self.assertEqual(0, ledger.connection.execute(
                "SELECT COUNT(*) FROM linear_mutations"
            ).fetchone()[0])
            ledger.close()


if __name__ == "__main__":
    unittest.main()
