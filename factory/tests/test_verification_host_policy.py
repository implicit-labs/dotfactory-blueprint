import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory.delivery import (  # noqa: E402
    DeliveryError,
    _verify,
    git,
    resolve_verification_policy,
    verification_definition,
    verification_policy_guidance,
)
from dotfactory.live_runner import CodexAdapter  # noqa: E402


def run_git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={"PATH": os.defpath, "HOME": str(root.parent)},
    )
    return result.stdout.decode().strip()


class FakeLaunch:
    class Request:
        config = {
            "exit_contract": "plan-result-v2",
            "allowed_preferred_labels": ["complete", "failed"],
        }

    request = Request()
    skills = ()


class VerificationHostPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-policy-")
        self.root = Path(self.temp.name) / "repository"
        self.root.mkdir()
        run_git(self.root, "init", "-b", "main")
        run_git(self.root, "config", "user.email", "test@example.invalid")
        run_git(self.root, "config", "user.name", "Fixture")
        (self.root / ".factory").mkdir()
        (self.root / ".factory/plan.md").write_text("policy fixture\n")
        (self.root / ".factory/verify.py").write_text("print('PASS fixture')\n")

    def tearDown(self):
        self.temp.cleanup()

    def write_definition(self, policy_marker=True, policy=None):
        definition = {
            "schema_version": 1,
            "criteria": [{
                "id": "policy",
                "requirement": "exercise policy",
                "kind": "automated",
                "files": [".factory/verify.py"],
            }],
        }
        if policy_marker:
            definition["verification_policy"] = (
                {"timeout_seconds": 60} if policy is None else policy
            )
        (self.root / ".factory/verification.json").write_text(json.dumps(definition))
        run_git(self.root, "add", ".factory")
        return definition

    def commit(self, message="fixture"):
        run_git(self.root, "commit", "-m", message)
        return run_git(self.root, "rev-parse", "HEAD")

    def test_planning_prompt_uses_canonical_policy_guidance(self):
        prompt = CodexAdapter().stdin(FakeLaunch(), prompt_text="Plan the issue.")
        guidance = verification_policy_guidance()
        self.assertEqual(1, prompt.count(guidance))
        self.assertIn("sys.executable", guidance)
        self.assertIn(platform.python_version(), guidance)
        self.assertIn(os.defpath, guidance)
        for required in ("isolated", "HOME", "secret", "60", "120", "4 MiB", "cancel"):
            self.assertIn(required, guidance)

    def test_actual_subprocess_uses_host_interpreter_and_excludes_secrets(self):
        self.write_definition()
        hostile = self.root / "hostile-path"
        hostile.mkdir()
        fake_python = hostile / "python3.13"
        fake_python.write_text("#!/bin/sh\nexit 97\n")
        fake_python.chmod(0o700)
        script = """import os, platform, sys
assert sys.flags.isolated == 1
assert os.environ[\"PATH\"] == os.defpath
assert \"FACTORY_SENTINEL_SECRET\" not in os.environ
assert os.environ[\"HOME\"] != \"/ambient/home\"
print(\"INTERPRETER=\" + sys.executable)
print(\"VERSION=\" + platform.python_version())
print(\"PATH=\" + os.environ[\"PATH\"])
"""
        (self.root / ".factory/verify.py").write_text(script)
        run_git(self.root, "add", ".factory")
        head = self.commit()
        with patch.dict(os.environ, {
            "PATH": str(hostile) + os.pathsep + os.defpath,
            "HOME": "/ambient/home",
            "FACTORY_SENTINEL_SECRET": "must-not-leak",
        }):
            proof = _verify(
                self.root,
                head,
                None,
                policy=resolve_verification_policy({
                    "schema_version": 1,
                    "verification_policy": {"timeout_seconds": 60},
                }),
            )
        self.assertEqual(0, proof["exit_code"])
        self.assertIn("INTERPRETER=" + sys.executable, proof["output"])
        self.assertEqual(sys.executable, proof["policy"]["interpreter"])
        self.assertEqual(platform.python_version(), proof["policy"]["python_version"])
        self.assertEqual(os.defpath, proof["policy"]["path"])
        self.assertEqual(60, proof["policy"]["effective_timeout_seconds"])
        self.assertTrue(proof["policy"]["isolated"])
        self.assertTrue(proof["policy"]["ambient_secrets_excluded"])

    def test_selected_deadline_is_frozen_and_recorded(self):
        self.write_definition(policy={"timeout_seconds": 120})
        self.commit()
        checked = verification_definition(self.root)
        self.assertEqual(120, checked["policy"]["timeout_seconds"])
        self.assertIn(".factory/verification.json", checked["files"])

    def test_simulated_clock_enforces_selected_deadline(self):
        self.write_definition(policy={"timeout_seconds": 120})
        (self.root / ".factory/verify.py").write_text(
            "import time\ntime.sleep(120)\n"
        )
        run_git(self.root, "add", ".factory")
        head = self.commit()
        moments = iter((100.0, 100.0, 220.0))
        with self.assertRaisesRegex(DeliveryError, "120 seconds"):
            _verify(
                self.root,
                head,
                None,
                policy=resolve_verification_policy({
                    "schema_version": 1,
                    "verification_policy": {"timeout_seconds": 120},
                }),
                monotonic=lambda: next(moments),
                sleep=lambda _seconds: None,
            )

    def test_invalid_policy_settings_fail_without_coercion(self):
        invalid = (
            {"timeout_seconds": True},
            {"timeout_seconds": "120"},
            {"timeout_seconds": 0},
            {"timeout_seconds": 61},
            {"timeout_seconds": 121},
            {"timeout_seconds": 120, "extra": 1},
        )
        for policy in invalid:
            with self.subTest(policy=policy), self.assertRaises(DeliveryError):
                resolve_verification_policy({
                    "schema_version": 1,
                    "verification_policy": policy,
                })

    def test_legacy_definition_keeps_sixty_second_meaning(self):
        definition = self.write_definition(policy_marker=False)
        self.commit()
        self.assertEqual(
            {"schema_version": 1, "timeout_seconds": 60},
            resolve_verification_policy(definition),
        )
        self.assertEqual(60, verification_definition(self.root)["policy"]["timeout_seconds"])


if __name__ == "__main__":
    unittest.main()
