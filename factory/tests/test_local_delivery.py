import io
import json
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from dotfactory import FactoryConfig, FactoryRuntime
from dotfactory.cli import _demo_config, main
from dotfactory.lifecycle import fixture_runner
from dotfactory.local_delivery import initialize


class LocalDeliveryTests(unittest.TestCase):
    def repository(self, root, verifier=True):
        config = _demo_config(root)
        seed = root / "seed"
        if verifier:
            (seed / ".factory").mkdir()
            (seed / ".factory" / "verify.py").write_text("print('checked')\n")
            for args in (["add", ".factory"], ["commit", "-m", "pin verification"], ["push", "origin", "main"]):
                subprocess.run(["git", "-C", str(seed), *args], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root / "repository"), "fetch", "origin"], check=True, capture_output=True)
        return config

    def invoke(self, arguments):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(arguments)
        return code, output.getvalue(), error.getvalue()

    def test_init_generates_valid_secret_free_lane_without_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repository(root)
            destination = root / "instance with spaces"
            with patch("dotfactory.cli.FactoryRuntime") as runtime:
                code, output, error = self.invoke([
                    "init", "--repository", str(root / "repository"),
                    "--output", str(destination), "--project", "sample",
                    "--linear-project", "owning-project", "--logfire",
                ])
            self.assertEqual((0, ""), (code, error))
            receipt = json.loads(output)
            self.assertEqual(receipt["next_argv"], shlex.split(receipt["next"]))
            self.assertEqual("Review", receipt["next_argv"][-1])
            runtime.assert_not_called()
            values = FactoryConfig.load(json.loads(output)["config"]).values
            self.assertFalse(values["projections"]["linear"]["enabled"])
            self.assertTrue(values["projections"]["logfire"]["enabled"])
            self.assertEqual("verified-python", values["default_workflow"])
            self.assertEqual("gpt-5.6-sol", values["runners"]["codex"]["default_model"])
            self.assertEqual("medium", values["runners"]["codex"]["default_reasoning_effort"])
            self.assertEqual("explicit", values["preparation"]["workspace"]["retention"])
            self.assertEqual(["factory.json"], [p.name for p in destination.iterdir()])
            self.assertEqual(0o600, (destination / "factory.json").stat().st_mode & 0o777)

    def test_init_accepts_repository_without_verifier_without_changing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repository(root, verifier=False)
            receipt = initialize(repository=str(root / "repository"), output=str(root / "instance"), project="sample", linear_project="owner")
            self.assertTrue(Path(receipt["config"]).is_file())
            self.assertFalse((root / "repository/.factory").exists())
            self.assertFalse((root / "instance/factory.db").exists())

    def test_initialized_lane_automatically_verifies_after_restart(self):
        from test_verified_delivery import EditingRunner
        from dotfactory.delivery import export_review
        with tempfile.TemporaryDirectory(prefix="df-init-", dir="/tmp") as temporary:
            root = Path(temporary)
            self.repository(root, verifier=False)
            receipt = initialize(repository=str(root / "repository"), output=str(root / "instance"), project="demo", linear_project="owner")
            config = FactoryConfig.load(receipt["config"])
            runner = EditingRunner()
            with FactoryRuntime(config, runner=runner) as runtime:
                execution = runtime.start_issue("demo", "LOCAL-INIT-1", description="Return Japanese greeting")
                runtime.run([execution], until_state="Ready")
                self.assertEqual(["Autoplanning"], runner.calls)
                self.assertEqual("Ready", runtime.ledger.current(execution)["current_state_id"])
            with FactoryRuntime(config, runner=runner) as runtime:
                runtime.run([execution], until_state="Review")
                self.assertEqual("Review", runtime.ledger.current(execution)["current_state_id"])
                export_review(runtime.ledger, execution, str(root / "review"))
            self.assertEqual(["Autoplanning", "Implementing", "Verifying"], runner.calls)
            proof = json.loads((root / "review/review.json").read_text())["check"]
            self.assertTrue(proof["passed"])
            self.assertFalse((root / "repository/.factory").exists())

    def test_init_never_overwrites_or_follows_an_existing_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            (target / "keep").write_text("preserve")
            link = root / "link"
            link.symlink_to(target)
            for destination in (target, link):
                with self.assertRaisesRegex(ValueError, "already exists"):
                    initialize(repository=str(root), output=str(destination), project="sample", linear_project="owner")
            self.assertEqual("preserve", (target / "keep").read_text())

    def test_status_before_first_run_does_not_create_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.repository(root)
            with patch("dotfactory.cli.FactoryRuntime") as runtime:
                code, output, error = self.invoke(["status", "--config", str(config), "--project", "demo"])
            self.assertEqual(1, code)
            self.assertIn("no local ledger exists", error)
            self.assertEqual("", output)
            runtime.assert_not_called()

    def test_status_reads_stopped_run_without_starting_runners(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.repository(root)
            with FactoryRuntime(FactoryConfig.load(path), runner=fixture_runner()) as runtime:
                execution = runtime.start_issue("demo", "LOCAL-1")
                runtime.run([execution], max_ticks=20)
            code, output, error = self.invoke(["status", "--config", str(path), "--project", "demo", "--execution", execution])
            self.assertEqual((0, ""), (code, error))
            response = json.loads(output)
            self.assertTrue(response["ok"])
            self.assertEqual("offline-exclusive-lock", response["transport"])
            self.assertIn(execution, output)

    def test_status_uses_owner_socket_without_opening_second_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.repository(Path(temporary))
            with patch("dotfactory.operator.socket_path") as endpoint, patch("dotfactory.operator.send", return_value={"ok": True, "data": {}}) as send, patch("dotfactory.cli.FactoryRuntime") as runtime:
                endpoint.return_value.exists.return_value = True
                code, output, error = self.invoke(["status", "--config", str(path), "--project", "demo"])
            self.assertEqual((0, ""), (code, error))
            self.assertEqual("owner-socket", json.loads(output)["transport"])
            send.assert_called_once()
            runtime.assert_not_called()

    def test_status_does_not_fall_back_after_ambiguous_socket_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.repository(Path(temporary))
            with patch("dotfactory.operator.socket_path") as endpoint, patch("dotfactory.operator.send", side_effect=TimeoutError("owner timeout")), patch("dotfactory.cli.FactoryRuntime") as runtime:
                endpoint.return_value.exists.return_value = True
                code, output, error = self.invoke(["status", "--config", str(path), "--project", "demo"])
            self.assertEqual(1, code)
            self.assertIn("owner timeout", error)
            runtime.assert_not_called()

    def test_status_without_socket_cannot_take_an_active_writer_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.repository(Path(temporary))
            with FactoryRuntime(FactoryConfig.load(path), runner=fixture_runner()):
                code, output, error = self.invoke(["status", "--config", str(path), "--project", "demo"])
            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertTrue(error.strip())

    def test_status_rejects_foreign_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.repository(root)
            values = json.loads(path.read_text())
            values["projects"]["other"] = dict(values["projects"]["demo"])
            values["projects"]["other"]["tracker"] = {"kind": "linear", "project_id": "other-project"}
            values["scheduler"]["limits"]["projects"]["other"] = 1
            path.write_text(json.dumps(values))
            with FactoryRuntime(FactoryConfig.load(path), runner=fixture_runner()) as runtime:
                execution = runtime.start_issue("other", "OTHER-1")
            code, output, error = self.invoke(["status", "--config", str(path), "--project", "demo", "--execution", execution])
            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("not available in project", error)

    def test_status_stale_socket_uses_exclusive_offline_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.repository(Path(temporary))
            with FactoryRuntime(FactoryConfig.load(path), runner=fixture_runner()):
                pass
            with patch("dotfactory.operator.socket_path") as endpoint, patch("dotfactory.operator.send", side_effect=ConnectionRefusedError()):
                endpoint.return_value.exists.return_value = True
                code, output, error = self.invoke(["status", "--config", str(path), "--project", "demo"])
            self.assertEqual((0, ""), (code, error))
            self.assertEqual("offline-exclusive-lock", json.loads(output)["transport"])


if __name__ == "__main__":
    unittest.main()
