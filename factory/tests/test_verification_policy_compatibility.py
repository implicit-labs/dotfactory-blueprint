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
        self.evaluate_fixture()

    def test_delivery_rechecks_host_after_preflight_succeeded(self):
        self.evaluate_fixture(host_drift=True)

    def evaluate_fixture(self, host_drift=False):
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
                execution_id="execution", state_id="Verifying",
            )
            launch = SimpleNamespace(request=request, workspace_path=str(root))
            ledger = MagicMock()
            ledger.assert_attempt_active = MagicMock()
            # Legacy fixture predates frozen execution policies.
            ledger.connection.execute.return_value.fetchone.return_value = None
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
            host = None
            if host_drift:
                marker = root / 'fixture-available'
                marker.touch()
                host = {'readiness': [{'name': 'fixture', 'command': [
                    '{python}', '-I', '-c', 'from pathlib import Path; assert Path(' + repr(str(marker)) + ').exists()']}]}
                from dotfactory.verification_host import inspect
                self.assertTrue(inspect(host, execute=True)['available'])
                marker.unlink()
            settings = {'stages': {'Verifying': {'coordinator': host}}} if host else None
            with (
                patch('dotfactory.execution.settings_view', return_value=settings),
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

            if host_drift:
                verifier.assert_not_called()
                receipt = ledger.record_delivery_check.call_args.args[2]
                self.assertFalse(receipt['passed'])
                self.assertFalse(receipt['coordinator']['available'])
                self.assertIn('coordinator verification prerequisites', receipt['error'])
                self.assertNotEqual('complete', result.preferred_label)
                return
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
