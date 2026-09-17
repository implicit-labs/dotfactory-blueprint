import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory.delivery import (  # noqa: E402
    DeliveryError,
    verification_definition,
    verification_policy_guidance,
)


def run_git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={"PATH": os.defpath, "HOME": str(root.parent)},
    )


class VerificationPolicyShapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-policy-shape-")
        self.root = Path(self.temp.name) / "repository"
        self.root.mkdir()
        run_git(self.root, "init", "-b", "main")
        run_git(self.root, "config", "user.email", "test@example.invalid")
        run_git(self.root, "config", "user.name", "Fixture")
        (self.root / ".factory").mkdir()
        (self.root / ".factory/plan.md").write_text("policy shape fixture\n")
        (self.root / ".factory/verify.py").write_text("print('PASS fixture')\n")

    def tearDown(self):
        self.temp.cleanup()

    def write_definition(self, **extra):
        definition = {
            "schema_version": 1,
            "criteria": [{
                "id": "policy-shape",
                "requirement": "exercise policy shape",
                "kind": "automated",
                "files": [".factory/verify.py"],
            }],
            **extra,
        }
        (self.root / ".factory/verification.json").write_text(json.dumps(definition))
        run_git(self.root, "add", ".factory")
        run_git(self.root, "commit", "-m", "fixture")

    def test_observed_top_level_timeout_is_rejected(self):
        self.write_definition(timeout_seconds=120)
        with self.assertRaisesRegex(DeliveryError, "unknown top-level field.*timeout_seconds"):
            verification_definition(self.root)

    def test_nested_timeout_is_accepted_and_shown_exactly(self):
        self.write_definition(verification_policy={"timeout_seconds": 120})
        checked = verification_definition(self.root)
        self.assertEqual(120, checked["policy"]["timeout_seconds"])
        self.assertIn(
            '"verification_policy":{"timeout_seconds":120}',
            verification_policy_guidance(),
        )

    def test_policy_free_legacy_definition_remains_sixty_seconds(self):
        self.write_definition()
        checked = verification_definition(self.root)
        self.assertEqual(60, checked["policy"]["timeout_seconds"])


if __name__ == "__main__":
    unittest.main()
