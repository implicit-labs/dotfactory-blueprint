import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory import (  # noqa: E402
    ControlError, ControlService, FactoryConfig, FactoryRuntime, LifecycleError,
    Principal,
)
from dotfactory.cli import _demo_config  # noqa: E402
from dotfactory.lifecycle import fixture_runner  # noqa: E402


class FactoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = FactoryConfig.load(_demo_config(self.root))

    def tearDown(self):
        self.temp.cleanup()

    def test_one_shot_reaches_human_review_with_one_trace_and_receipt(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1", title="Toy run")
            receipt = runtime.run([execution], max_ticks=20)
            current = runtime.ledger.current(execution)
            trace = runtime.ledger.trace_page(execution, limit=1000)
            self.assertEqual("Review", current["current_state_id"])
            self.assertEqual("Review", receipt.executions[0]["current_state"])
            self.assertEqual(1, len({item["trace_id"] for item in trace}))
            self.assertTrue(receipt.executions[0]["trace"]["complete"])
            self.assertGreater(receipt.executions[0]["trace"]["open_span_count"], 0)
            self.assertEqual(receipt.executions[0]["trace"]["through_seq"], trace[-1]["seq"])
            self.assertEqual(64, len(receipt.digest))
            expected_safe = not ((3, 51, 0) <= sqlite3.sqlite_version_info < (3, 51, 3))
            self.assertFalse(receipt.concurrent_writers_allowed)
            self.assertEqual(expected_safe, receipt.sqlite_concurrency_safe)

    def test_restart_reuses_the_running_execution_without_duplicate_work(self):
        first_runtime = FactoryRuntime(self.config, runner=fixture_runner())
        execution = first_runtime.start_issue("demo", "DEMO-1")
        first_runtime.run([execution], max_ticks=20)
        event_count = first_runtime.ledger.connection.execute(
            "SELECT COUNT(*) FROM events WHERE execution_id=?", (execution,)
        ).fetchone()[0]
        first_runtime.close()

        with FactoryRuntime(self.config, runner=fixture_runner()) as restarted:
            same = restarted.start_issue("demo", "DEMO-1")
            receipt = restarted.run([same], max_ticks=2)
            self.assertEqual(execution, same)
            self.assertEqual("Review", receipt.executions[0]["current_state"])
            self.assertEqual(event_count, restarted.ledger.connection.execute(
                "SELECT COUNT(*) FROM events WHERE execution_id=?", (same,)
            ).fetchone()[0])

    def test_terminal_transition_cleans_only_the_owned_clean_workspace(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.run([execution], max_ticks=20)
            workspace = runtime.ledger.workspace_for_execution(execution)
            self.assertTrue(Path(workspace["path"]).is_dir())
            control = ControlService(runtime.ledger, runtime.kernels["demo"])
            receipt = control.execute(
                execution, command_id="approve-demo",
                principal=Principal("operator", "approver", "test"),
                request={
                    "action": "approve", "expected_state": "Review",
                    "parameters": {"note": "Toy evidence accepted."},
                },
            )
            self.assertEqual("completed", receipt["status"])
            step = runtime.step()
            self.assertEqual("ready", step["cleanup"][0]["disposition"])
            self.assertFalse(Path(workspace["path"]).exists())
            self.assertEqual(
                "cleaned", runtime.ledger.workspace_for_execution(execution)["status"]
            )

    def test_second_process_fails_closed_on_instance_lock(self):
        with FactoryRuntime(self.config, runner=fixture_runner()):
            with self.assertRaisesRegex(LifecycleError, "another dotfactory process"):
                FactoryRuntime(self.config, runner=fixture_runner())

    def test_composed_control_routes_scheduler_attention_to_scheduler(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            control = runtime.control_service("demo")
            self.assertIs(
                runtime.scheduler, control.attention_controllers["scheduler"]
            )
            self.assertIs(runtime.engines["demo"], control.resource_controller)

    def test_linear_drain_reloads_outbox_after_each_confirmation(self):
        first = {"id": "first", "execution_id": "execution"}
        duplicate = {"id": "duplicate", "execution_id": "execution"}

        class Ledger:
            def __init__(self):
                self.pending = [first, duplicate]

            def pending_linear_mutations(self, _limit):
                return list(self.pending)

            def current(self, _execution_id):
                return {"project_key": "demo"}

        class Worker:
            def __init__(self, ledger):
                self.ledger = ledger
                self.delivered = []

            def drain_one(self, mutation):
                self.delivered.append(mutation["id"])
                # Observing the first confirmation also confirms its duplicate.
                self.ledger.pending = []

        runtime = object.__new__(FactoryRuntime)
        runtime.ledger = Ledger()
        worker = Worker(runtime.ledger)
        runtime.linear_workers = {"demo": worker}

        runtime._drain_linear()

        self.assertEqual(["first"], worker.delivered)

    def test_project_bound_control_rejects_a_foreign_execution_before_writing(self):
        values = json.loads(self.config.path.read_text(encoding="utf-8"))
        foreign = json.loads(json.dumps(values["projects"]["demo"]))
        foreign["display_name"] = "Other lifecycle"
        foreign["tracker"]["project_id"] = "other-project"
        values["projects"]["other"] = foreign
        values["scheduler"]["limits"]["projects"]["other"] = 1
        config_path = self.root / "project-bound.json"
        config_path.write_text(
            json.dumps(values, indent=2) + "\n", encoding="utf-8"
        )
        config = FactoryConfig.load(config_path)
        with FactoryRuntime(config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("other", "OTHER-1")
            attention = runtime.ledger.open_attention(
                execution_id=execution, attempt_id=None, preparation_id=None,
                dedupe_key="foreign-attention", category="foreign",
                provider="scheduler", detail={"allowed_actions": ["retry"]},
            )
            control = runtime.control_service("demo")
            with self.assertRaises(ControlError) as raised:
                control.execute(
                    execution, command_id="foreign-attention",
                    principal=Principal("operator@example.test", "operator", "test"),
                    request={
                        "action": "attention", "expected_state": "Backlog",
                        "parameters": {
                            "attention_id": "foreign-attention",
                            "remedy": "retry", "expected_attempt_id": "foreign",
                        },
                    },
                )
            self.assertEqual("execution_not_found", raised.exception.code)
            self.assertEqual(404, raised.exception.status)
            self.assertIn("is not available in project demo", str(raised.exception))
            self.assertNotIn("other", str(raised.exception))
            self.assertEqual(
                "open", runtime.ledger.attention(attention["id"])["status"]
            )
            self.assertEqual(0, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM control_commands WHERE execution_id=?",
                (execution,),
            ).fetchone()[0])

    def test_control_only_runtime_skips_runner_and_linear_preflights(self):
        control_root = self.root / "control-only"
        control_root.mkdir()
        config_path = _demo_config(control_root)
        values = json.loads(config_path.read_text(encoding="utf-8"))
        values["runners"]["codex"]["command"] = "missing-runner-command"
        values["projections"]["linear"]["enabled"] = True
        values["projects"]["demo"]["tracker"]["team_id"] = "demo-team"
        config_path.write_text(
            json.dumps(values, indent=2) + "\n", encoding="utf-8"
        )
        config = FactoryConfig.load(config_path)
        with FactoryRuntime(
            config, project_keys=["demo"], control_only=True, environment={},
        ) as runtime:
            reasons = {item["kind"]: item["reason"] for item in runtime.preflights}
            self.assertEqual(
                "external runner preflight skipped", reasons["runner"]
            )
            self.assertEqual(
                "external projection preflight skipped", reasons["linear"]
            )
            self.assertIs(
                runtime.scheduler,
                runtime.control_service("demo").attention_controllers["scheduler"],
            )


if __name__ == "__main__":
    unittest.main()
