import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from dotfactory.cli import _demo_config, _git
from dotfactory.delivery import (
    DeliveryError, _verify, evaluate, export_review, git, local_file,
    planned_receipt, require_receipt,
)
from dotfactory.control import Principal, ControlError
from dotfactory.handoff import build_handoff
from dotfactory.instance import FactoryConfig
from dotfactory.kernel import KernelError
from dotfactory.lifecycle import FactoryRuntime
from dotfactory.live_runner import CodexAdapter
from dotfactory.runner import RunnerResult, runner_request
from dotfactory.workflow import WorkflowError, load_workflow

ROOT = Path(__file__).resolve().parents[1]
VERIFY = '''import pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root))
from greeting import greet
assert greet() == "こんにちは", greet()
print("PASS description requirement: Japanese greeting")
'''
BROKEN_ARGV_VERIFY = '''import sys, unittest
class GreetingTest(unittest.TestCase):
    def test_placeholder(self):
        self.assertTrue(True)
unittest.main()
'''


class ApprovedRuntime(FactoryRuntime):
    """Test harness acting as the human approver; production never auto-approves."""
    def step(self):
        result = super().step()
        for row in self.ledger.list_runs(status="running"):
            if row["current_state_id"] == "PlanReview":
                approve_plan(self, row["id"])
        return result


def approve_plan(runtime, execution):
    from dotfactory.delivery import planned_receipt
    head = planned_receipt(runtime.ledger, execution)["receipt"]["source"]["head_sha"]
    state = runtime.ledger.current(execution)["current_state_id"]
    result = runtime.control_service("demo").execute(
        execution, command_id="approve-" + head,
        principal=Principal("reviewer", "approver", "test"),
        request={"action": "approve", "expected_state": state,
                 "parameters": {"plan_sha": head, "note": "Reviewed acceptance coverage",
                                **({"owner": "verifier"} if state == "ReplanReview" else {})}})
    if result["status"] != "completed":
        raise AssertionError(result)


class EditingRunner:
    def __init__(self, *, wrong=False, missing=False, tamper=False):
        self.wrong, self.missing, self.tamper = wrong, missing, tamper
        self.calls = []

    def run(self, launch):
        root = Path(launch.workspace_path)
        self.calls.append(launch.request.state_id)
        (root / ".factory").mkdir(exist_ok=True)
        _git(root, "config", "user.email", "test@example.invalid")
        _git(root, "config", "user.name", "Fixture")
        (root / ".factory/plan.md").write_text("Implement the Japanese greeting requirement; run pinned checks.")
        if launch.request.state_id in ("Autoplanning", "Planning"):
            (root / ".factory/verify.py").write_text(VERIFY)
            (root / ".factory/verification.json").write_text(json.dumps({
                "schema_version": 1, "criteria": [
                    {"id": "greeting", "requirement": "Return Japanese greeting", "kind": "automated", "files": [".factory/verify.py"]},
                    {"id": "review", "requirement": "Review wording", "kind": "manual", "procedure": "Read the greeting text."}]}))
        if launch.request.state_id not in ("Autoplanning", "Planning"):
            greeting = 'wrong' if self.wrong else 'こんにちは'
            (root / "greeting.py").write_text(f'def greet():\n    return {greeting!r}\n')
            (root / ".factory/delivery.json").write_text(json.dumps({
                "summary": "Implement Japanese greeting", "limitations": ["No UI tested"]}))
            if self.tamper:
                (root / ".factory/verify.py").write_text('print("fake pass")\n')
        _git(root, "add", ".")
        _git(root, "commit", "--allow-empty", "-m", "delivery fixture")
        path = ".factory/plan.md" if launch.request.state_id in ("Autoplanning", "Planning") else ".factory/delivery.json"
        return RunnerResult("Committed the requested deliverables.", "complete", ({"kind": "proof", "uri": "missing.md" if self.missing else path},))


class VerifiedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-proof-", dir="/tmp")
        self.root = Path(self.temp.name)
        path = _demo_config(self.root)
        repo = self.root / "repository"
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "Fixture")
        (repo / ".factory").mkdir()
        (repo / ".factory/verify.py").write_text(VERIFY)
        (repo / "greeting.py").write_text('def greet():\n    return "hello"\n')
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "pin verification policy")
        _git(repo, "push", "origin", "main")
        values = json.loads(path.read_text())
        # Keep coverage for saved, pre-continuation workflow snapshots.
        workflow = self.root / "reviewed.dot"
        workflow.write_text((ROOT / "workflows/verified-python.dot").read_text().replace(
            '  Autoplanning -> PlanReview [on=review]\n', '').replace(
            'Autoplanning -> Ready [on=complete]', 'Autoplanning -> PlanReview [on=complete]').replace(
            'prompt="prompts/', f'prompt="{ROOT}/workflows/prompts/'))
        values["workflows"]["default"]["path"] = str(workflow)
        path.write_text(json.dumps(values))
        self.config = FactoryConfig.load(path)

    def use_automatic_workflow(self):
        path = self.root / "automatic.json"
        values = dict(self.config.values)
        values["workflows"] = {"default": {"path": str(ROOT / "workflows/verified-python.dot")}}
        path.write_text(json.dumps(values))
        self.config = FactoryConfig.load(path)

    def test_autoplan_pins_checks_and_continues_after_restart_without_human(self):
        from dotfactory.delivery import approved_plan
        self.use_automatic_workflow()
        runner = EditingRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "AUTO-1")
            runtime.run([execution], until_state="Ready")
            self.assertEqual(["Autoplanning"], runner.calls)
            plan = approved_plan(runtime.ledger, execution)
            self.assertTrue(plan["head_sha"])
        with FactoryRuntime(self.config, runner=runner) as runtime:
            receipt = runtime.run([execution], until_state="Review")
            self.assertEqual("target_state", receipt.shutdown_reason)
            self.assertEqual(plan, approved_plan(runtime.ledger, execution))
            self.assertEqual(0, runtime.ledger.connection.execute(
                "SELECT count(*) FROM transition_decisions WHERE actor='human'").fetchone()[0])
        self.assertEqual(["Autoplanning", "Implementing", "Verifying"], runner.calls)

    def test_automatic_workflow_manual_planning_still_waits(self):
        self.use_automatic_workflow()
        runner = EditingRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "MANUAL-1")
            runtime.control_service("demo").execute(
                execution, command_id="manual-plan", principal=Principal("reviewer", "approver", "test"),
                request={"action": "transition", "expected_state": "Todo", "parameters": {
                    "to_state": "Planning", "owner": "reviewer"}})
            runtime.run([execution], until_state="PlanReview")
            runtime.step()
            self.assertEqual(["Planning"], runner.calls)
            approve_plan(runtime, execution)
            runtime.run([execution], until_state="Review")
            self.assertEqual("Review", runtime.ledger.current(execution)["current_state_id"])

    def test_automatic_plan_tampering_blocks_implementation(self):
        self.use_automatic_workflow()
        runner = EditingRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "AUTO-TAMPER")
            runtime.run([execution], until_state="Ready")
            root = Path(runtime.ledger.workspace_for_execution(execution)["path"])
            (root / ".factory/verify.py").write_text('print("fake pass")\n')
            _git(root, "add", ".")
            _git(root, "commit", "-m", "tamper")
            runtime.step()
            self.assertEqual(["Autoplanning"], runner.calls)
            self.assertEqual("Ready", runtime.ledger.current(execution)["current_state_id"])

    def test_failed_autoplan_validation_never_authorizes_implementation(self):
        self.use_automatic_workflow()
        runner = EditingRunner(missing=True)
        with FactoryRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "AUTO-FAIL")
            runtime.step()
            self.assertEqual("Investigating", runtime.ledger.current(execution)["current_state_id"])
            self.assertEqual(["Autoplanning"], runner.calls)

    def tearDown(self):
        self.temp.cleanup()

    def test_planning_defines_checks_and_waits_for_exact_human_approval(self):
        repo = self.root / "repository"
        _git(repo, "rm", ".factory/verify.py")
        _git(repo, "commit", "-m", "start without a verifier")
        _git(repo, "push", "origin", "main")
        runner = EditingRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1", description="Return Japanese greeting")
            runtime.run([execution], until_state="PlanReview")
            runtime.step()
            self.assertEqual(["Autoplanning"], runner.calls)
            exported = export_review(runtime.ledger, execution, str(self.root / "proposed"))
            self.assertTrue((self.root / "proposed/checks/.factory/verify.py").is_file())
            proposal = json.loads((self.root / "proposed/verification-plan.json").read_text())
            self.assertEqual("manual", proposal["definition"]["criteria"][1]["kind"])
            self.assertNotIn("verification", json.loads((self.root / "proposed/review.json").read_text())["check"])
            with self.assertRaisesRegex(ControlError, "exact reviewed plan_sha"):
                runtime.control_service("demo").execute(
                    execution, command_id="wrong-plan", principal=Principal("reviewer", "approver", "test"),
                    request={"action": "approve", "expected_state": "PlanReview", "parameters": {"plan_sha": "0" * 40}})
            approve_plan(runtime, execution)
        # Approval survives restart; implementation uses that planning commit.
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runtime.run([execution], until_state="Review")
            export_review(runtime.ledger, execution, str(self.root / "final"))
            proof = json.loads((self.root / "final/review.json").read_text())["check"]
            self.assertEqual(exported["head_sha"], proof["approved_plan"]["head_sha"])
            self.assertTrue(proof["passed"])

    def test_nested_checks_wait_for_approval_and_stay_frozen_after_restart(self):
        class NestedRunner(EditingRunner):
            def run(self, launch):
                result = super().run(launch)
                if launch.request.state_id == "Autoplanning":
                    root = Path(launch.workspace_path)
                    check = root / "factory/tests/test_acceptance.py"
                    check.parent.mkdir(parents=True)
                    check.write_text("raise RuntimeError('must not execute during planning')\n")
                    path = root / ".factory/verification.json"
                    definition = json.loads(path.read_text())
                    definition["criteria"][0]["files"].append("factory/tests/test_acceptance.py")
                    path.write_text(json.dumps(definition))
                    _git(root, "add", ".")
                    _git(root, "commit", "-m", "declare nested check")
                return result
        runner = NestedRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "NESTED-1")
            runtime.run([execution], until_state="PlanReview")
            runtime.step()
            self.assertEqual(["Autoplanning"], runner.calls)
            self.assertEqual("PlanReview", runtime.ledger.current(execution)["current_state_id"])
            approve_plan(runtime, execution)
            root = Path(runtime.ledger.workspace_for_execution(execution)["path"])
        with FactoryRuntime(self.config, runner=runner) as runtime:
            from dotfactory.delivery import approved_plan
            self.assertIn("factory/tests/test_acceptance.py", approved_plan(runtime.ledger, execution)["files"])
            (root / "factory/tests/test_acceptance.py").write_text("pass\n")
            with self.assertRaisesRegex(DeliveryError, "approved verification file changed"):
                approved_plan(runtime.ledger, execution)

    def test_planned_check_path_policy_rejects_aliases_and_production_paths(self):
        from dotfactory.delivery import _planned_check_path
        for name in ("tests/test_a.py", "factory/tests/test_a.py", "packages/a/tests/helpers/a.py", ".factory/verify.py"):
            with self.subTest(name=name):
                self.assertTrue(_planned_check_path(name))
        for name in (None, "tests.py", "src/main.py", "factory/testing/test_a.py", "/tests/a.py",
                     "tests/../src/a.py", "a/../tests/a.py", "tests/./a.py", "tests//a.py",
                     "tests/%2e%2e/a.py", "tests/a.py?x.py", "tests/a.py#x.py", "file:tests/a.py",
                     "tests\\a.py", "tests/.git/a.py", "tests/check.sh", "pkg/.factory/a.py"):
            with self.subTest(name=name):
                self.assertFalse(_planned_check_path(name))

    def test_nested_checks_still_require_tracked_regular_files(self):
        from dotfactory.delivery import verification_definition
        root = self.root / "repository"
        (root / ".factory/plan.md").write_text("Proposed check\n")
        definition = {"schema_version": 1, "criteria": [{"id": "a", "requirement": "a", "kind": "automated", "files": ["factory/tests/a.py"]}]}
        (root / ".factory/verification.json").write_text(json.dumps(definition))
        _git(root, "add", ".factory")
        check = root / "factory/tests/a.py"
        check.parent.mkdir(parents=True)
        check.write_text("pass\n")
        with self.assertRaises(DeliveryError):
            verification_definition(root)
        check.unlink()
        check.symlink_to(root / ".factory/verify.py")
        _git(root, "add", "factory/tests/a.py")
        with self.assertRaisesRegex(DeliveryError, "symlink"):
            verification_definition(root)
        check.unlink()
        check.write_text("unfinished = (\n")
        _git(root, "add", "factory/tests/a.py")
        with self.assertRaisesRegex(DeliveryError, "invalid planned Python check"):
            verification_definition(root)

    def test_linear_approval_requires_exact_plan_commit_in_feedback(self):
        with FactoryRuntime(self.config, runner=EditingRunner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.step()
            decision = runtime.kernels["demo"].observe_linear_status(execution, "Ready", command_id="no-hash")
            self.assertEqual("rejected", decision["payload"]["disposition"])
            from dotfactory.delivery import planned_receipt
            head = planned_receipt(runtime.ledger, execution)["receipt"]["source"]["head_sha"]
            runtime.kernels["demo"].observe_linear_status(execution, "Ready", command_id="with-hash",
                feedback=[{"source": "linear", "kind": "approval", "author": "reviewer", "body": "Approve " + head, "url": "https://example.invalid/comment"}])
            self.assertEqual("Ready", runtime.ledger.current(execution)["current_state_id"])

    def test_implementation_cannot_change_approved_test_helpers(self):
        class HelperRunner(EditingRunner):
            def run(self, launch):
                result = super().run(launch)
                root = Path(launch.workspace_path)
                if launch.request.state_id == "Autoplanning":
                    (root / "tests").mkdir(exist_ok=True)
                    (root / "tests/check_greeting.py").write_text("assert True\n")
                    p = root / ".factory/verification.json"
                    definition = json.loads(p.read_text())
                    definition["criteria"][0]["files"].append("tests/check_greeting.py")
                    p.write_text(json.dumps(definition))
                else:
                    (root / "tests/check_greeting.py").write_text("pass\n")
                _git(root, "add", ".")
                _git(root, "commit", "-m", "test helper")
                return result
        with ApprovedRuntime(self.config, runner=HelperRunner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.step()
            runtime.step()
            self.assertEqual("Investigating", runtime.ledger.current(execution)["current_state_id"])

    def test_plan_change_invalidates_approval_before_launch(self):
        with FactoryRuntime(self.config, runner=EditingRunner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.step()
            approve_plan(runtime, execution)
            root = Path(runtime.ledger.workspace_for_execution(execution)["path"])
            (root / ".factory/plan.md").write_text("unapproved replacement")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "change approved plan")
            runtime.step()
            self.assertEqual("Ready", runtime.ledger.current(execution)["current_state_id"])
            with self.assertRaisesRegex(KernelError, "approved verification file changed"):
                runtime.kernels["demo"].transition(execution, "Implementing", actor="agent", signal="listener_claim", owner="test", command_id="invalid-plan")

    def test_adopted_ready_waits_for_planning_without_launch_or_crash(self):
        runner = EditingRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            execution = runtime.kernels["demo"].begin("demo", "DEMO-READY", {}, command_id="adopt", adopted_state="Ready")
            runtime.step()
            self.assertEqual([], runner.calls)
            runtime.control_service("demo").execute(
                execution, command_id="plan-ready", principal=Principal("reviewer", "approver", "test"),
                request={"action": "transition", "expected_state": "Ready", "parameters": {"to_state": "Planning", "owner": "reviewer"}})
            runtime.step()
            self.assertEqual("PlanReview", runtime.ledger.current(execution)["current_state_id"])

    def test_custom_planreview_without_delivery_contract_keeps_normal_approval(self):
        path = self.root / "custom.dot"
        path.write_text('digraph Custom { start [shape=Mdiamond]; PlanReview [type=human]; done [shape=Msquare]; start -> PlanReview; PlanReview -> done [on=approve]; }')
        config_path = self.root / "factory.json"
        values = json.loads(config_path.read_text())
        values["workflows"]["default"]["path"] = str(path)
        config_path.write_text(json.dumps(values))
        with FactoryRuntime(FactoryConfig.load(config_path), runner=EditingRunner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-CUSTOM")
            result = runtime.control_service("demo").execute(
                execution, command_id="normal-approval", principal=Principal("reviewer", "approver", "test"),
                request={"action": "approve", "expected_state": "PlanReview", "confirmed": True, "parameters": {"note": "Reviewed custom workflow"}})
            self.assertEqual("completed", result["status"])

    def test_rejected_plan_can_be_replanned_before_approval(self):
        runner = EditingRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "DEMO-REPLAN")
            runtime.step()
            runtime.control_service("demo").execute(
                execution, command_id="replan", principal=Principal("reviewer", "approver", "test"),
                request={"action": "transition", "expected_state": "PlanReview", "parameters": {
                    "to_state": "Planning", "owner": "reviewer", "feedback": [{
                        "source": "control_api", "kind": "changes_requested", "author": "reviewer",
                        "body": "Recheck acceptance coverage", "url": "control://replan"}]}})
            runtime.step()
            self.assertEqual(["Autoplanning", "Planning"], runner.calls)
            self.assertEqual("PlanReview", runtime.ledger.current(execution)["current_state_id"])
            approve_plan(runtime, execution)

    def test_real_patch_is_independently_checked_before_review(self):
        runner = EditingRunner()
        with ApprovedRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1", description="Return こんにちは")
            receipt = runtime.run([execution], until_state="Review")
            self.assertEqual("target_state", receipt.shutdown_reason, receipt.as_dict())
            self.assertEqual(["Autoplanning", "Implementing", "Verifying"], runner.calls)
            records = runtime.ledger.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='delivery_checked' ORDER BY seq").fetchall()
            proof = json.loads(records[-1][0])
            self.assertTrue(proof["passed"])
            self.assertEqual(0, proof["verification"]["exit_code"])
            self.assertIn("PASS description requirement", proof["verification"]["output"])
            self.assertIn("greeting.py", proof["source"]["changed_files"])

    def test_failing_check_cannot_advance_to_review(self):
        with ApprovedRuntime(self.config, runner=EditingRunner(wrong=True)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            for _ in range(3):
                runtime.step()
            self.assertEqual("Investigating", runtime.ledger.current(execution)["current_state_id"])
            record = runtime.ledger.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='delivery_checked' ORDER BY seq DESC LIMIT 1").fetchone()
            self.assertFalse(json.loads(record[0])["passed"])
            handoff = build_handoff(runtime.ledger.connection, execution, runner_request(runtime.kernels["demo"], execution).attempt_id)
            self.assertIn("pinned verification failed", json.dumps(handoff))
            self.assertIn("AssertionError", json.dumps(handoff))

    def test_failed_recovery_stops_at_blocked(self):
        class RecoveryFailure(EditingRunner):
            def run(self, launch):
                if launch.request.state_id == "Investigating":
                    return RunnerResult("Recovery timed out", "failed", ({"kind": "error", "uri": "ledger://timeout"},))
                return super().run(launch)
        with ApprovedRuntime(self.config, runner=RecoveryFailure(missing=True)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.step()
            runtime.step()
            self.assertEqual("Blocked", runtime.ledger.current(execution)["current_state_id"])

    def test_approver_can_replace_unusable_frozen_verifier_without_losing_history(self):
        class BrokenThenFixedRunner(EditingRunner):
            def run(self, launch):
                if launch.request.state_id == "Investigating":
                    self.calls.append("Investigating")
                    return RunnerResult(
                        "Frozen verifier treated the export path as a unittest name.",
                        "blocked", ({"kind": "diagnosis", "uri": "ledger://argv-failure"},),
                    )
                if launch.request.state_id == "Replanning":
                    root = Path(launch.workspace_path)
                    (root / ".factory/verify.py").write_text(VERIFY)
                result = super().run(launch)
                if launch.request.state_id == "Autoplanning":
                    root = Path(launch.workspace_path)
                    (root / ".factory/verify.py").write_text(BROKEN_ARGV_VERIFY)
                    _git(root, "add", ".factory/verify.py")
                    _git(root, "commit", "--amend", "--no-edit")
                return result

        self.use_automatic_workflow()
        runner = BrokenThenFixedRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "DEMO-ARGV-REPLAN")
            for _ in range(4):
                runtime.step()
            self.assertEqual("Blocked", runtime.ledger.current(execution)["current_state_id"])
            failed = json.loads(runtime.ledger.connection.execute(
                "SELECT payload_json FROM events WHERE execution_id=? "
                "AND event_type='delivery_checked' AND payload_json LIKE '%pinned verification failed%' "
                "ORDER BY seq DESC LIMIT 1", (execution,),
            ).fetchone()[0])
            self.assertEqual("frozen_verification_failed", failed["failure"]["category"])
            self.assertNotEqual(0, failed["verification"]["exit_code"])
            self.assertIn("has no attribute", failed["verification"]["output"])
            delivery = runtime.control_service("demo").observation.delivery(execution)
            self.assertEqual(
                "frozen_verification_failed", delivery["data"]["failure"]["category"],
            )
            actions = runtime.control_service("demo").observation.run(execution)["data"]["available_actions"]
            replan = next(item for item in actions if item["action"] == "replan")
            self.assertTrue(replan["confirmation_required"])

            denied = runtime.control_service("demo").execute(
                execution, command_id="operator-replan",
                principal=Principal("operator", "operator", "test"),
                request={"action": "replan", "expected_state": "Blocked", "confirmed": True,
                         "parameters": {"owner": "planner", "reason": "argv harness is unusable"}},
            )
            self.assertEqual("denied", denied["status"])
            root = Path(runtime.ledger.workspace_for_execution(execution)["path"])
            greeting = (root / "greeting.py").read_text()
            (root / "greeting.py").write_text('def greet():\n    return "changed after failure"\n')
            with self.assertRaisesRegex(ControlError, "commit all delivery files"):
                runtime.control_service("demo").execute(
                    execution, command_id="dirty-replan",
                    principal=Principal("reviewer", "approver", "test"),
                    request={"action": "replan", "expected_state": "Blocked", "confirmed": True,
                             "parameters": {"owner": "planner", "reason": "argv harness is unusable"}},
                )
            (root / "greeting.py").write_text(greeting)
            runtime.control_service("demo").execute(
                execution, command_id="approved-replan",
                principal=Principal("reviewer", "approver", "test"),
                request={"action": "replan", "expected_state": "Blocked", "confirmed": True,
                         "parameters": {"owner": "planner", "reason": "Remove export path before unittest parses argv"}},
            )
            runtime.step()
            self.assertEqual("ReplanReview", runtime.ledger.current(execution)["current_state_id"])
            replacement = planned_receipt(runtime.ledger, execution)["receipt"]
            self.assertEqual(
                failed["approved_plan"]["plan_attempt_id"],
                replacement["replan"]["replaces_plan_attempt_id"],
            )
            packet = export_review(runtime.ledger, execution, str(self.root / "replan-review"))
            self.assertEqual(replacement["source"]["head_sha"], packet["head_sha"])
            approve_plan(runtime, execution)
            runtime.step()
            latest = json.loads(runtime.ledger.connection.execute(
                "SELECT payload_json FROM events WHERE execution_id=? "
                "AND event_type='delivery_checked' ORDER BY seq DESC LIMIT 1",
                (execution,),
            ).fetchone()[0])
            self.assertEqual(
                "Review", runtime.ledger.current(execution)["current_state_id"],
                json.dumps(latest),
            )
            failures = runtime.ledger.connection.execute(
                "SELECT count(*) FROM events WHERE execution_id=? "
                "AND event_type='delivery_checked' AND payload_json LIKE '%frozen_verification_failed%'",
                (execution,),
            ).fetchone()[0]
            self.assertEqual(1, failures)
            self.assertEqual(
                ["Autoplanning", "Implementing", "Verifying", "Investigating", "Replanning", "Verifying"],
                runner.calls,
            )

    def test_nonexistent_evidence_cannot_complete_planning(self):
        with ApprovedRuntime(self.config, runner=EditingRunner(missing=True)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.step()
            self.assertEqual("Investigating", runtime.ledger.current(execution)["current_state_id"])

    def test_agent_cannot_replace_the_pinned_check(self):
        with ApprovedRuntime(self.config, runner=EditingRunner(tamper=True)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            for _ in range(3):
                runtime.step()
            self.assertEqual("Investigating", runtime.ledger.current(execution)["current_state_id"])

    def test_kernel_cannot_be_bypassed_with_fabricated_evidence(self):
        with ApprovedRuntime(self.config, runner=EditingRunner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime._claim_pickups()
            request = runner_request(runtime.kernels["demo"], execution)
            with self.assertRaisesRegex(KernelError, "host delivery check"):
                runtime.kernels["demo"].complete_attempt(
                    execution, preferred_label="complete", outcome="succeeded",
                    evidence=[{"kind": "delivery_check", "uri": "ledger://fabricated"}],
                    attempt_id=request.attempt_id, fence_token=request.fence_token,
                    owner=request.owner, command_id="bypass")

    def test_linear_status_cannot_bypass_host_verification(self):
        with ApprovedRuntime(self.config, runner=EditingRunner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.step()
            runtime.step()
            request = runner_request(runtime.kernels["demo"], execution)
            result = runtime.kernels["demo"].observe_linear_status(
                execution, "Review", command_id="bypass", attempt_id=request.attempt_id,
                fence_token=request.fence_token, outcome="human claims success",
                evidence=[{"kind": "proof", "uri": "fake://proof"}])
            self.assertEqual("rejected", result["payload"]["disposition"])
            self.assertEqual("Verifying", runtime.ledger.current(execution)["current_state_id"])

    def test_legacy_json_rejects_unknown_contract(self):
        source = json.loads((ROOT / "workflow.json").read_text())
        source["states"][0]["execution"] = {"exit_contract": "invented-v99"}
        path = self.root / "unknown.json"
        path.write_text(json.dumps(source))
        with self.assertRaisesRegex(WorkflowError, "unsupported exit contract"):
            load_workflow(path)

    def test_changed_source_invalidates_saved_check(self):
        with ApprovedRuntime(self.config, runner=EditingRunner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime._claim_pickups()
            request = runner_request(runtime.kernels["demo"], execution)
            launch = runtime.projects["demo"].preparation.prepare(request).launch
            prompt = CodexAdapter().stdin(launch, prompt_text="Perform this delivery step")
            self.assertIn("Every evidence URI must be an existing committed workspace file", prompt)
            result = runtime.scheduler.runner.run(launch)
            evaluate(runtime.ledger, launch, result)
            root = Path(launch.workspace_path)
            (root / ".factory/plan.md").write_text("changed after check")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "invalidate")
            with self.assertRaisesRegex(DeliveryError, "source changed"):
                require_receipt(runtime.ledger, request.attempt_id, "plan-result-v2")

    def test_unknown_contract_rejected_at_workflow_load(self):
        path = self.root / "unknown.dot"
        source = (ROOT / "workflows/three-step.dot").read_text().replace('type=agent', 'type=agent, exit_contract="invented-v99"')
        path.write_text(source)
        with self.assertRaisesRegex(WorkflowError, "unsupported exit contract"):
            load_workflow(path)

    def test_review_packet_and_human_rework(self):
        runner = EditingRunner()
        with ApprovedRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.run([execution], until_state="Review")
            packet = export_review(runtime.ledger, execution, str(self.root / "review"))
            self.assertEqual(64, len(packet["patch_sha256"]))
            self.assertIn("こんにちは", (self.root / "review/change.patch").read_text())
            runtime.control_service("demo").execute(
                execution, command_id="revise", principal=Principal("reviewer", "approver", "test"),
                request={"action": "transition", "expected_state": "Review",
                         "parameters": {"to_state": "Reworking", "owner": "reviewer",
                             "feedback": [{"source": "control_api", "kind": "changes_requested",
                                "author": "reviewer", "body": "Retain the Japanese greeting and clarify the report.",
                                "url": "control://revision"}]}},
            )
            runtime.run([execution], max_ticks=10)
            self.assertEqual("Review", runtime.ledger.current(execution)["current_state_id"])
            self.assertEqual(1, runner.calls.count("Reworking"))
            export_review(runtime.ledger, execution, str(self.root / "revised"))
            packet = json.loads((self.root / "revised/review.json").read_text())
            self.assertIn("clarify the report", json.dumps(packet["context"]["feedback"]))
            self.assertIn("intent", packet)


    def test_saved_host_check_is_reused_before_dispatch_result_is_recorded(self):
        with ApprovedRuntime(self.config, runner=EditingRunner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.step()
            runtime.step()
            request = runner_request(runtime.kernels["demo"], execution)
            launch = runtime.projects["demo"].preparation.prepare(request).launch
            result = runtime.scheduler.runner.run(launch)
            first = evaluate(runtime.ledger, launch, result)
            with patch("dotfactory.delivery._verify", side_effect=AssertionError("verifier ran twice")):
                second = evaluate(runtime.ledger, launch, result)
            self.assertEqual(first, second)

    def test_recovery_commits_saved_check_without_rerunning_agent_or_verifier(self):
        class Crash(BaseException):
            pass
        runner = EditingRunner()
        with ApprovedRuntime(self.config, runner=runner) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.step()
            runtime.step()
            def crash(boundary):
                if boundary == "after_result_recorded":
                    raise Crash()
            runtime.scheduler.fault_hook = crash
            with self.assertRaises(Crash):
                runtime.step()
        with ApprovedRuntime(self.config, runner=runner) as runtime:
            runtime.run([execution], until_state="Review")
            self.assertEqual(["Autoplanning", "Implementing", "Verifying"], runner.calls)
            self.assertEqual(3, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='delivery_checked'").fetchone()[0])

    def test_path_escape_and_symlink_are_rejected(self):
        external = self.root / "outside.txt"
        external.write_text("not evidence")
        root = self.root / "evidence"
        root.mkdir()
        (root / "link").symlink_to(external)
        for uri in ("../outside.txt", str(external), "link", "https://example.invalid/proof"):
            with self.subTest(uri=uri), self.assertRaises(DeliveryError):
                local_file(root, uri)

    def test_verifier_uses_git_objects_despite_export_attributes_and_ignored_files(self):
        repo = self.root / "repository"
        (repo / "greeting.py").write_text('def greet():\n    return "こんにちは"\n')
        (repo / ".gitattributes").write_text("greeting.py export-ignore\n")
        (repo / ".gitignore").write_text("ignored.py\n")
        (repo / "ignored.py").write_text("raise RuntimeError('not committed')\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "export attributes must not change checked source")
        head = git(repo, "rev-parse", "HEAD").decode().strip()
        proof = _verify(repo, head, None)
        self.assertEqual(0, proof["exit_code"])

    def test_verifier_cancellation_terminates_owned_process(self):
        repo = self.root / "repository"
        (repo / ".factory/verify.py").write_text("import time\ntime.sleep(120)\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "cancellation fixture")
        head = git(repo, "rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(DeliveryError, "canceled"):
            _verify(repo, head, lambda: True)

    def test_cancellation_does_not_require_a_successful_check(self):
        with ApprovedRuntime(self.config, runner=EditingRunner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime._claim_pickups()
            receipt = runtime.control_service("demo").execute(
                execution, command_id="cancel", principal=Principal("reviewer", "approver", "test"),
                request={"action": "cancel", "expected_state": "Autoplanning", "confirmed": True})
            self.assertEqual("completed", receipt["status"])


if __name__ == "__main__":
    unittest.main()
