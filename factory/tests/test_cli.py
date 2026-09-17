import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotfactory import FactoryConfig, FactoryRuntime, SQLiteLedger
from dotfactory.cli import _demo_config, _run_exit_code, main
from dotfactory.control import Principal
from dotfactory.lifecycle import fixture_runner


class FactoryCLITests(unittest.TestCase):
    def _run_with_description(self, description_file=None):
        runtime = MagicMock()
        runtime.start_issue.return_value = "execution-1"
        runtime.run.return_value.as_dict.return_value = {}
        context = MagicMock()
        context.__enter__.return_value = runtime
        arguments = [
            "run", "--config", "/tmp/factory.json", "--project", "demo",
            "--issue", "DEMO-1",
        ]
        if description_file is not None:
            arguments.extend(("--description-file", str(description_file)))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "dotfactory.cli.FactoryConfig.load", return_value="config"
        ), patch(
            "dotfactory.cli.FactoryRuntime", return_value=context
        ) as factory, patch(
            "dotfactory.cli._install_signals"
        ), patch(
            "dotfactory.cli._run_exit_code", return_value=0
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue(), factory, runtime

    def test_description_file_path_failures_precede_runtime_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unreadable = root / "unreadable.txt"
            unreadable.write_text("PRIVATE-CONTENTS", encoding="utf-8")
            cases = (
                ("missing", root / "missing.txt", None),
                ("directory", root, None),
                ("unreadable", unreadable, PermissionError("PRIVATE-CONTENTS")),
            )
            for name, path, failure in cases:
                with self.subTest(name=name):
                    opener = (
                        patch.object(Path, "open", side_effect=failure)
                        if failure is not None else nullcontext()
                    )
                    with opener:
                        result, stdout, stderr, factory, _runtime = (
                            self._run_with_description(path)
                        )
                    self.assertEqual(1, result)
                    self.assertEqual("", stdout)
                    self.assertIn("description file", stderr.lower())
                    self.assertNotIn("PRIVATE-CONTENTS", stderr)
                    self.assertLess(len(stderr), 300)
                    factory.assert_not_called()

    def test_description_file_rejects_invalid_utf8_before_runtime_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "description.txt"
            path.write_bytes(b"valid prefix\xffPRIVATE-CONTENTS")
            result, stdout, stderr, factory, _runtime = (
                self._run_with_description(path)
            )
        self.assertEqual(1, result)
        self.assertEqual("", stdout)
        self.assertIn("utf-8", stderr.lower())
        self.assertNotIn("PRIVATE-CONTENTS", stderr)
        self.assertLess(len(stderr), 300)
        factory.assert_not_called()

    def test_description_file_rejects_ascii_and_unicode_over_character_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in (
                ("ascii.txt", "a" * 65537),
                ("unicode.txt", "界" * 65537),
            ):
                with self.subTest(name=name):
                    path = root / name
                    path.write_text(content, encoding="utf-8")
                    result, stdout, stderr, factory, _runtime = (
                        self._run_with_description(path)
                    )
                    self.assertEqual(1, result)
                    self.assertEqual("", stdout)
                    self.assertIn("65,536", stderr)
                    self.assertLess(len(stderr), 300)
                    factory.assert_not_called()

    def test_description_file_accepts_empty_valid_and_exact_character_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in (
                ("empty.txt", ""),
                ("valid.txt", "Validate locally."),
                ("ascii-limit.txt", "a" * 65536),
                ("unicode-limit.txt", "界" * 65536),
            ):
                with self.subTest(name=name):
                    path = root / name
                    path.write_text(content, encoding="utf-8")
                    result, _stdout, stderr, factory, runtime = (
                        self._run_with_description(path)
                    )
                    self.assertEqual(0, result)
                    self.assertEqual("", stderr)
                    factory.assert_called_once_with(
                        "config", project_keys=["demo"]
                    )
                    runtime.start_issue.assert_called_once_with(
                        "demo", "DEMO-1", title=None, description=content,
                    )

    def test_omitted_description_does_not_open_a_file(self):
        with patch.object(
            Path, "open", side_effect=AssertionError("must not open")
        ):
            result, _stdout, stderr, factory, runtime = (
                self._run_with_description()
            )
        self.assertEqual(0, result)
        self.assertEqual("", stderr)
        factory.assert_called_once_with("config", project_keys=["demo"])
        runtime.start_issue.assert_called_once_with(
            "demo", "DEMO-1", title=None, description="",
        )

    def test_description_file_is_bounded_utf8_read_once_before_runtime(self):
        events = []

        class Reader:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit):
                events.append(("read", limit))
                return "already read"

        def open_file(_path, mode="r", **kwargs):
            events.append(("open", mode, kwargs.get("encoding")))
            return Reader()

        def construct_runtime(*_args, **_kwargs):
            events.append(("runtime",))
            context = MagicMock()
            context.__enter__.return_value = runtime
            return context

        runtime = MagicMock()
        runtime.start_issue.return_value = "execution-1"
        runtime.run.return_value.as_dict.return_value = {}
        with patch.object(Path, "is_file", return_value=True), patch.object(
            Path, "open", autospec=True, side_effect=open_file
        ), patch(
            "dotfactory.cli.FactoryConfig.load", return_value="config"
        ), patch(
            "dotfactory.cli.FactoryRuntime", side_effect=construct_runtime
        ), patch(
            "dotfactory.cli._install_signals"
        ), patch(
            "dotfactory.cli._run_exit_code", return_value=0
        ):
            result = main([
                "run", "--config", "/tmp/factory.json", "--project", "demo",
                "--issue", "DEMO-1", "--description-file", "/tmp/task.md",
            ])
        self.assertEqual(0, result)
        self.assertEqual([
            ("open", "r", "utf-8"),
            ("read", 65537),
            ("runtime",),
        ], events)
        runtime.start_issue.assert_called_once_with(
            "demo", "DEMO-1", title=None, description="already read",
        )

    def test_run_exit_code_is_zero_only_for_an_idle_settled_boundary(self):
        runtime = MagicMock()
        runtime.ledger.run_snapshot.return_value = {"attention_requests": []}
        receipt = MagicMock(
            shutdown_reason="settled",
            ticks=({"scheduler": {"disposition": "idle"}},),
            executions=({"execution_id": "execution-1"},),
        )
        self.assertEqual(0, _run_exit_code(runtime, receipt))
        for shutdown_reason, disposition in (
            ("settled", "needs_attention"),
            ("settled", "capacity"),
            ("max_ticks", "completed"),
            ("signal", "idle"),
        ):
            with self.subTest(
                shutdown_reason=shutdown_reason, disposition=disposition,
            ):
                receipt.shutdown_reason = shutdown_reason
                receipt.ticks = ({"scheduler": {"disposition": disposition}},)
                self.assertEqual(1, _run_exit_code(runtime, receipt))

    def test_run_exit_code_rejects_an_empty_receipt(self):
        receipt = MagicMock(shutdown_reason="settled", ticks=(), executions=())
        self.assertEqual(1, _run_exit_code(MagicMock(), receipt))

    def test_run_exit_code_rejects_preexisting_open_attention(self):
        runtime = MagicMock()
        runtime.ledger.run_snapshot.return_value = {
            "attention_requests": [{"id": "attention-1", "status": "open"}],
        }
        receipt = MagicMock(
            shutdown_reason="settled",
            ticks=({"scheduler": {"disposition": "idle"}},),
            executions=({"execution_id": "execution-1"},),
        )
        self.assertEqual(1, _run_exit_code(runtime, receipt))

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
        ) as load, patch("dotfactory.cli._ledger_path", return_value=Path("/tmp/nonexistent-df-cli-test.db")):
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
            principal=Principal("operator@example.test", "approver", "cli"),
            request={
                "action": "attention", "expected_state": "Investigating",
                "confirmed": False,
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

    def test_dataset_cli_exports_one_deterministic_local_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _demo_config(root)
            config = FactoryConfig.load(config_path)
            with FactoryRuntime(config, runner=fixture_runner()) as runtime:
                execution = runtime.start_issue("demo", "DEMO-DATASET-1")
                runtime.run([execution], max_ticks=20)
            output_dir = root / "datasets"
            output = io.StringIO()
            with redirect_stdout(output):
                result = main([
                    "dataset", "--config", str(config_path),
                    "--project", "demo", "--execution", execution,
                    "--output", str(output_dir),
                ])
            self.assertEqual(0, result)
            receipt = json.loads(output.getvalue())
            self.assertFalse(receipt["hosted"]["enabled"])
            self.assertTrue(Path(receipt["dataset"]).is_file())
            self.assertTrue(Path(receipt["manifest"]).is_file())


if __name__ == "__main__":
    unittest.main()
