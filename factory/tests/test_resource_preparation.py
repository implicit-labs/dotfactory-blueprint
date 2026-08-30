import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory import (  # noqa: E402
    ControlError, ControlService, DurableKernel, FakePreparedRunner, Principal,
    RunnerResult, SQLiteLedger,
    run_prepared_attempt,
)
from dotfactory.ledger import (  # noqa: E402
    LedgerError, ResourceBusy, StaleAttempt, parse_timestamp,
)
from dotfactory.resources import (  # noqa: E402
    CapabilityPlan, PreparationBusy, PreparationEngine,
    PreparationNeedsAttention, ProviderActivation,
)
from dotfactory.portless import PortlessProvider  # noqa: E402
from dotfactory.runner import runner_request  # noqa: E402
from dotfactory.workspace import (  # noqa: E402
    GitWorkspaceProvider, WorkspaceConflict, WorkspaceHandle,
    WorkspaceUnsafeCleanup,
)


class FakeWorkspaceProvider:
    def __init__(self, root: Path):
        self.root = root
        self.created = 0

    def materialize(
        self, *, repository_path, root, remote, base_ref, issue_identifier,
        execution_number,
    ):
        self.created += 1
        path = self.root / f"{issue_identifier}-{execution_number}"
        path.mkdir(parents=True, exist_ok=False)
        return WorkspaceHandle(
            repository_path=str(Path(repository_path).resolve()),
            git_common_dir=str(self.root / "common.git"), remote=remote,
            base_ref=base_ref, base_sha="a" * 40,
            branch_name=f"factory/{issue_identifier.lower()}-{execution_number}",
            path=str(path),
        )

    def reconcile(self, handle):
        if not Path(handle.path).is_dir():
            raise RuntimeError("missing workspace")
        return handle

    def cleanup(self, handle):
        Path(handle.path).rmdir()


class FakeResourceProvider:
    def __init__(self):
        self.activations = []
        self.cleaned = []
        self.busy = False
        self.fail_capability = None
        self.attention_capability = None

    def plan(self, *, capability, config, workspace):
        if self.busy:
            raise PreparationBusy("fixture resource is busy", retry_after_seconds=3)
        if capability == self.attention_capability:
            raise PreparationNeedsAttention(
                "fixture needs attention", category="unhealthy",
                detail={"last_safe_step": "fixture plan",
                        "allowed_actions": ["retry", "cancel"]},
                capability=capability, provider="fixture",
            )
        return CapabilityPlan(
            provider="fixture", capability=capability, scope="attempt",
            resource_id=f"fixture:{capability}", target=capability, config=config,
        )

    def activate(self, plan, *, workspace, owner_token):
        if plan.capability == self.fail_capability:
            raise RuntimeError("fixture activation failed")
        activation = ProviderActivation(
            resource_id=plan.resource_id,
            environment=((f"{plan.capability.upper()}_URL", "https://fixture.localhost"),),
            urls=("https://fixture.localhost",),
            metadata={"api_token": "never-persist-this"}, handle=owner_token,
        )
        self.activations.append(activation)
        return activation

    def reconcile(self, allocation, *, workspace, owner_token):
        for activation in self.activations:
            if activation.resource_id == allocation["resource_id"]:
                return activation
        raise PreparationNeedsAttention(
            "fixture cannot reconcile", category="unsafe-cleanup",
            detail={"allowed_actions": ["quarantine"]}, provider="fixture",
        )

    def cleanup(self, activation, *, owner_token):
        if activation.handle != owner_token:
            raise RuntimeError("differently owned")
        if activation.resource_id not in self.cleaned:
            self.cleaned.append(activation.resource_id)
        return {"cleaned": activation.resource_id}


