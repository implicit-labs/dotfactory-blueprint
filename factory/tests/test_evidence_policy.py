"""Local evidence policy proofs: real admission/export, isolation and crash recovery."""
import copy
import io
import json
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

import test_verified_delivery as fixture
from dotfactory import FactoryConfig, FactoryRuntime, LifecycleError
from dotfactory.cli import main
from dotfactory.configuration import preview
from dotfactory.delivery import export_review
from dotfactory.evidence_bundle import cleanup
from dotfactory.evidence_policy import authorization, resolve, settings_view
from dotfactory.ledger import LedgerError


class EvidencePolicyTests(unittest.TestCase):
    setUp = fixture.VerifiedDeliveryTests.setUp
    tearDown = fixture.VerifiedDeliveryTests.tearDown

    def configure(self, section=None, project=None, *, second=False):
        values = copy.deepcopy(self.config.values)
        if section is not None:
            values["evidence_policy"] = section
        if project is not None:
            values["projects"]["demo"]["evidence_policy"] = project
        if second:
            values["projects"]["other"] = copy.deepcopy(values["projects"]["demo"])
            values["projects"]["other"]["evidence_policy"] = {"mode": "disabled"}
            values["projects"]["other"]["tracker"]["project_id"] = "other-project"
        self.config.path.write_text(json.dumps(values))
        self.config = FactoryConfig.load(self.config.path)
        return self.config

    def prepared(self, runtime, name="EVIDENCE-1", override=None):
        execution = runtime.start_issue("demo", name, description="private intent",
                                        execution_override=override)
        runtime.run([execution], until_state="PlanReview")
        self.assertEqual("PlanReview", runtime.ledger.current(execution)["current_state_id"])
        return execution

    def expire(self, ledger):
        current = datetime.fromisoformat(ledger.clock().replace("Z", "+00:00"))
        ledger.clock = lambda: (current + timedelta(days=1)).isoformat()

    def test_resolution_constraints_clearing_and_preview(self):
        self.configure({"defaults": {"retention_seconds": 600, "redact_fields": ["title"]},
                        "constraints": {"allow_full": False, "max_retention_seconds": 100,
                                        "redact_fields": ["intent"]}},
                       {"retention_seconds": 500, "mode": "full"})
        override = {"evidence_policy": {"retention_seconds": None, "redact_fields": []}}
        policy = preview(self.config, "demo", override)["evidence_policy"]
        self.assertEqual({"destination": "operator-local", "mode": "summary",
                          "retention_seconds": 100, "redact_fields": ["intent"]}, policy["effective"])
        self.assertEqual("instance:constraint", policy["provenance"]["mode"])
        self.assertTrue(policy["conflicts"])
        self.assertNotIn("root", json.dumps(policy))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, main(["config-preview", "--config", str(self.config.path), "--project", "demo"]))
        self.assertEqual("summary", json.loads(output.getvalue())["evidence_policy"]["effective"]["mode"])
        with self.assertRaisesRegex(ValueError, "require worker"):
            preview(self.config, "demo", {"stages": {"Verifying": {"workers": ["missing"]}}})

    def test_invalid_values_and_credentials_rejected_before_admission(self):
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            for number, override in enumerate(({"retention_seconds": True}, {"mode": "publish"},
                    {"destination": "https://unapproved.invalid"}, {"token": "private"},
                    {"destination": "missing"}, {"redact_fields": "password"})):
                with self.subTest(override=override), self.assertRaises(ValueError):
                    runtime.start_issue("demo", f"BAD-{number}", execution_override={"evidence_policy": override})
            self.assertEqual(0, runtime.ledger.connection.execute("SELECT COUNT(*) FROM workflow_executions").fetchone()[0])
        with self.assertRaises(ValueError):
            self.configure({"destinations": {"bad": {"kind": "local_review", "root": "/tmp", "enabled": True, "credentials": "secret"}}})

    def test_snapshot_atomicity_direct_kernel_restart_replay_and_migration(self):
        self.configure(project={"mode": "summary"})
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = runtime.start_issue("demo", "FROZEN", execution_override={"evidence_policy": {"retention_seconds": 70}})
            frozen = runtime.ledger.run_snapshot(run)["evidence_policy"]
            direct = runtime.kernels["demo"].begin("demo", "DIRECT", {}, command_id="direct")
            self.assertEqual("summary", settings_view(runtime.ledger, direct)["effective"]["mode"])
            from dotfactory.evidence_policy import record_admission
            before = runtime.ledger.connection.execute("SELECT COUNT(*) FROM workflow_executions").fetchone()[0]
            from unittest.mock import patch
            def fail(*args):
                record_admission(*args)
                raise RuntimeError("rollback")
            with patch("dotfactory.evidence_policy.record_admission", side_effect=fail), self.assertRaisesRegex(RuntimeError, "rollback"):
                runtime.start_issue("demo", "ROLLBACK")
            self.assertEqual(before, runtime.ledger.connection.execute("SELECT COUNT(*) FROM workflow_executions").fetchone()[0])
            runtime.ledger.connection.execute("DELETE FROM evidence_policies WHERE execution_id=?", (direct,))
        self.configure(project={"mode": "disabled"})
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            self.assertEqual(frozen, settings_view(runtime.ledger, run))
            self.assertEqual(run, runtime.start_issue("demo", "FROZEN"))
            with self.assertRaisesRegex(LifecycleError, "frozen"):
                runtime.start_issue("demo", "FROZEN", execution_override={"evidence_policy": {"mode": "full"}})
            self.assertIsNone(settings_view(runtime.ledger, direct))
            with self.assertRaisesRegex(LedgerError, "requires a migrated"):
                authorization(runtime.ledger, direct, str(self.root / "missing"))

    def test_actual_legacy_adoption_happens_once_and_reopen_requires_owner(self):
        from dotfactory import SQLiteLedger, DurableKernel
        from dotfactory.evidence_policy import configure
        self.configure(project={"mode": "disabled"})
        path = self.root / "legacy.db"
        with closing(SQLiteLedger(path)) as ledger:
            ledger.configure_factory("legacy")
            ledger.register_project("demo", display_name="demo", tracker_kind="linear", tracker_project_id="demo-project")
            kernel = DurableKernel(ledger, fixture.ROOT / "workflows/default.dot")
            run = kernel.begin("demo", "LEGACY", {}, command_id="legacy")
            self.assertIsNone(settings_view(ledger, run))
            configure(ledger, self.config.values, ["demo"])
            migrated = settings_view(ledger, run)
            self.assertEqual("legacy-v0", migrated["migration"])
            self.assertEqual("full", migrated["effective"]["mode"])
        with closing(SQLiteLedger(path)) as ledger:
            with self.assertRaisesRegex(LedgerError, "owning configuration"):
                authorization(ledger, run, str(self.root / "reopened"))

    def test_summary_export_never_contains_intent_patch_logs_or_binary_artifacts(self):
        self.configure(project={"mode": "summary"})
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = self.prepared(runtime)
            output = self.root / "summary"
            receipt = export_review(runtime.ledger, run, str(output))
            self.assertEqual({"review.json", "evidence-manifest.json"}, {p.name for p in output.iterdir()})
            self.assertNotIn("private intent", (output / "review.json").read_text())
            self.assertFalse(json.loads((output / "review.json").read_text())["source_bytes_included"])
            self.assertEqual(receipt, export_review(runtime.ledger, run, str(output)))

    def test_full_export_integrity_redaction_and_cleanup_of_export_only(self):
        self.configure(project={"retention_seconds": 1, "redact_fields": ["intent", "context"]})
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = self.prepared(runtime)
            output = self.root / "full"
            receipt = export_review(runtime.ledger, run, str(output))
            packet = json.loads((output / "review.json").read_text())
            self.assertEqual("[REDACTED]", packet["intent"])
            import hashlib
            self.assertEqual(receipt["patch_sha256"], hashlib.sha256((output / "change.patch").read_bytes()).hexdigest())
            self.assertEqual([], cleanup(runtime.ledger, "demo"))
            self.expire(runtime.ledger)
            self.assertEqual("eligible", cleanup(runtime.ledger, "demo")[0]["status"])
            self.assertTrue(output.exists())
            self.assertEqual("deleted", cleanup(runtime.ledger, "demo", apply=True)[0]["status"])
            self.assertFalse(output.exists())
            self.assertTrue(Path(runtime.ledger.workspace_for_execution(run)["path"]).is_dir())
            self.assertTrue(settings_view(runtime.ledger, run))
            self.assertEqual([], cleanup(runtime.ledger, "demo", apply=True))

    def test_destination_isolation_unavailable_and_symlink_refusal(self):
        root = self.root / "allowed"
        self.configure({"destinations": {"approved": {"kind": "local_review", "enabled": True, "root": str(root)}},
                        "defaults": {"destination": "approved"}}, second=True)
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = runtime.start_issue("demo", "DESTINATION")
            other = runtime.start_issue("other", "OTHER")
            self.assertEqual("disabled", settings_view(runtime.ledger, other)["effective"]["mode"])
            with self.assertRaisesRegex(LedgerError, "disabled"):
                authorization(runtime.ledger, other, str(root / "other"))
            with self.assertRaisesRegex(LedgerError, "inside"):
                authorization(runtime.ledger, run, str(self.root / "outside"))
            root.mkdir()
            (root / "link").symlink_to(self.root, target_is_directory=True)
            with self.assertRaisesRegex(LedgerError, "symlink"):
                authorization(runtime.ledger, run, str(root / "link" / "export"))
            runtime.ledger.evidence_configuration["evidence_policy"]["destinations"]["approved"]["enabled"] = False
            with self.assertRaisesRegex(LedgerError, "unavailable"):
                authorization(runtime.ledger, run, str(root / "export"))

    def test_mandatory_constraints_cannot_be_weakened_and_snapshot_tamper_blocks(self):
        self.configure({"constraints": {"require_export": True}})
        with self.assertRaisesRegex(ValueError, "mandatory"):
            resolve(self.config.values, "demo", {"mode": "disabled"})
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = runtime.start_issue("demo", "TAMPER")
            runtime.ledger.evidence_configuration["evidence_policy"]["constraints"]["allow_full"] = False
            self.assertEqual("summary", authorization(runtime.ledger, run, str(self.root / "bundle"))[1]["mode"])
            policy = settings_view(runtime.ledger, run)
            policy["effective"]["mode"] = "disabled"
            runtime.ledger.connection.execute("UPDATE evidence_policies SET policy_json=? WHERE execution_id=?", (json.dumps(policy), run))
            with self.assertRaisesRegex(LedgerError, "invalid"):
                authorization(runtime.ledger, run, str(self.root / "bundle"))

    def test_cleanup_refuses_extra_modified_or_symlinked_files(self):
        self.configure(project={"retention_seconds": 1})
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = self.prepared(runtime)
            outputs = [self.root / name for name in ("extra", "modified", "symlink")]
            for output in outputs:
                export_review(runtime.ledger, run, str(output))
            (outputs[0] / "user.txt").write_text("unowned")
            (outputs[1] / "change.patch").write_text("changed")
            target = self.root / "user-data"
            target.write_text("preserve")
            (outputs[2] / "review.json").unlink()
            (outputs[2] / "review.json").symlink_to(target)
            self.expire(runtime.ledger)
            self.assertTrue(all(item["status"] == "needs_attention" for item in cleanup(runtime.ledger, "demo", apply=True)))
            self.assertTrue(all(output.exists() for output in outputs))
            self.assertEqual("preserve", target.read_text())

    def test_publication_and_cleanup_recover_after_journaled_crashes(self):
        self.configure(project={"retention_seconds": 1})
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = self.prepared(runtime)
            output = self.root / "crash"
            def fault(boundary):
                if boundary == "after_evidence_publish":
                    raise RuntimeError("crash")
            runtime.ledger.fault_hook = fault
            with self.assertRaisesRegex(RuntimeError, "crash"):
                export_review(runtime.ledger, run, str(output))
            runtime.ledger.fault_hook = None
            self.assertEqual("full", export_review(runtime.ledger, run, str(output))["mode"])
            self.expire(runtime.ledger)
            def cleanup_fault(boundary):
                if boundary == "after_evidence_cleanup":
                    raise RuntimeError("cleanup crash")
            runtime.ledger.fault_hook = cleanup_fault
            with self.assertRaisesRegex(RuntimeError, "cleanup crash"):
                cleanup(runtime.ledger, "demo", apply=True)
            runtime.ledger.fault_hook = None
            self.assertEqual("deleted", cleanup(runtime.ledger, "demo", apply=True)[0]["status"])

    def test_disabled_export_does_not_create_output_or_send_network(self):
        self.configure(project={"mode": "disabled"})
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = runtime.start_issue("demo", "DISABLED")
            output = self.root / "missing" / "bundle"
            with self.assertRaisesRegex(LedgerError, "disabled"):
                export_review(runtime.ledger, run, str(output))
            self.assertFalse(output.parent.exists())

    def test_full_export_rejects_secret_in_source_without_publishing(self):
        class SecretRunner(fixture.EditingRunner):
            def run(self, launch):
                result = super().run(launch)
                root = Path(launch.workspace_path)
                (root / ".factory/plan.md").write_text("private test credential: ghp_" + "x" * 30)
                fixture._git(root, "add", ".")
                fixture._git(root, "commit", "--allow-empty", "-m", "sensitive fixture")
                return result
        with FactoryRuntime(self.config, runner=SecretRunner()) as runtime:
            run = self.prepared(runtime)
            output = self.root / "secret"
            with self.assertRaisesRegex(LedgerError, "possible secret"):
                export_review(runtime.ledger, run, str(output))
            self.assertFalse(output.exists())
            self.assertFalse(list(self.root.glob(".factory-review-*")))

    def test_replay_rechecks_expiry_current_constraints_and_run_ownership(self):
        self.configure(project={"retention_seconds": 2})
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = self.prepared(runtime)
            output = self.root / "receipt"
            export_review(runtime.ledger, run, str(output))
            other = runtime.start_issue("demo", "OTHER-RUN")
            with self.assertRaisesRegex(LedgerError, "different run"):
                export_review(runtime.ledger, other, str(output))
            runtime.ledger.evidence_configuration["evidence_policy"] = {"constraints": {"allow_full": False}}
            with self.assertRaisesRegex(LedgerError, "constraints changed"):
                export_review(runtime.ledger, run, str(output))
            runtime.ledger.evidence_configuration["evidence_policy"] = {}
            self.expire(runtime.ledger)
            with self.assertRaisesRegex(LedgerError, "expired"):
                export_review(runtime.ledger, run, str(output))

    def test_cleanup_is_project_scoped_and_cli_dry_run_is_default(self):
        self.configure(project={"retention_seconds": 1}, second=True)
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = self.prepared(runtime)
            output = self.root / "scoped"
            export_review(runtime.ledger, run, str(output))
            self.expire(runtime.ledger)
            self.assertEqual([], cleanup(runtime.ledger, "other", apply=True))
            self.assertTrue(output.exists())
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, main(["evidence-cleanup", "--config", str(self.config.path), "--project", "demo"]))
        self.assertFalse(json.loads(stdout.getvalue())["apply"])
        self.assertTrue(output.exists())

    def test_destination_project_allowlist_cannot_be_bypassed_by_run(self):
        self.configure(second=True)
        self.configure({"destinations": {
            "other-only": {"kind": "local_review", "enabled": True, "root": str(self.root / "other"), "projects": ["other"]}}})
        with self.assertRaisesRegex(ValueError, "not approved for this project"):
            resolve(self.config.values, "demo", {"destination": "other-only"})
        self.assertEqual("other-only", resolve(self.config.values, "other", {"destination": "other-only"})["effective"]["destination"])

    def test_cleanup_refuses_replacement_root_after_validation(self):
        import shutil
        self.configure(project={"retention_seconds": 1})
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = self.prepared(runtime)
            output = self.root / "replace"
            export_review(runtime.ledger, run, str(output))
            saved = self.root / "original"
            self.expire(runtime.ledger)
            def replace(boundary):
                if boundary == "before_evidence_cleanup":
                    output.rename(saved)
                    shutil.copytree(saved, output)
            runtime.ledger.fault_hook = replace
            self.assertEqual("needs_attention", cleanup(runtime.ledger, "demo", apply=True)[0]["status"])
            self.assertTrue((output / "review.json").is_file())
            self.assertTrue((saved / "review.json").is_file())

    def test_publication_never_replaces_concurrent_empty_output(self):
        with FactoryRuntime(self.config, runner=fixture.EditingRunner()) as runtime:
            run = self.prepared(runtime)
            output = self.root / "collision"
            inode = []
            def collide(boundary):
                if boundary == "before_evidence_publish":
                    output.mkdir()
                    inode.append(output.stat().st_ino)
            runtime.ledger.fault_hook = collide
            with self.assertRaises(FileExistsError):
                export_review(runtime.ledger, run, str(output))
            self.assertEqual(inode[0], output.stat().st_ino)
            self.assertEqual([], list(output.iterdir()))

    def test_bearer_cookie_and_secret_fields_are_completely_scrubbed(self):
        from dotfactory.evidence_bundle import _redact
        value = {"headers": "Authorization: Bearer " + "opaque-value-123\nCookie: one=value; two=private",
                 "quoted": 'password="multi word private phrase"',
                 "access_token": "private-token-value", "ok": "keep"}
        redacted = _redact(value, set())
        self.assertEqual({"headers": "[REDACTED]", "quoted": "[REDACTED]",
                          "access_token": "[REDACTED]", "ok": "keep"}, redacted)


if __name__ == "__main__":
    unittest.main()
