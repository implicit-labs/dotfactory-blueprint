import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotfactory import FactoryConfig, FactoryRuntime, SQLiteLedger
from dotfactory.cli import _demo_config, main
from dotfactory.control import Principal
from dotfactory.lifecycle import fixture_runner


class FactoryCLITests(unittest.TestCase):
    def test_attention_records_control_without_starting_a_run(self):
        service = MagicMock()
        service.execute.return_value = {
            "status": "completed", "result": {"remedy": "retry"},
        }
        runtime = MagicMock()
        runtime.control_service.return_value = service
        context = MagicMock()
        context.__enter__.return_value = runtime
        output = io.StringIO()
        with patch(
            "dotfactory.cli.FactoryConfig.load", return_value="config"
        ) as load:
            with patch(
                "dotfactory.cli.FactoryRuntime", return_value=context
            ) as factory:
                with redirect_stdout(output):
                    result = main([
                        "attention", "--config", "/tmp/factory.json",
                        "--project", "example", "--execution", "execution-1",
                        "--attention-id", "attention-1",
                        "--expected-state", "Investigating",
                        "--expected-attempt", "attempt-1", "--remedy", "retry",
                        "--command-id", "operator:attention-1:retry",
                        "--subject", "operator@example.test",
                    ])
        self.assertEqual(0, result)
        load.assert_called_once_with("/tmp/factory.json")
        factory.assert_called_once_with(
            "config", project_keys=["example"], control_only=True,
        )
        runtime.control_service.assert_called_once_with("example")
        service.execute.assert_called_once_with(
            "execution-1", command_id="operator:attention-1:retry",
            principal=Principal("operator@example.test", "operator", "cli"),
            request={
                "action": "attention", "expected_state": "Investigating",
                "parameters": {
                    "attention_id": "attention-1", "remedy": "retry",
                    "expected_attempt_id": "attempt-1",
                },
            },
        )
        self.assertEqual("completed", json.loads(output.getvalue())["status"])
        runtime.run.assert_not_called()

    def test_attention_cli_cannot_mutate_an_execution_from_another_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _demo_config(root)
            values = json.loads(config_path.read_text(encoding="utf-8"))
            foreign = json.loads(json.dumps(values["projects"]["demo"]))
            foreign["display_name"] = "Other lifecycle"
            foreign["tracker"]["project_id"] = "other-project"
            values["projects"]["other"] = foreign
            values["scheduler"]["limits"]["projects"]["other"] = 1
            config_path.write_text(
                json.dumps(values, indent=2) + "\n", encoding="utf-8"
            )
            config = FactoryConfig.load(config_path)
            with FactoryRuntime(config, runner=fixture_runner()) as runtime:
                execution = runtime.start_issue("other", "OTHER-CLI-1")
                attention = runtime.ledger.open_attention(
                    execution_id=execution, attempt_id=None, preparation_id=None,
                    dedupe_key="foreign-cli-attention", category="foreign",
                    provider="scheduler", detail={"allowed_actions": ["retry"]},
                )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = main([
                    "attention", "--config", str(config_path),
                    "--project", "demo", "--execution", execution,
                    "--attention-id", attention["id"],
                    "--expected-state", "Backlog",
                    "--expected-attempt", "foreign-attempt",
                    "--remedy", "retry",
                    "--command-id", "operator:foreign-cli:retry",
                ])
            self.assertEqual(1, result)
            self.assertEqual("", stdout.getvalue())
            self.assertIn(
                "execution is not available in project demo", stderr.getvalue()
            )
            self.assertNotIn("Other lifecycle", stderr.getvalue())
            ledger = SQLiteLedger(Path(values["ledger_path"]))
            try:
                self.assertEqual("open", ledger.attention(attention["id"])["status"])
                self.assertEqual(0, ledger.connection.execute(
                    "SELECT COUNT(*) FROM control_commands WHERE execution_id=?",
                    (execution,),
                ).fetchone()[0])
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
