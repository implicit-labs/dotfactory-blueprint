import ast
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from dotfactory.cli import _demo_config, main


class DoctorCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_path = _demo_config(self.root)
        self.values = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.runner_marker = self.root / "runner-launched"
        self.runner = self.root / "doctor-test-runner"
        self.runner.write_text(
            "#!/bin/sh\nprintf launched > " + str(self.runner_marker) + "\n",
            encoding="utf-8",
        )
        self.runner.chmod(0o755)
        self.values["runners"]["codex"]["command"] = str(self.runner)
        self.write_config()

    def tearDown(self):
        self.temporary.cleanup()

    def write_config(self):
        self.config_path.write_text(
            json.dumps(self.values, indent=2) + "\n", encoding="utf-8"
        )

    def invoke(self, *arguments, environment=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        updates = {} if environment is None else environment
        with patch.dict(os.environ, updates, clear=False):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["doctor", "--config", str(self.config_path), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def json_result(self, environment=None):
        code, output, error = self.invoke("--json", environment=environment)
        self.assertEqual("", error)
        return code, json.loads(output)

    def assert_contract(self, result):
        self.assertEqual(
            {"schema_version", "command", "status", "summary", "checks"},
            set(result),
        )
        self.assertEqual(1, result["schema_version"])
        self.assertEqual("doctor", result["command"])
        self.assertIn(result["status"], {"pass", "fail"})
        self.assertEqual(
            {"passed", "failed", "skipped", "not_checked"},
            set(result["summary"]),
        )
        ids = [check["id"] for check in result["checks"]]
        self.assertEqual(sorted(ids), ids)
        self.assertEqual(len(ids), len(set(ids)))
        expected_fields = {
            "id", "scope", "status", "required", "message", "remedy", "boundary",
        }
        for check in result["checks"]:
            self.assertEqual(expected_fields, set(check))
            self.assertIn(check["status"], {"pass", "fail", "skipped", "not_checked"})
            self.assertIsInstance(check["required"], bool)
            for key in ("id", "scope", "message", "remedy", "boundary"):
                self.assertIsInstance(check[key], str)
                self.assertTrue(check[key])
        counts = {
            "passed": sum(item["status"] == "pass" for item in result["checks"]),
            "failed": sum(item["status"] == "fail" for item in result["checks"]),
            "skipped": sum(item["status"] == "skipped" for item in result["checks"]),
            "not_checked": sum(
                item["status"] == "not_checked" for item in result["checks"]
            ),
        }
        self.assertEqual(counts, result["summary"])
        required_failed = any(
            item["required"] and item["status"] == "fail"
            for item in result["checks"]
        )
        self.assertEqual("fail" if required_failed else "pass", result["status"])

    def check(self, result, check_id):
        return next(item for item in result["checks"] if item["id"] == check_id)

    def test_success_text_and_json_contract(self):
        code, result = self.json_result()
        self.assertEqual(0, code)
        self.assert_contract(result)
        self.assertEqual("pass", result["status"])
        code, output, error = self.invoke()
        self.assertEqual(0, code)
        self.assertEqual("", error)
        self.assertIn("PASS", output)
        self.assertIn("SKIPPED", output)
        self.assertIn("NOT CHECKED", output)
        self.assertIn("Remedy:", output)
        self.assertIn("Boundary:", output)

    def test_missing_and_invalid_config_are_actionable_without_tracebacks(self):
        missing = self.root / "missing.json"
        code, output, error = self.invoke("--json")
        self.assertEqual(0, code)
        self.assertEqual("", error)
        self.assertTrue(output)

        original = self.config_path
        self.config_path = missing
        code, output, error = self.invoke("--json")
        self.assertNotEqual(0, code)
        self.assertEqual("", error)
        result = json.loads(output)
        self.assert_contract(result)
        self.assertEqual("fail", self.check(result, "config")["status"])
        self.assertTrue(self.check(result, "config")["remedy"])
        self.assertNotIn("Traceback", output)

        self.config_path = original
        self.config_path.write_text("{not json", encoding="utf-8")
        code, output, error = self.invoke()
        self.assertNotEqual(0, code)
        self.assertEqual("", error)
        self.assertIn("FAIL", output)
        self.assertIn("Remedy:", output)
        self.assertNotIn("Traceback", output)

    def test_non_object_json_is_a_safe_configuration_failure(self):
        from dotfactory.instance import FactoryConfig, FactoryConfigError
        for value in ([], None, "credential-shaped-string", 1, True):
            with self.subTest(value=value):
                self.config_path.write_text(json.dumps(value))
                with self.assertRaisesRegex(FactoryConfigError, "JSON object"):
                    FactoryConfig.load(self.config_path)
                for args in ((), ("--json",)):
                    code, output, error = self.invoke(*args)
                    self.assertEqual(1, code)
                    self.assertEqual("", error)
                    self.assertNotIn("Traceback", output)
                    self.assertNotIn("credential-shaped-string", output)
                    if args:
                        self.assertEqual("fail", json.loads(output)["status"])
                    else:
                        self.assertIn("Remedy:", output)

    def test_repository_checks_cover_git_root_origin_and_local_origin_main(self):
        code, result = self.json_result()
        self.assertEqual(0, code)
        for suffix in ("git-root", "origin", "origin-main"):
            self.assertEqual("pass", self.check(result, "project:demo:" + suffix)["status"])

        missing = self.root / "does-not-exist"
        self.values["projects"]["demo"]["repository_path"] = str(missing)
        self.write_config()
        code, result = self.json_result()
        self.assertNotEqual(0, code)
        failed = self.check(result, "project:demo:git-root")
        self.assertEqual("fail", failed["status"])
        self.assertIn("repository", failed["remedy"].lower())

        repository = self.root / "repository"
        self.values["projects"]["demo"]["repository_path"] = str(repository)
        self.write_config()
        subprocess.run(
            ["git", "-C", str(repository), "remote", "remove", "origin"],
            check=True,
        )
        code, result = self.json_result()
        self.assertNotEqual(0, code)
        origin = self.check(result, "project:demo:origin")
        self.assertEqual("fail", origin["status"])
        self.assertIn("origin", origin["remedy"].lower())

        bare_origin = self.root / "origin.git"
        subprocess.run(
            ["git", "-C", str(repository), "remote", "add", "origin", str(bare_origin)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "update-ref", "-d", "refs/remotes/origin/main"],
            check=True,
        )
        code, result = self.json_result()
        self.assertNotEqual(0, code)
        base = self.check(result, "project:demo:origin-main")
        self.assertEqual("fail", base["status"])
        self.assertIn("fetch", base["remedy"].lower())

    def test_runner_executable_is_checked_without_launch_and_auth_is_not_checked(self):
        code, result = self.json_result()
        self.assertEqual(0, code)
        self.assertEqual("pass", self.check(result, "runner:codex:executable")["status"])
        auth = self.check(result, "runner:codex:authentication")
        self.assertEqual("not_checked", auth["status"])
        self.assertFalse(auth["required"])
        self.assertFalse(self.runner_marker.exists())

        self.values["runners"]["codex"]["command"] = "missing-doctor-runner-9f731b"
        self.write_config()
        code, result = self.json_result()
        self.assertNotEqual(0, code)
        missing = self.check(result, "runner:codex:executable")
        self.assertEqual("fail", missing["status"])
        self.assertIn("install", missing["remedy"].lower())

    def test_enabled_integration_and_runner_environment_presence(self):
        self.values["runners"]["codex"]["environment_envs"] = ["DOCTOR_RUNNER_KEY"]
        self.values["projections"]["logfire"] = {
            "enabled": True,
            "endpoint_env": "DOCTOR_LOGFIRE_ENDPOINT",
            "project": "example/readiness",
            "headers_env": "DOCTOR_LOGFIRE_HEADERS",
        }
        self.values["projections"]["linear"]["enabled"] = True
        self.values["projects"]["demo"]["tracker"]["team_id"] = "demo-team"
        self.write_config()
        with patch.dict(
            os.environ,
            {
                "DOCTOR_RUNNER_KEY": "",
                "DOCTOR_LOGFIRE_ENDPOINT": "",
                "DOCTOR_LOGFIRE_HEADERS": "",
                "LINEAR_API_KEY": "",
                "LINEAR_WEBHOOK_SECRET": "",
            },
            clear=False,
        ):
            code, result = self.json_result()
        self.assertNotEqual(0, code)
        self.assertEqual(
            "fail",
            self.check(result, "runner:codex:environment:DOCTOR_RUNNER_KEY")["status"],
        )
        self.assertEqual(
            "fail",
            self.check(
                result, "integration:logfire:environment:DOCTOR_LOGFIRE_HEADERS"
            )["status"],
        )
        self.assertEqual(
            "fail",
            self.check(result, "integration:linear:environment:LINEAR_API_KEY")["status"],
        )
        environment = {
            "DOCTOR_RUNNER_KEY": "runner-value",
            "DOCTOR_LOGFIRE_ENDPOINT": "https://example.invalid/otel",
            "DOCTOR_LOGFIRE_HEADERS": "Authorization=secret-value",
            "LINEAR_API_KEY": "linear-value",
            "LINEAR_WEBHOOK_SECRET": "webhook-value",
        }
        code, result = self.json_result(environment=environment)
        self.assertEqual(0, code)
        self.assertEqual(
            "pass",
            self.check(result, "runner:codex:environment:DOCTOR_RUNNER_KEY")["status"],
        )
        auth = self.check(result, "integration:logfire:authentication")
        self.assertEqual("not_checked", auth["status"])

    def test_polling_credentials_match_runtime_without_webhook_secret(self):
        from dotfactory.instance import FactoryConfig
        self.values["projections"]["linear"]["enabled"] = True
        self.values["projections"]["linear"]["webhook_secret_env"] = "LINEAR_WEBHOOK_SECRET"
        self.values["projects"]["demo"]["tracker"]["team_id"] = "demo-team"
        self.write_config()
        environment = {"LINEAR_API_KEY": "synthetic-token", "LINEAR_WEBHOOK_SECRET": ""}
        resolved = FactoryConfig.load(self.config_path).resolve_linear_projection(environment=environment)
        self.assertTrue(resolved["enabled"])
        code, result = self.json_result(environment=environment)
        self.assertEqual(0, code)
        webhook = self.check(result, "integration:linear:webhook")
        self.assertFalse(webhook["required"])
        self.assertEqual("not_checked", webhook["status"])
        self.assertNotIn("synthetic-token", json.dumps(result))
        code, output, error = self.invoke(environment=environment)
        self.assertEqual((0, ""), (code, error))
        self.assertIn("polling does not require", output)
        self.assertNotIn("synthetic-token", output)
        code, result = self.json_result(environment={"LINEAR_API_KEY": "", "LINEAR_WEBHOOK_SECRET": ""})
        self.assertNotEqual(0, code)
        self.assertEqual("fail", self.check(result, "integration:linear:environment:LINEAR_API_KEY")["status"])

    def test_disabled_integrations_are_skipped(self):
        self.values["projections"]["linear"]["enabled"] = False
        self.values["projections"]["logfire"] = {
            "enabled": False,
            "headers_env": "DOCTOR_DISABLED_SECRET",
            "project": "example/readiness",
            "dataset_enabled": False,
            "dataset_api_key_env": "DOCTOR_DISABLED_DATASET_KEY",
        }
        self.write_config()
        code, result = self.json_result()
        self.assertEqual(0, code)
        self.assertEqual(
            "skipped", self.check(result, "integration:linear:enabled")["status"]
        )
        self.assertEqual(
            "skipped", self.check(result, "integration:logfire:enabled")["status"]
        )
        self.assertNotIn("DOCTOR_DISABLED_SECRET", json.dumps(result))
        self.assertNotIn("DOCTOR_DISABLED_DATASET_KEY", json.dumps(result))

    def test_failure_json_and_exit_code(self):
        self.values["runners"]["codex"]["command"] = "missing-doctor-runner-c7810a"
        self.write_config()
        code, result = self.json_result()
        self.assertEqual(1, code)
        self.assert_contract(result)
        self.assertEqual("fail", result["status"])
        self.assertGreater(result["summary"]["failed"], 0)

    def test_output_redacts_remote_credentials_config_secrets_and_environment_values(self):
        repository = self.root / "repository"
        remote = "https://doctor-user:remote-secret@example.invalid/private.git"
        subprocess.run(
            ["git", "-C", str(repository), "remote", "set-url", "origin", remote],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "update-ref", "-d", "refs/remotes/origin/main"],
            check=True,
        )
        self.values["runners"]["codex"]["environment_envs"] = ["DOCTOR_SECRET_ENV"]
        self.write_config()
        code, output, error = self.invoke(
            "--json", environment={"DOCTOR_SECRET_ENV": "environment-secret"}
        )
        self.assertNotEqual(0, code)
        combined = output + error
        for secret in (remote, "doctor-user", "remote-secret", "environment-secret"):
            self.assertNotIn(secret, combined)

        self.config_path.write_text(
            '{"schema_version": 6, "password": "raw-config-secret"}\n',
            encoding="utf-8",
        )
        code, output, error = self.invoke("--json")
        self.assertNotEqual(0, code)
        self.assertNotIn("raw-config-secret", output + error)

    def test_doctor_has_no_runtime_network_or_repository_side_effects(self):
        repository = self.root / "repository"
        ledger = Path(self.values["ledger_path"])
        config_before = self.config_path.read_bytes()
        config_stat = self.config_path.stat()
        status_before = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain=v1"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout
        real_run = subprocess.run
        observed = []

        def local_git_only(command, *args, **kwargs):
            observed.append(tuple(str(item) for item in command))
            self.assertEqual("git", Path(str(command[0])).name)
            forbidden = {"fetch", "pull", "push", "clone", "init", "add", "commit", "update-ref"}
            self.assertTrue(forbidden.isdisjoint(set(command)))
            return real_run(command, *args, **kwargs)

        with patch("subprocess.run", side_effect=local_git_only), patch(
            "socket.create_connection", side_effect=AssertionError("network attempted")
        ), patch(
            "urllib.request.urlopen", side_effect=AssertionError("network attempted")
        ):
            code, result = self.json_result()
        self.assertEqual(0, code)
        self.assertTrue(observed)
        self.assertEqual(config_before, self.config_path.read_bytes())
        self.assertEqual(config_stat.st_mtime_ns, self.config_path.stat().st_mtime_ns)
        self.assertFalse(ledger.exists())
        self.assertFalse(self.runner_marker.exists())
        status_after = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain=v1"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout
        self.assertEqual(status_before, status_after)

    def test_new_module_uses_python39_syntax_and_stdlib_only(self):
        module = Path(__file__).resolve().parents[1] / "src" / "dotfactory" / "doctor.py"
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module), feature_version=(3, 9))
        allowed_local = {"instance", "configuration", "verification_host"}
        allowed_stdlib = {
            "dataclasses", "json", "os", "pathlib", "re", "shlex", "shutil",
            "subprocess", "typing",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed_stdlib)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                self.assertIn((node.module or "").split(".")[0], allowed_stdlib)
            elif isinstance(node, ast.ImportFrom) and node.level == 1:
                self.assertIn(node.module, allowed_local)

    def test_readme_documents_doctor_contract_and_boundaries(self):
        readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(
            encoding="utf-8"
        ).lower()
        for phrase in (
            "dotfactory doctor",
            "--json",
            "schema_version",
            "exit",
            "skipped",
            "not checked",
            "does not fetch",
            "authentication",
        ):
            self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()
