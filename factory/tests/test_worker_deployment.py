"""Persistent native login paths survive worker filtering; startup fails closed."""

import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dotfactory import worker
from dotfactory.cli import main
from dotfactory.execution import transport_command, validate_policy

PATH = Path(__file__).resolve().parents[1] / "deploy/owned-worker/start.py"
SPEC = importlib.util.spec_from_file_location("worker_start", PATH)
START = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(START)


class WorkerDeploymentTests(unittest.TestCase):
    def test_cloud_boot_requires_explicit_project_and_persistent_state(self):
        self.assertIsNone(START.coordinator_command({}))
        with self.assertRaises(ValueError):
            START.coordinator_command({"DOTFACTORY_PROJECT": "example"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = json.loads((PATH.parents[2] / "factory.example.json").read_text())
            values["ledger_path"] = str(root / "factory.db")
            values["scheduler"]["limits"]["projects"] = {"example": 1}
            values["preparation"]["workspace"]["root"] = str(root / "workspaces")
            values["projects"] = {"example": {
                "display_name": "Example", "enabled_by_default": True,
                "repository_path": str(root / "repository"),
                "tracker": {"kind": "linear", "project_id": "project"},
            }}
            values["execution"] = {
                "workers": {"cloud": {"transport": "local", "root": str(root / "work"),
                                       "billing": "subscription"}},
                "stages": {"Implementing": {"workers": ["cloud"], "scope": "portable",
                                            "requires": ["os:linux"], "checks": []}},
            }
            config = root / "factory.json"
            config.write_text(json.dumps(values))
            env = {"DOTFACTORY_CONFIG": str(config), "DOTFACTORY_PROJECT": "example"}
            self.assertEqual(START.coordinator_command(env, root)[-2:], ["--project", "example"])
            (root / "ephemeral-link").symlink_to("/tmp", target_is_directory=True)
            for name in ("ledger", "repository", "workspace", "worker", "worker-symlink"):
                invalid = json.loads(json.dumps(values))
                if name == "ledger":
                    invalid["ledger_path"] = "/tmp/ephemeral.db"
                elif name == "repository":
                    invalid["projects"]["example"]["repository_path"] = "/tmp/ephemeral"
                elif name == "workspace":
                    invalid["preparation"]["workspace"]["root"] = "/tmp/ephemeral"
                else:
                    invalid["execution"]["workers"]["cloud"]["root"] = (
                        "/tmp/ephemeral" if name == "worker" else str(root / "ephemeral-link/work"))
                config.write_text(json.dumps(invalid))
                with self.subTest(name=name), self.assertRaises(ValueError):
                    START.coordinator_command(env, root)

    def test_http_server_and_continuous_work_have_distinct_cli_contracts(self):
        for command, expected in (("serve", "--role"), ("work", "--max-ticks")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exited:
                main([command, "--help"])
            self.assertEqual(exited.exception.code, 0)
            self.assertIn(expected, output.getvalue())

    def test_native_config_paths_survive_without_api_billing_fallback(self):
        with patch.dict(os.environ, {"CODEX_HOME": "/data/codex", "CLAUDE_CONFIG_DIR": "/data/claude",
                                     "OPENAI_API_KEY": "not-for-subscription",
                                     "ANTHROPIC_API_KEY": "not-for-subscription"}, clear=True):
            env = worker.environment("subscription", "codex")
        self.assertEqual(env["CODEX_HOME"], "/data/codex")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/data/claude")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_restart_preserves_native_login_and_attempt_files(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            START.prepare(root)
            marker = root / "codex" / "auth.json"
            marker.write_text("fixture login")
            START.prepare(root)
            self.assertEqual(marker.read_text(), "fixture login")
            self.assertEqual((root / "codex").stat().st_mode & 0o777, 0o700)

    def test_missing_mount_is_not_created_as_ephemeral_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "absent"
            with self.assertRaises(ValueError):
                START.prepare(missing)
            self.assertFalse(missing.exists())

    def test_symlink_config_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "codex").symlink_to(root)
            with self.assertRaises(ValueError):
                START.prepare(root)

    def test_render_ssh_uses_existing_transport_and_isolated_python(self):
        config = {"transport": "ssh", "target": "srv-example@ssh.virginia.render.com",
                  "root": "/data/work", "entrypoint": "/opt/dotfactory/worker.py",
                  "billing": "subscription"}
        validate_policy({"workers": {"render": config}, "stages": {"Implementing": {
            "workers": ["render"], "scope": "portable", "requires": ["os:linux"], "checks": [],
        }}})
        command = transport_command(config)
        self.assertEqual(command[-2], config["target"])
        self.assertEqual(command[-1], "python3 -I /opt/dotfactory/worker.py")
        self.assertIn("BatchMode=yes", command)


if __name__ == "__main__":
    unittest.main()