class SimulatedCrash(BaseException):
    pass


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = SQLiteLedger(self.root / "factory.db")
        self.ledger.configure_factory("test-factory")
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="linear-dotfactory",
        )
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")
        self.execution = self.kernel.begin(
            "dotfactory", "TASK-569", {"title": "prepare"}, command_id="begin",
        )
        self.claim = self.kernel.transition(
            self.execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner", command_id="claim",
        )
        self.request = replace(
            runner_request(self.kernel, self.execution), config={"resources": ["alpha"]},
        )
        self.workspace = FakeWorkspaceProvider(self.root / "worktrees")
        self.provider = FakeResourceProvider()
        self.engine = PreparationEngine(
            self.ledger, workspace_provider=self.workspace,
            providers={"fixture": self.provider}, owner_token="factory-owner",
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def configuration(self, capabilities=None):
        names = capabilities or ["alpha"]
        return {
            "workspace": {"root": str(self.root / "worktrees"),
                          "remote": "origin", "base_ref": "main",
                          "retention": "until_terminal"},
            "providers": {"fixture": {"kind": "fixture"}},
            "capabilities": {
                name: {"provider": "fixture", "scope": "attempt",
                       "mode": "exclusive", "config": {}}
                for name in names
            },
        }

    def project(self):
        return {"repository_path": str(self.root / "repository")}

    def test_ready_launch_is_immutable_and_redacts_persistence(self):
        result = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(),
        )
        self.assertEqual("ready", result.disposition)
        self.assertEqual(("https://fixture.localhost",), result.launch.urls)
        self.assertIn("TASK-569-1", result.launch.workspace_path)
        preparation = self.ledger.preparation(result.launch.preparation_id)
        self.assertEqual("ready", preparation["status"])
        persisted = json.dumps(preparation, sort_keys=True)
        self.assertNotIn("never-persist-this", persisted)
        self.assertNotIn(result.launch.workspace_path, persisted)
        binding = self.ledger.current(self.execution)["attempt"]["binding"]
        self.assertNotIn("resources", binding["resolved"])

    def test_only_prepared_launch_runs_and_cleanup_precedes_transition(self):
        prepared = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(),
        ).launch
        runner = FakePreparedRunner([
            RunnerResult(
                outcome="succeeded", preferred_label="complete",
                evidence=({"kind": "plan", "uri": "local://plan"},),
            )
        ])
        result = run_prepared_attempt(
            self.kernel, self.engine, prepared, runner, command_id="prepared-complete",
        )
        self.assertEqual("Ready", result["to_state"])
        self.assertEqual(["fixture:alpha"], self.provider.cleaned)
        allocation_status = self.ledger.connection.execute(
            "SELECT status FROM resource_allocations"
        ).fetchone()[0]
        self.assertEqual("released", allocation_status)
        self.assertEqual("active", self.ledger.workspace_for_execution(
            self.execution
        )["status"])
        cleaned = self.engine.cleanup_workspace(self.execution)
        self.assertEqual("ready", cleaned.disposition)
        self.assertEqual("cleaned", self.ledger.workspace_for_execution(
            self.execution
        )["status"])
        self.assertEqual(
            "ready", self.engine.cleanup_workspace(self.execution).disposition
        )
        cleanup_statuses = [row[0] for row in self.ledger.connection.execute(
            "SELECT status FROM cleanup_plans ORDER BY created_at,id"
        )]
        self.assertEqual(["completed", "completed"], cleanup_statuses)

    def test_stale_fence_cannot_prepare_or_launch(self):
        stale = replace(self.request, fence_token="stale")
        with self.assertRaises(StaleAttempt):
            self.engine.prepare(
                stale, project=self.project(),
                preparation_config=self.configuration(),
            )

    def test_preparation_creation_fault_rolls_back_and_retries_cleanly(self):
        def crash(boundary):
            if boundary == "after_preparation_created":
                raise RuntimeError("crash")

        self.ledger.fault_hook = crash
        with self.assertRaisesRegex(RuntimeError, "crash"):
            self.ledger.begin_preparation(
                attempt_id=self.request.attempt_id,
                fence_token=self.request.fence_token, request_digest="request",
            )
        self.assertEqual(0, self.ledger.connection.execute(
            "SELECT COUNT(*) FROM preparations"
        ).fetchone()[0])
        self.ledger.fault_hook = None
        preparation = self.ledger.begin_preparation(
            attempt_id=self.request.attempt_id,
            fence_token=self.request.fence_token, request_digest="request",
        )
        self.assertEqual("preparing", preparation["status"])

    def test_allocation_race_has_one_owner(self):
        second_execution = self.kernel.begin(
            "dotfactory", "TASK-570", {"title": "second"}, command_id="begin-second",
        )
        second_claim = self.kernel.transition(
            second_execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-2", command_id="claim-second",
        )
        first = self.ledger.begin_preparation(
            attempt_id=self.request.attempt_id,
            fence_token=self.request.fence_token, request_digest="first",
        )
        second = self.ledger.begin_preparation(
            attempt_id=second_claim["attempt_id"],
            fence_token=second_claim["fence_token"], request_digest="second",
        )
        self.ledger.acquire_allocation(
            first["id"], fence_token=self.request.fence_token, scope="attempt",
            provider="fixture", capability="simulator", resource_id="simulator:one",
        )
        with self.assertRaises(ResourceBusy):
            self.ledger.acquire_allocation(
                second["id"], fence_token=second_claim["fence_token"],
                scope="attempt", provider="fixture", capability="simulator",
                resource_id="simulator:one",
            )
        self.assertEqual(1, self.ledger.connection.execute(
            "SELECT COUNT(*) FROM resource_allocations WHERE status='active'"
        ).fetchone()[0])

    def test_partial_failure_compensates_in_reverse_and_never_becomes_ready(self):
        self.request = replace(self.request, config={"resources": ["alpha", "beta"]})
        self.provider.fail_capability = "beta"
        result = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(["alpha", "beta"]),
        )
        self.assertEqual("fatal", result.disposition)
        self.assertIsNone(result.launch)
        self.assertEqual(["fixture:alpha"], self.provider.cleaned)
        rows = self.ledger.connection.execute(
            "SELECT status FROM resource_allocations ORDER BY acquired_at,id"
        ).fetchall()
        self.assertEqual(["released", "released"], [row[0] for row in rows])

    def test_contention_retries_without_completing_or_revisiting_attempt(self):
        self.provider.busy = True
        before = len(self.ledger.run_history(self.execution)["state_runs"])
        first = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(),
        )
        self.assertEqual("busy", first.disposition)
        self.assertEqual(5, first.retry_after_seconds)
        preparation = self.ledger.connection.execute(
            "SELECT id,error_json FROM preparations"
        ).fetchone()
        retry = json.loads(preparation["error_json"])
        retry["next_retry_at"] = "2000-01-01T00:00:00+00:00"
        self.ledger.connection.execute(
            "UPDATE preparations SET error_json=? WHERE id=?",
            (json.dumps(retry, sort_keys=True), preparation["id"]),
        )
        second_busy = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(),
        )
        self.assertEqual("busy", second_busy.disposition)
        retry = json.loads(self.ledger.connection.execute(
            "SELECT error_json FROM preparations"
        ).fetchone()["error_json"])
        retry["next_retry_at"] = "2000-01-01T00:00:00+00:00"
        self.ledger.connection.execute(
            "UPDATE preparations SET error_json=?",
            (json.dumps(retry, sort_keys=True),),
        )
        self.provider.busy = False
        third = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(),
        )
        self.assertEqual("ready", third.disposition)
        self.assertEqual(2, self.ledger.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='preparation_busy'"
        ).fetchone()[0])
        self.assertEqual(before, len(self.ledger.run_history(self.execution)["state_runs"]))
        self.assertEqual("active", self.ledger.current(self.execution)["attempt"]["status"])

    def test_contention_deadline_is_separate_from_runner_retries(self):
        self.provider.busy = True
        first = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config={
                **self.configuration(),
                "retry": {
                    "initial_seconds": 1,
                    "maximum_seconds": 2,
                    "deadline_seconds": 3,
                },
            },
        )
        self.assertEqual("busy", first.disposition)
        preparation = self.ledger.connection.execute(
            "SELECT created_at FROM preparations"
        ).fetchone()
        future = parse_timestamp(preparation["created_at"]) + timedelta(seconds=4)
        self.ledger.clock = lambda: future.isoformat()
        exhausted = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config={
                **self.configuration(),
                "retry": {
                    "initial_seconds": 1,
                    "maximum_seconds": 2,
                    "deadline_seconds": 3,
                },
            },
        )
        self.assertEqual("needs_attention", exhausted.disposition)
        self.assertEqual("exhausted", exhausted.attention["category"])
        self.assertEqual(
            "active", self.ledger.current(self.execution)["attempt"]["status"]
        )
        self.assertEqual(1, len(self.ledger.run_history(self.execution)["attempts"]))

    def test_started_activation_reconciles_after_process_restart(self):
        def crash(boundary):
            if boundary == "after_provider_activated":
                raise SimulatedCrash(boundary)

        crashing = PreparationEngine(
            self.ledger, workspace_provider=self.workspace,
            providers={"fixture": self.provider}, owner_token="factory-owner",
            fault_hook=crash,
        )
        with self.assertRaises(SimulatedCrash):
            crashing.prepare(
                self.request, project=self.project(),
                preparation_config=self.configuration(),
            )
        mutation = self.ledger.connection.execute(
            "SELECT status FROM resource_mutations WHERE provider='fixture'"
        ).fetchone()
        self.assertEqual("started", mutation["status"])
        restarted = PreparationEngine(
            self.ledger, workspace_provider=self.workspace,
            providers={"fixture": self.provider}, owner_token="factory-owner",
        )
        recovered = restarted.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(),
        )
        self.assertEqual("ready", recovered.disposition)
        self.assertEqual(1, len(self.provider.activations))
        self.assertEqual(
            "completed", self.ledger.connection.execute(
                "SELECT status FROM resource_mutations WHERE provider='fixture'"
            ).fetchone()["status"],
        )

    def test_attention_remedies_are_authorized_fenced_and_not_replayed(self):
        self.provider.attention_capability = "alpha"
        blocked = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(),
        )
        self.assertEqual("needs_attention", blocked.disposition)
        control = ControlService(self.ledger, self.kernel, self.engine)
        request = {
            "action": "attention", "expected_state": "Autoplanning",
            "parameters": {
                "attention_id": blocked.attention["id"], "remedy": "retry",
                "expected_attempt_id": self.request.attempt_id,
            },
        }
        receipt = control.execute(
            self.execution, command_id="retry-resource",
            principal=Principal("operator", "operator", "test"), request=request,
        )
        self.assertEqual("completed", receipt["status"])
        self.assertEqual("resolved", self.ledger.attention(
            blocked.attention["id"]
        )["status"])
        self.assertEqual(
            receipt,
            control.execute(
                self.execution, command_id="retry-resource",
                principal=Principal("operator", "operator", "test"), request=request,
            ),
        )
        with self.assertRaisesRegex(ControlError, "already resolved"):
            control.execute(
                self.execution, command_id="replay-resource",
                principal=Principal("operator", "operator", "test"), request=request,
            )

    def test_attention_rejects_stale_attempt_and_unauthorized_release(self):
        self.provider.attention_capability = "alpha"
        blocked = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(),
        )
        control = ControlService(self.ledger, self.kernel, self.engine)
        stale = {
            "action": "attention", "expected_state": "Autoplanning",
            "parameters": {
                "attention_id": blocked.attention["id"], "remedy": "retry",
                "expected_attempt_id": "stale-attempt",
            },
        }
        with self.assertRaisesRegex(ControlError, "stale"):
            control.execute(
                self.execution, command_id="stale-resource",
                principal=Principal("operator", "operator", "test"), request=stale,
            )
        blocked.attention["detail"]["allowed_actions"].append("release")
        self.ledger.connection.execute(
            "UPDATE attention_requests SET detail_json=? WHERE id=?",
            (json.dumps(blocked.attention["detail"], sort_keys=True),
             blocked.attention["id"]),
        )
        denied = control.execute(
            self.execution, command_id="operator-release",
            principal=Principal("operator", "operator", "test"),
            request={
                "action": "attention", "expected_state": "Autoplanning",
                "confirmed": True,
                "parameters": {
                    "attention_id": blocked.attention["id"], "remedy": "release",
                    "expected_attempt_id": self.request.attempt_id,
                },
            },
        )
        self.assertEqual("denied", denied["status"])

    def test_retain_quarantine_release_and_cancel_remedies_complete(self):
        for index, remedy in enumerate(("retain", "quarantine", "release", "cancel"), 1):
            with self.subTest(remedy=remedy):
                execution = self.kernel.begin(
                    "dotfactory", f"TASK-57{index}", {"title": remedy},
                    command_id=f"begin-{remedy}",
                )
                self.kernel.transition(
                    execution, "Autoplanning", actor="agent", signal="listener_claim",
                    owner="planner", command_id=f"claim-{remedy}",
                )
                request = replace(
                    runner_request(self.kernel, execution),
                    config={"resources": ["alpha"]},
                )
                provider = FakeResourceProvider()
                provider.attention_capability = "alpha"
                engine = PreparationEngine(
                    self.ledger, workspace_provider=self.workspace,
                    providers={"fixture": provider}, owner_token="factory-owner",
                )
                blocked = engine.prepare(
                    request, project=self.project(),
                    preparation_config=self.configuration(),
                )
                detail = dict(blocked.attention["detail"])
                detail["allowed_actions"] = [remedy]
                self.ledger.connection.execute(
                    "UPDATE attention_requests SET detail_json=? WHERE id=?",
                    (json.dumps(detail, sort_keys=True), blocked.attention["id"]),
                )
                principal = Principal(
                    "operator", "approver" if remedy == "release" else "operator", "test"
                )
                receipt = ControlService(
                    self.ledger, self.kernel, engine
                ).execute(
                    execution, command_id=f"remedy-{remedy}", principal=principal,
                    request={
                        "action": "attention", "expected_state": "Autoplanning",
                        "confirmed": remedy == "release",
                        "parameters": {
                            "attention_id": blocked.attention["id"],
                            "remedy": remedy,
                            "expected_attempt_id": request.attempt_id,
                        },
                    },
                )
                self.assertEqual("completed", receipt["status"])
                self.assertEqual(
                    "canceled",
                    self.ledger.preparation(blocked.attention["preparation_id"])["status"],
                )

    def test_attention_requests_deduplicate_and_redact(self):
        first = self.ledger.open_attention(
            execution_id=self.execution, attempt_id=self.request.attempt_id,
            preparation_id=None, dedupe_key="same", category="unauthorized",
            detail={"api_token": "private", "allowed_actions": ["cancel"]},
        )
        second = self.ledger.open_attention(
            execution_id=self.execution, attempt_id=self.request.attempt_id,
            preparation_id=None, dedupe_key="same", category="unauthorized",
            detail={"api_token": "different"},
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual("[REDACTED]", first["detail"]["api_token"])
        count = self.ledger.connection.execute(
            "SELECT COUNT(*) FROM attention_requests"
        ).fetchone()[0]
        self.assertEqual(1, count)

    def test_dirty_workspace_cleanup_quarantines_and_escalates_once(self):
        prepared = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(),
        )
        self.assertEqual("ready", prepared.disposition)

        def dirty(_handle):
            raise WorkspaceUnsafeCleanup("dirty workspace")

        self.workspace.cleanup = dirty
        first = self.engine.cleanup_workspace(self.execution)
        second = self.engine.cleanup_workspace(self.execution)
        self.assertEqual("needs_attention", first.disposition)
        self.assertEqual(first.attention["id"], second.attention["id"])
        self.assertEqual("quarantined", self.ledger.workspace_for_execution(
            self.execution
        )["status"])
        self.assertEqual(1, self.ledger.connection.execute(
            "SELECT COUNT(*) FROM attention_requests WHERE status='open'"
        ).fetchone()[0])

    def test_checkpoint_release_then_rework_reuses_execution_workspace(self):
        first = self.engine.prepare(
            self.request, project=self.project(),
            preparation_config=self.configuration(),
        ).launch
        run_prepared_attempt(
            self.kernel, self.engine, first,
            FakePreparedRunner([RunnerResult(
                outcome="succeeded", preferred_label="complete",
                evidence=({"kind": "plan", "uri": "local://plan"},),
            )]), command_id="finish-planning",
        )
        implementation = self.kernel.transition(
            self.execution, "Implementing", actor="agent", signal="listener_claim",
            owner="builder", command_id="claim-implementation",
        )
        second_request = replace(
            runner_request(self.kernel, self.execution), config={"resources": ["alpha"]},
        )
        self.assertEqual(implementation["attempt_id"], second_request.attempt_id)
        second = self.engine.prepare(
            second_request, project=self.project(),
            preparation_config=self.configuration(),
        ).launch
        self.assertEqual(first.workspace_path, second.workspace_path)
        self.assertEqual(1, self.workspace.created)

    def test_all_preparation_and_cleanup_boundaries_resume_safely(self):
        preparation_boundaries = (
            "after_workspace_ready", "after_workspace_prepared",
            "after_allocation_acquired", "after_provider_activated",
            "after_allocation_ready", "after_preparation_committed",
        )
        cleanup_boundaries = (
            "after_cleanup_planned", "after_provider_cleanup",
            "after_allocation_released", "after_cleanup_finished",
        )
        for target in preparation_boundaries + cleanup_boundaries:
            with self.subTest(boundary=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ledger = SQLiteLedger(root / "factory.db")
                ledger.configure_factory("fault-factory")
                ledger.register_project(
                    "dotfactory", display_name="dotfactory", tracker_kind="linear",
                    tracker_project_id="linear-dotfactory",
                )
                kernel = DurableKernel(ledger, ROOT / "workflows" / "default.dot")
                execution = kernel.begin(
                    "dotfactory", "TASK-569", {"title": "faults"}, command_id="begin",
                )
                kernel.transition(
                    execution, "Autoplanning", actor="agent", signal="listener_claim",
                    owner="planner", command_id="claim",
                )
                request = replace(
                    runner_request(kernel, execution), config={"resources": ["alpha"]},
                )
                workspace = FakeWorkspaceProvider(root / "worktrees")
                provider = FakeResourceProvider()
                fired = []

                def crash(boundary):
                    if boundary == target and not fired:
                        fired.append(boundary)
                        raise SimulatedCrash(boundary)

                engine = PreparationEngine(
                    ledger, workspace_provider=workspace,
                    providers={"fixture": provider}, owner_token="factory-owner",
                    fault_hook=crash,
                )
                config = {
                    "workspace": {
                        "root": str(root / "worktrees"), "remote": "origin",
                        "base_ref": "main", "retention": "until_terminal",
                    },
                    "providers": {"fixture": {"kind": "fixture"}},
                    "capabilities": {
                        "alpha": {"provider": "fixture", "scope": "attempt",
                                  "mode": "exclusive", "config": {}},
                    },
                }
                if target in preparation_boundaries:
                    with self.assertRaises(SimulatedCrash):
                        engine.prepare(
                            request, project={"repository_path": str(root / "repo")},
                            preparation_config=config,
                        )
                    restarted = PreparationEngine(
                        ledger, workspace_provider=workspace,
                        providers={"fixture": provider}, owner_token="factory-owner",
                    )
                    ready = restarted.prepare(
                        request, project={"repository_path": str(root / "repo")},
                        preparation_config=config,
                    )
                    self.assertEqual("ready", ready.disposition)
                else:
                    launch = engine.prepare(
                        request, project={"repository_path": str(root / "repo")},
                        preparation_config=config,
                    ).launch
                    with self.assertRaises(SimulatedCrash):
                        engine.cleanup_attempt(launch)
                    restarted = PreparationEngine(
                        ledger, workspace_provider=workspace,
                        providers={"fixture": provider}, owner_token="factory-owner",
                    )
                    cleaned = restarted.cleanup_attempt(launch)
                    self.assertEqual("ready", cleaned.disposition)
                    self.assertEqual(["fixture:alpha"], provider.cleaned)
                ledger.close()

    def test_two_executions_get_distinct_worktrees(self):
        second_execution = self.kernel.begin(
            "dotfactory", "TASK-569", {"title": "second"}, command_id="begin-second",
        )
        self.kernel.transition(
            second_execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-2", command_id="claim-second",
        )
        first_request = replace(self.request, config={"resources": []})
        second_request = replace(
            runner_request(self.kernel, second_execution), config={"resources": []},
        )
        first = self.engine.prepare(
            first_request, project=self.project(),
            preparation_config=self.configuration(),
        ).launch
        second = self.engine.prepare(
            second_request, project=self.project(),
            preparation_config=self.configuration(),
        ).launch
        self.assertNotEqual(first.workspace_path, second.workspace_path)
        self.assertNotEqual(first.branch_name, second.branch_name)
        self.assertTrue(first.workspace_path.endswith("TASK-569-1"))
        self.assertTrue(second.workspace_path.endswith("TASK-569-2"))

    def test_two_fixture_services_get_distinct_portless_urls(self):
        fixture = self.root / "portless-fixture"
        fixture.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys, time\n"
            "if '--portless' in sys.argv or 'list' in sys.argv:\n"
            "    raise SystemExit(0)\n"
            "service = sys.argv[sys.argv.index('--name') + 1]\n"
            "workspace = pathlib.Path.cwd().name.lower()\n"
            "number = int(workspace.rsplit('-', 1)[1])\n"
            "print(f'PORT={4100 + number} '"
            "f'PORTLESS_URL=https://{workspace}.{service}.localhost', flush=True)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        fixture.chmod(0o755)
        portless = PortlessProvider(
            command=str(fixture), preflight_command=str(fixture),
            startup_timeout_seconds=2,
            route_inspector=lambda _url: None,
            process_identity=lambda pid: f"fixture-process-{pid}",
        )
        engine = PreparationEngine(
            self.ledger, workspace_provider=self.workspace,
            providers={"portless": portless}, owner_token="factory-owner",
        )
        second_execution = self.kernel.begin(
            "dotfactory", "TASK-569", {"title": "second service"},
            command_id="begin-portless-second",
        )
        self.kernel.transition(
            second_execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner-2", command_id="claim-portless-second",
        )
        config = {
            "workspace": {
                "root": str(self.root / "worktrees"), "remote": "origin",
                "base_ref": "main", "retention": "until_terminal",
            },
            "providers": {"portless": {"kind": "portless"}},
            "capabilities": {
                "local-web": {"provider": "portless", "scope": "attempt",
                              "mode": "exclusive", "config": {
                                  "service_name": "web",
                                  "command": ["fixture", "serve"],
                              }},
            },
        }
        first_request = replace(
            self.request, config={"resources": ["local-web"]},
        )
        second_request = replace(
            runner_request(self.kernel, second_execution),
            config={"resources": ["local-web"]},
        )
        first_result = engine.prepare(
            first_request, project=self.project(), preparation_config=config,
        )
        second_result = engine.prepare(
            second_request, project=self.project(), preparation_config=config,
        )
        self.assertEqual("ready", first_result.disposition, first_result.error)
        self.assertEqual("ready", second_result.disposition, second_result.error)
        first = first_result.launch
        second = second_result.launch
        try:
            self.assertNotEqual(first.urls, second.urls)
            self.assertTrue(first.urls[0].endswith(".localhost"))
            self.assertTrue(second.urls[0].endswith(".localhost"))
            self.assertNotEqual(first.allocation_ids, second.allocation_ids)
        finally:
            engine.cleanup_attempt(first)
            engine.cleanup_attempt(second)


class GitWorkspaceProviderTests(unittest.TestCase):
    def git(self, directory, *arguments):
        return subprocess.run(
            ["git", "-C", str(directory), *arguments], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()

    def test_branches_from_fetched_main_and_refuses_dirty_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin.git"
            seed = root / "seed"
            checkout = root / "checkout"
            subprocess.run(["git", "init", "--bare", str(origin)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "init", "-b", "main", str(seed)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.git(seed, "config", "user.email", "test@example.com")
            self.git(seed, "config", "user.name", "Test")
            (seed / "README.md").write_text("one\n")
            self.git(seed, "add", "README.md")
            self.git(seed, "commit", "-m", "initial")
            self.git(seed, "remote", "add", "origin", str(origin))
            self.git(seed, "push", "-u", "origin", "main")
            subprocess.run(["git", "clone", "-b", "main", str(origin), str(checkout)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            main_before = self.git(checkout, "rev-parse", "HEAD")
            provider = GitWorkspaceProvider()
            handle = provider.materialize(
                repository_path=str(checkout), root=str(root / "worktrees"),
                remote="origin", base_ref="main", issue_identifier="TASK-569",
                execution_number=1,
            )
            self.assertEqual(main_before, handle.base_sha)
            self.assertEqual(main_before, self.git(checkout, "rev-parse", "HEAD"))
            self.assertIn("TASK-569-1", handle.path)
            dirty = Path(handle.path) / "untracked.txt"
            dirty.write_text("keep me")
            with self.assertRaises(WorkspaceUnsafeCleanup):
                provider.cleanup(handle)
            self.assertTrue(dirty.exists())
            dirty.unlink()
            provider.cleanup(handle)
            self.assertFalse(Path(handle.path).exists())

    def test_repository_local_worktree_root_must_be_gitignored(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            subprocess.run(
                ["git", "init", "-b", "main", str(repository)], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.git(repository, "config", "user.email", "test@example.com")
            self.git(repository, "config", "user.name", "Test")
            (repository / "README.md").write_text("one\n")
            self.git(repository, "add", "README.md")
            self.git(repository, "commit", "-m", "initial")
            self.git(repository, "remote", "add", "origin", str(repository))
            provider = GitWorkspaceProvider()
            with self.assertRaisesRegex(WorkspaceConflict, "not ignored"):
                provider.materialize(
                    repository_path=str(repository),
                    root=str(repository / ".worktrees"), remote="origin",
                    base_ref="main", issue_identifier="TASK-569", execution_number=1,
                )
            (repository / ".gitignore").write_text("/.worktrees/\n")
            self.git(repository, "add", ".gitignore")
            self.git(repository, "commit", "-m", "ignore worktrees")
            handle = provider.materialize(
                repository_path=str(repository),
                root=str(repository / ".worktrees"), remote="origin",
                base_ref="main", issue_identifier="TASK-569", execution_number=1,
            )
            self.assertEqual(
                (repository / ".worktrees" / "TASK-569-1").resolve(),
                Path(handle.path),
            )


class PortlessProviderTests(unittest.TestCase):
    def setUp(self):
        self.workspace = WorkspaceHandle(
            repository_path="/repository", git_common_dir="/repository/.git",
            remote="origin", base_ref="main", base_sha="a" * 40,
            branch_name="factory/imp-569-1", path="/worktrees/TASK-569-1",
        )
        self.provider = PortlessProvider()

    def test_plan_uses_owned_worktree_route_identity(self):
        plan = self.provider.plan(
            capability="local-web",
            config={"service_name": "web", "command": ["npm", "run", "dev"]},
            workspace=self.workspace,
        )
        self.assertEqual("portless:factory/imp-569-1:web", plan.resource_id)
        self.assertNotIn("--force", plan.config["command"])

    def test_plan_rejects_takeover_and_exposure_flags(self):
        for flag in ("--force", "--lan", "--tunnel", "--wildcard"):
            with self.subTest(flag=flag), self.assertRaises(PreparationNeedsAttention):
                self.provider.plan(
                    capability="local-web",
                    config={"service_name": "web", "command": ["npm", "run", flag]},
                    workspace=self.workspace,
                )

    def test_reconcile_classifies_process_and_route_orphans(self):
        url = "https://factory-imp-569-1.web.localhost"
        allocation = {
            "resource_id": "portless:factory/imp-569-1:web",
            "capability": "local-web",
            "metadata": {
                "pid": 123, "process_identity": "started-once",
                "route_url": url, "app_port": 4123, "service_name": "web",
            },
        }
        cases = (
            ("process-without-route", {123: "started-once"}, {}),
            ("route-without-process", {}, {url: {"url": url, "pid": 123}}),
            ("pid-identity-mismatch", {123: "started-twice"}, {
                url: {"url": url, "pid": 123},
            }),
            ("route-owner-mismatch", {123: "started-once"}, {
                url: {"url": url, "pid": 999},
            }),
        )
        for expected, identities, routes in cases:
            with self.subTest(expected=expected):
                provider = PortlessProvider(
                    process_identity=lambda pid, values=identities: values.get(pid),
                    route_inspector=lambda target, values=routes: values.get(target),
                )
                with self.assertRaises(PreparationNeedsAttention) as raised:
                    provider.reconcile(
                        allocation, workspace=self.workspace, owner_token="factory-owner",
                    )
                self.assertEqual(expected, raised.exception.detail["orphan_state"])

    def test_reconciled_owned_process_can_be_cleaned_without_route_takeover(self):
        url = "https://factory-imp-569-1.web.localhost"
        identities = {123: "started-once"}
        routes = {url: {"url": url, "pid": 123}}

        def stop(pid, _signal):
            identities.pop(pid, None)
            routes.pop(url, None)

        provider = PortlessProvider(
            process_identity=lambda pid: identities.get(pid),
            route_inspector=lambda target: routes.get(target),
            kill_process_group=stop,
        )
        allocation = {
            "resource_id": "portless:factory/imp-569-1:web",
            "capability": "local-web",
            "metadata": {
                "pid": 123, "process_identity": "started-once",
                "route_url": url, "app_port": 4123, "service_name": "web",
            },
        }
        activation = provider.reconcile(
            allocation, workspace=self.workspace, owner_token="factory-owner",
        )
        result = provider.cleanup(activation, owner_token="factory-owner")
        self.assertEqual(123, result["pid"])
        self.assertEqual({}, routes)


class SchemaSevenMigrationTests(unittest.TestCase):
    def test_schema_six_adds_preparation_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema-six.db"
            database = __import__("sqlite3").connect(path)
            database.execute("PRAGMA user_version=6")
            database.close()
            ledger = SQLiteLedger(path)
            tables = {
                row[0] for row in ledger.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue({
                "execution_workspaces", "preparations", "resource_allocations",
                "resource_mutations", "attention_requests", "cleanup_plans",
            }.issubset(tables))
            self.assertEqual(8, ledger.connection.execute("PRAGMA user_version").fetchone()[0])
            ledger.close()


if __name__ == "__main__":
    unittest.main()
