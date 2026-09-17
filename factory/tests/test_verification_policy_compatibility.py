import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory import delivery  # noqa: E402


class VerificationPolicyCompatibilityTests(unittest.TestCase):
    def test_legacy_accepted_receipt_uses_sixty_seconds_through_evaluate(self):
        with tempfile.TemporaryDirectory(prefix="df-legacy-policy-") as directory:
            root = Path(directory)
            (root / ".factory").mkdir()
            (root / ".factory/plan.md").write_text("legacy accepted plan\n")
            (root / ".factory/delivery.json").write_text(json.dumps({
                "summary": "legacy compatibility fixture",
                "limitations": [],
            }))

            request = SimpleNamespace(
                config={"exit_contract": "python-verification-v2"},
                attempt_id="attempt",
                fence_token="fence",
                workflow_digest="workflow",
                execution_id="execution",
            )
            launch = SimpleNamespace(request=request, workspace_path=str(root))
            ledger = MagicMock()
            ledger.assert_attempt_active = MagicMock()
            ledger.event_for_command.return_value = None
            ledger.workspace_for_execution.return_value = {"path": str(root)}
            ledger.record_delivery_check.side_effect = (
                lambda _attempt, _fence, receipt: {"payload": receipt}
            )
            legacy_approved = {
                "head_sha": "old-plan",
                "definition": {"schema_version": 1},
                "files": {},
            }

            def verify(_root, _base, _progress, *, policy=None):
                resolved = delivery.resolve_verification_policy({
                    "schema_version": 1,
                    **({"verification_policy": policy} if policy is not None else {}),
                })
                return {
                    "exit_code": 0,
                    "policy": {"effective_timeout_seconds": resolved["timeout_seconds"]},
                }

            source = {
                "base_sha": "base",
                "head_sha": "head",
                "changed_files": ["factory/src/dotfactory/delivery.py"],
            }
            with (
                patch.object(delivery, "source_snapshot", return_value=source),
                patch.object(delivery, "_evidence", return_value=[]),
                patch.object(delivery, "git", return_value=b"factory/src/dotfactory/delivery.py\0"),
                patch.object(delivery, "approved_plan", return_value=legacy_approved),
                patch.object(delivery, "_verify", side_effect=verify) as verifier,
            ):
                result = delivery.evaluate(
                    ledger,
                    launch,
                    delivery.RunnerResult("done", "complete", ()),
                )

            self.assertEqual("complete", result.preferred_label)
            verifier.assert_called_once()
            self.assertIsNone(verifier.call_args.kwargs["policy"])
            receipt = ledger.record_delivery_check.call_args.args[2]
            self.assertEqual(
                60,
                receipt["verification"]["policy"]["effective_timeout_seconds"],
            )


if __name__ == "__main__":
    unittest.main()
