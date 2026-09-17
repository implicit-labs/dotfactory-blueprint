import copy
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory import ControlService, FactoryConfig, FactoryRuntime, Principal  # noqa: E402
from dotfactory.cli import _demo_config  # noqa: E402
from dotfactory.lifecycle import fixture_runner  # noqa: E402


class TerminalRestartRecoveryTests(unittest.TestCase):
    def test_terminal_execution_gets_one_deterministic_numbered_restart(self):
        with tempfile.TemporaryDirectory(prefix="df-terminal-restart-") as directory:
            root = Path(directory)
            config = FactoryConfig.load(_demo_config(root))
            with FactoryRuntime(config, runner=fixture_runner(3)) as runtime:
                original = runtime.kernels["demo"].begin(
                    "demo",
                    "DEMO-RECOVER",
                    {
                        "title": "Legacy incompatible plan",
                        "description": "Replan after terminal completion",
                        "source": "explicit_issue",
                    },
                    command_id="runtime-begin:demo:DEMO-RECOVER",
                )
                self.assertEqual(
                    original,
                    runtime.start_issue("demo", "DEMO-RECOVER"),
                )
                runtime.run([original], max_ticks=1)
                self.assertEqual("Ready", runtime.ledger.current(original)["current_state_id"])

                original_workspace = runtime.ledger.workspace_for_execution(original)
                self.assertIsNotNone(original_workspace)
                runtime.ledger.set_workspace_cleanup_policy(
                    original,
                    owner_token=runtime.engines["demo"].owner_token,
                    policy="retain",
                    command_id="retain-frozen-recovery-evidence",
                )
                original_path = Path(original_workspace["path"])
                original_source = (original_path / "README.md").read_bytes()

                control = ControlService(runtime.ledger, runtime.kernels["demo"])
                control.execute(
                    original,
                    command_id="cancel-incompatible-plan",
                    principal=Principal("operator", "operator", "test"),
                    request={
                        "action": "cancel",
                        "expected_state": "Ready",
                        "confirmed": True,
                        "parameters": {"reason": "Replan under the current host policy"},
                    },
                )
                frozen_history = copy.deepcopy(runtime.ledger.run_history(original))

                replacement = runtime.start_issue("demo", "DEMO-RECOVER")
                self.assertNotEqual(original, replacement)
                self.assertEqual(2, runtime.ledger.current(replacement)["execution_number"])
                self.assertEqual(
                    "DEMO-RECOVER-2",
                    runtime.ledger.current(replacement)["execution_key"],
                )

            with FactoryRuntime(config, runner=fixture_runner(3)) as runtime:
                retry = runtime.start_issue("demo", "DEMO-RECOVER")
                self.assertEqual(replacement, retry)
                receipt = runtime.run([retry], max_ticks=10)
                self.assertEqual("Review", receipt.executions[0]["current_state"])
                replacement_workspace = runtime.ledger.workspace_for_execution(retry)
                self.assertIsNotNone(replacement_workspace)
                self.assertNotEqual(original_workspace["path"], replacement_workspace["path"])

                active_retry = runtime.start_issue("demo", "DEMO-RECOVER")
                self.assertEqual(retry, active_retry)
                self.assertEqual(
                    frozen_history,
                    runtime.ledger.run_history(original),
                )
                self.assertEqual(original_source, (original_path / "README.md").read_bytes())
                self.assertTrue(original_path.is_dir())

                executions = runtime.ledger.connection.execute(
                    "SELECT execution_number FROM workflow_executions we "
                    "JOIN work_items wi ON wi.id=we.work_item_id "
                    "WHERE wi.project_key=? AND wi.identifier=? "
                    "ORDER BY execution_number",
                    ("demo", "DEMO-RECOVER"),
                ).fetchall()
                self.assertEqual([1, 2], [row["execution_number"] for row in executions])


if __name__ == "__main__":
    unittest.main()
