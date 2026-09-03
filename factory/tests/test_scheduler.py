import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotfactory import (
    ControlService, DurableKernel, FactoryConfig, FakePreparedRunner,
    ObservationService, PreparationEngine, PreparationResult, PreparedLaunch, Principal,
    RunnerResult, ScheduledProject, Scheduler, SchedulerPolicy, SQLiteLedger,
)
from dotfactory.ledger import LedgerError, StaleAttempt
from dotfactory.observability import stable_span_id
from dotfactory.resources import PreparationError
from dotfactory.runner import runner_request


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ({"kind": "proof", "uri": "local://scheduler-proof"},)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 29, 20, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value.isoformat()

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakePreparation:
    def __init__(self, ledger, dispositions=None):
        self.ledger = ledger
        self.dispositions = list(dispositions or ["ready"])
        self.requests = []
        self.cleaned = []

    def prepare(self, request):
        self.requests.append(request)
        disposition = self.dispositions.pop(0) if self.dispositions else "ready"
        if disposition == "busy":
            return PreparationResult(
                "busy", retry_after_seconds=5,
                error={"message": "fixture capacity is busy"},
            )
        preparation = self.ledger.begin_preparation(
            attempt_id=request.attempt_id, fence_token=request.fence_token,
            request_digest=f"fixture:{request.attempt_id}",
        )
        if disposition == "attention":
            attention = self.ledger.open_attention(
                execution_id=request.execution_id, attempt_id=request.attempt_id,
                preparation_id=preparation["id"], dedupe_key=f"fixture:{request.attempt_id}",
                category="unhealthy", provider="fixture",
                detail={"allowed_actions": ["retry", "cancel"],
                        "last_safe_step": "fixture"},
            )
            self.ledger.fail_preparation(
                preparation["id"], fence_token=request.fence_token,
                status="needs_attention", error={"message": "fixture attention"},
            )
            return PreparationResult("needs_attention", attention=attention)
        if disposition == "fatal":
            self.ledger.fail_preparation(
                preparation["id"], fence_token=request.fence_token,
                status="failed", error={"message": "fixture fatal"},
            )
            return PreparationResult("fatal", error={"message": "fixture fatal"})
        if preparation["status"] in ("failed", "busy"):
            preparation = self.ledger.resume_preparation(
                preparation["id"], fence_token=request.fence_token,
            )
        if preparation["status"] != "ready":
            preparation = self.ledger.mark_preparation_ready(
                preparation["id"], fence_token=request.fence_token,
                result_digest=f"digest:{request.attempt_id}",
                prepared={"allocation_ids": [], "environment_names": [], "urls": []},
            )
        launch = PreparedLaunch(
            request=request, preparation_id=preparation["id"],
            preparation_digest=preparation["result_digest"],
            workspace_path=f"/worktrees/{request.execution_id}",
            branch_name=f"factory/{request.execution_id}",
            environment=(), commands=(), urls=(), allocation_ids=(),
        )
        return PreparationResult("ready", launch=launch)

    def cleanup_attempt(self, launch):
        self.cleaned.append(launch.request.attempt_id)
        return PreparationResult("ready")


class SimulatedCrash(BaseException):
    pass


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.db_path = self.root / "factory.db"
        self.ledger = SQLiteLedger(self.db_path, clock=self.clock)
        self.ledger.configure_factory("scheduler-test")
        self.ledger.register_project(
            "alpha", display_name="Alpha", tracker_kind="linear",
            tracker_project_id="linear-alpha",
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def work(self, identifier="TASK-572", project="alpha", runner="codex"):
        kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "default.dot",
            factory_defaults={"runner": runner, "resources": []},
        )
        execution = kernel.begin(
            project, identifier, {"title": identifier},
            command_id=f"begin-{identifier}",
        )
        claim = kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner=f"owner-{identifier}", command_id=f"enter-{identifier}",
        )
        self.clock.advance(1)
        return kernel, execution, claim

    def scheduler(
        self, kernel, preparation, runner=None, fault_hook=None, observer=None,
        project="alpha",
    ):
        return Scheduler(
            self.ledger,
            projects={project: ScheduledProject(kernel, preparation)},
            runner=runner or FakePreparedRunner([
                RunnerResult("succeeded", "complete", EVIDENCE)
            ]),
            owner="scheduler-a",
            policy=SchedulerPolicy(
                claim_ttl_seconds=120, host_limit=2,
                project_limits={project: 2}, runner_limits={"codex": 2},
            ),
            fault_hook=fault_hook, observer=observer,
        )

    def result_ready_attention(
        self, identifier="TASK-RESULT-RETRY", project="alpha",
    ):
        kernel, execution, claim = self.work(identifier, project=project)
        investigating = kernel.complete_attempt(
            execution, preferred_label="failed", outcome="failed",
            evidence=[{"kind": "runner_error", "uri": "local://first-run"}],
            attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
            owner=f"owner-{identifier}", command_id=f"fail-{identifier}",
        )
        preparation = FakePreparation(self.ledger)
        runner = FakePreparedRunner([
            RunnerResult("recovered", "retry", EVIDENCE)
        ])
        scheduler = self.scheduler(kernel, preparation, runner, project=project)
        complete_attempt = kernel.complete_attempt

        def broken_commit(*_args, **_kwargs):
            raise RuntimeError("result commit fix is not active")

        kernel.complete_attempt = broken_commit
        try:
            tick = scheduler.tick()
        finally:
            kernel.complete_attempt = complete_attempt
        self.assertEqual("needs_attention", tick.disposition)
        self.assertEqual("result-commit", tick.detail["category"])
        return kernel, execution, investigating, preparation, runner, scheduler, tick

    def test_tick_prepares_dispatches_cleans_and_commits(self):
        kernel, execution, _claim = self.work()
        preparation = FakePreparation(self.ledger)
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])
        tick = self.scheduler(kernel, preparation, runner).tick()
        self.assertEqual("completed", tick.disposition)
        self.assertEqual("Ready", self.ledger.current(execution)["current_state_id"])
        self.assertEqual(1, len(runner.launches))
        self.assertEqual(1, len(preparation.cleaned))
        self.assertEqual("completed", self.ledger.dispatch(tick.dispatch_id)["status"])

    def test_raw_runner_request_is_rejected(self):
        kernel, execution, _claim = self.work()
        scheduler = self.scheduler(kernel, FakePreparation(self.ledger))
        with self.assertRaisesRegex(PreparationError, "PreparedLaunch"):
            scheduler.dispatch_prepared(runner_request(kernel, execution))

    def test_busy_preparation_waits_without_graph_retry(self):
        kernel, execution, _claim = self.work()
        preparation = FakePreparation(self.ledger, ["busy", "ready"])
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])
        scheduler = self.scheduler(kernel, preparation, runner)
        first = scheduler.tick()
        self.assertEqual("busy", first.disposition)
        self.assertEqual("Autoplanning", self.ledger.current(execution)["current_state_id"])
        self.assertEqual(1, sum(
            item["state_id"] == "Autoplanning"
            for item in self.ledger.run_history(execution)["state_runs"]
        ))
        self.assertEqual(0, len(runner.launches))
        self.assertEqual("waiting", self.ledger.dispatch(first.dispatch_id)["status"])
        self.clock.advance(6)
        second = scheduler.tick()
        self.assertEqual("completed", second.disposition)
        self.assertEqual(1, len(runner.launches))

    def test_preparation_attention_and_fatal_never_launch(self):
        for index, disposition in enumerate(("attention", "fatal"), start=1):
            if index > 1:
                self.ledger.register_project(
                    f"project-{index}", display_name=f"Project {index}",
                    tracker_kind="linear", tracker_project_id=f"linear-{index}",
                )
                project = f"project-{index}"
            else:
                project = "alpha"
            kernel, execution, _claim = self.work(
                f"TASK-57{index + 1}", project=project,
            )
            preparation = FakePreparation(self.ledger, [disposition])
            runner = FakePreparedRunner([])
            scheduler = Scheduler(
                self.ledger,
                projects={project: ScheduledProject(kernel, preparation)}, runner=runner,
                owner=f"scheduler-{index}", policy=SchedulerPolicy(host_limit=2),
            )
            tick = scheduler.tick()
            self.assertEqual("needs_attention", tick.disposition)
            self.assertEqual(0, len(runner.launches))
            self.assertEqual("Autoplanning", self.ledger.current(execution)["current_state_id"])
            attention = self.ledger.attention(tick.detail["attention_id"])
            self.assertEqual("open", attention["status"])

    def test_durable_terminal_runner_failure_follows_failed_graph_edge(self):
        kernel, execution, _claim = self.work("TASK-RUNNER-FAIL")

        class DurablyFailingRunner:
            def __init__(self, ledger):
                self.ledger = ledger

            def run(self, launch):
                request = launch.request
                run = self.ledger.plan_runner_run(
                    execution_id=request.execution_id,
                    attempt_id=request.attempt_id,
                    preparation_id=launch.preparation_id,
                    preparation_digest=launch.preparation_digest,
                    fence_token=request.fence_token, runner_key="codex",
                    adapter_kind="fixture", adapter_version="1.0.0",
                    protocol_version=1, execution_trace_id="1" * 32,
                    trace_id="2" * 32, root_span_id="3" * 16,
                    parent_trace_id=None, command=["fixture"],
                    command_digest="command", prompt_digest="prompt",
                    host_id="host", boot_id="boot",
                )
                self.ledger.mark_runner_starting(
                    str(run["id"]), fence_token=request.fence_token
                )
                self.ledger.finish_runner_run(
                    str(run["id"]), fence_token=request.fence_token,
                    status="failed", error={
                        "code": "INVALID_RESULT_PROOF", "category": "protocol",
                        "message": "runner evidence was invalid", "retryable": False,
                        "safe_remedy": "Inspect the runner receipt.",
                    },
                )
                raise RuntimeError("runner evidence was invalid")

        tick = self.scheduler(
            kernel, FakePreparation(self.ledger), DurablyFailingRunner(self.ledger)
        ).tick()
        self.assertEqual("completed", tick.disposition)
        self.assertEqual("Investigating", self.ledger.current(execution)["current_state_id"])
        self.assertEqual("completed", self.ledger.dispatch(tick.dispatch_id)["status"])
        self.assertEqual(
            "runner_error", self.ledger.run_history(execution)["artifacts"][-1]["kind"]
        )

    def test_terminal_cancel_closes_ambiguous_live_records_and_survives_restart(self):
        kernel, execution, _claim = self.work("TASK-CANCEL-LIVE")
        _other_kernel, other_execution, other_claim = self.work("TASK-OTHER-LIVE")
        other_preparation = self.ledger.begin_preparation(
            attempt_id=other_claim["attempt_id"],
            fence_token=other_claim["fence_token"],
            request_digest="unrelated-preparation",
        )
        unrelated_allocation = self.ledger.acquire_allocation(
            other_preparation["id"], fence_token=other_claim["fence_token"],
            scope="attempt", provider="fixture", capability="server",
            resource_id="unrelated-allocation",
        )
        unrelated_attention = self.ledger.open_attention(
            execution_id=other_execution, attempt_id=other_claim["attempt_id"],
            preparation_id=None, dedupe_key="unrelated-attention",
            category="unrelated", provider="fixture",
            detail={"allowed_actions": ["retry"]},
        )
        unrelated_lease = self.ledger.acquire_resource(
            "unrelated-resource", attempt_id=other_claim["attempt_id"],
            fence_token=other_claim["fence_token"],
            expires_at=(self.clock.value + timedelta(minutes=5)).isoformat(),
            idempotency_key="unrelated-resource-lease",
        )

        class AmbiguousLiveRunner:
            def __init__(self, ledger):
                self.ledger = ledger
                self.runner_run_id = None

            def run(self, launch):
                request = launch.request
                run = self.ledger.plan_runner_run(
                    execution_id=request.execution_id,
                    attempt_id=request.attempt_id,
                    preparation_id=launch.preparation_id,
                    preparation_digest=launch.preparation_digest,
                    fence_token=request.fence_token, runner_key="codex",
                    adapter_kind="fixture", adapter_version="1.0.0",
                    protocol_version=1, execution_trace_id="1" * 32,
                    trace_id="2" * 32, root_span_id="3" * 16,
                    parent_trace_id=None, command=["fixture"],
                    command_digest="command", prompt_digest="prompt",
                    host_id="host", boot_id="boot",
                )
                self.runner_run_id = str(run["id"])
                self.ledger.mark_runner_starting(
                    self.runner_run_id, fence_token=request.fence_token
                )
                self.ledger.mark_runner_running(
                    self.runner_run_id, fence_token=request.fence_token,
                    pid=1234, process_group_id=1234,
                )
                raise RuntimeError("runner process ended before result capture")

        runner = AmbiguousLiveRunner(self.ledger)
        scheduler = self.scheduler(kernel, FakePreparation(self.ledger), runner)
        blocked = scheduler.tick()
        attention_id = blocked.detail["attention_id"]
        self.assertEqual("needs_attention", blocked.disposition)
        self.assertEqual("ambiguous-dispatch", blocked.detail["category"])
        self.assertEqual(
            "running", self.ledger.runner_run(runner.runner_run_id)["status"]
        )
        self.assertEqual(
            "attention", self.ledger.dispatch(blocked.dispatch_id)["status"]
        )
        live_waterfall = ObservationService(
            self.ledger, kernel
        ).execution_projection(execution)["waterfall"]
        live_spans = {
            item["span_id"]: item for item in live_waterfall["items"]
            if item["kind"] == "span"
        }
        self.assertIsNone(live_spans[
            self.ledger.runner_run(runner.runner_run_id)["root_span_id"]
        ]["ended_at"])
        self.assertIsNone(live_spans[
            stable_span_id("scheduler_dispatch", blocked.dispatch_id)
        ]["ended_at"])

        control = ControlService(self.ledger, kernel)
        request = {
            "action": "cancel", "expected_state": "Autoplanning",
            "confirmed": True,
            "parameters": {"reason": "Operator stopped the ambiguous live runner."},
        }
        first = control.execute(
            execution, command_id="cancel-ambiguous-live",
            principal=Principal("operator", "operator", "test"), request=request,
        )
        repeated = control.execute(
            execution, command_id="cancel-ambiguous-live",
            principal=Principal("operator", "operator", "test"), request=request,
        )
        self.assertEqual(first, repeated)

        runner_run = self.ledger.runner_run(runner.runner_run_id)
        self.assertEqual("canceled", runner_run["status"])
        self.assertIsNotNone(runner_run["completed_at"])
        self.assertIsNone(runner_run["result"])
        self.assertIsNone(runner_run["receipt"])
        self.assertTrue(runner_run["error"]["ambiguous_side_effect"])
        dispatch = self.ledger.dispatch(blocked.dispatch_id)
        self.assertEqual("superseded", dispatch["status"])
        self.assertEqual("ambiguous-dispatch", dispatch["error"]["category"])
        self.assertIsNotNone(dispatch["completed_at"])
        attention = self.ledger.attention(attention_id)
        self.assertEqual("canceled", attention["status"])
        self.assertIsNotNone(attention["resolved_at"])

        summary = ObservationService(
            self.ledger, kernel
        ).execution_projection(execution)
        self.assertEqual(0, summary["waterfall"]["open_span_count"])
        completion_facts = {
            (item["entity_kind"], item["entity_id"]): item
            for item in self.ledger.trace_completion_facts(execution)
        }
        self.assertEqual(
            runner_run["root_span_id"],
            completion_facts[("runner_run", runner.runner_run_id)]["span_id"],
        )
        self.assertEqual(
            stable_span_id("scheduler_dispatch", blocked.dispatch_id),
            completion_facts[("scheduler_dispatch", blocked.dispatch_id)]["span_id"],
        )
        summary = summary["summary"]
        self.assertEqual("TASK-CANCEL-LIVE: Canceled", summary["headline"])
        self.assertIn(
            "DOTFACTORY_SCHEDULER_DISPATCH_ATTENTION",
            {item["code"] for item in summary["errors"]},
        )
        self.assertEqual(
            "open", self.ledger.attention(unrelated_attention["id"])["status"]
        )
        self.assertEqual("active", self.ledger.connection.execute(
            "SELECT status FROM resource_leases WHERE id=?", (unrelated_lease,),
        ).fetchone()[0])
        self.assertEqual("active", self.ledger.connection.execute(
            "SELECT status FROM resource_allocations WHERE id=?",
            (unrelated_allocation["id"],),
        ).fetchone()[0])

        self.ledger.close()
        self.ledger = SQLiteLedger(self.db_path, clock=self.clock)
        restarted_kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "default.dot",
            factory_defaults={"runner": "codex", "resources": []},
        )
        after_restart = ControlService(self.ledger, restarted_kernel).execute(
            execution, command_id="cancel-ambiguous-live",
            principal=Principal("operator", "operator", "test"), request=request,
        )
        self.assertEqual(first, after_restart)
        restarted = self.scheduler(
            restarted_kernel, FakePreparation(self.ledger), FakePreparedRunner([])
        ).tick()
        self.assertEqual("idle", restarted.disposition)
        self.assertEqual(1, self.ledger.connection.execute(
            "SELECT COUNT(*) FROM events WHERE execution_id=? "
            "AND event_type='runner_canceled'", (execution,),
        ).fetchone()[0])
        self.assertEqual(1, self.ledger.connection.execute(
            "SELECT COUNT(*) FROM events WHERE execution_id=? "
            "AND event_type='scheduler_dispatch_superseded'", (execution,),
        ).fetchone()[0])
        self.assertEqual(1, self.ledger.connection.execute(
            "SELECT COUNT(*) FROM events WHERE execution_id=? "
            "AND event_type='attention_resolved'", (execution,),
        ).fetchone()[0])

    def test_investigation_retry_commits_the_conditional_resume_edge(self):
        kernel, execution, _claim = self.work("TASK-RECOVERY-RETRY")
        runner = FakePreparedRunner([
            RunnerResult("failed", "failed", EVIDENCE),
            RunnerResult("recovered", "retry", EVIDENCE),
        ])
        scheduler = self.scheduler(kernel, FakePreparation(self.ledger), runner)
        failed = scheduler.tick()
        self.assertEqual("completed", failed.disposition)
        self.assertEqual(
            "Investigating", self.ledger.current(execution)["current_state_id"]
        )
        retried = scheduler.tick()
        self.assertEqual("completed", retried.disposition)
        self.assertEqual(
            "Autoplanning", self.ledger.current(execution)["current_state_id"]
        )
        self.assertEqual(
            "investigating.autoplanning",
            [
                item["payload"]["edge_id"]
                for item in self.ledger.run_history(execution)["events"]
                if item["event_type"] == "transition_accepted"
            ][-1],
        )

    def test_result_ready_attention_replays_without_duplicate_runner(self):
        (
            _kernel, execution, attempt, preparation, runner, scheduler, tick,
        ) = self.result_ready_attention()
        dispatch_before = self.ledger.dispatch(tick.dispatch_id)
        resolved = scheduler.remedy_attention(
            execution, attention_id=tick.detail["attention_id"], remedy="retry",
            command_id="operator:result-ready:retry",
            expected_attempt_id=attempt["attempt_id"],
        )
        replay = scheduler.remedy_attention(
            execution, attention_id=tick.detail["attention_id"], remedy="retry",
            command_id="operator:result-ready:retry",
            expected_attempt_id=attempt["attempt_id"],
        )
        self.assertEqual("resolved", resolved["attention"]["status"])
        self.assertEqual(resolved["attention"], replay["attention"])
        with self.assertRaises(StaleAttempt):
            scheduler.remedy_attention(
                execution, attention_id=tick.detail["attention_id"], remedy="retry",
                command_id="operator:result-ready:retry",
                expected_attempt_id="stale-attempt",
            )
        self.assertEqual("attention", self.ledger.dispatch(tick.dispatch_id)["status"])
        self.assertEqual(1, self.ledger.connection.execute(
            "SELECT COUNT(*) FROM events WHERE execution_id=? "
            "AND event_type='attention_resolved' AND attempt_id=?",
            (execution, attempt["attempt_id"]),
        ).fetchone()[0])
        resumed = scheduler.tick()
        self.assertEqual("resumed", resumed.disposition)
        self.assertEqual("result_ready", resumed.detail["phase"])
        self.assertEqual(
            dispatch_before["result"], self.ledger.dispatch(tick.dispatch_id)["result"]
        )
        recovered = scheduler.tick()
        self.assertEqual("recovered", recovered.disposition)
        self.assertEqual("Autoplanning", self.ledger.current(execution)["current_state_id"])
        self.assertEqual("completed", self.ledger.dispatch(tick.dispatch_id)["status"])
        self.assertEqual(1, len(runner.launches))
        self.assertEqual(2, len(preparation.requests))

    def test_failed_result_replay_opens_fresh_attention_until_fixed(self):
        (
            kernel, execution, attempt, preparation, runner, scheduler, first,
        ) = self.result_ready_attention("TASK-REPLAY-FAILS-AGAIN")
        scheduler.remedy_attention(
            execution, attention_id=first.detail["attention_id"], remedy="retry",
            command_id="operator:first-retry",
            expected_attempt_id=attempt["attempt_id"],
        )
        self.assertEqual("resumed", scheduler.tick().disposition)
        original_dispatch = self.ledger.dispatch(first.dispatch_id)
        complete_attempt = kernel.complete_attempt

        def still_broken(*_args, **_kwargs):
            raise RuntimeError("result commit is still unavailable")

        kernel.complete_attempt = still_broken
        try:
            repeated = scheduler.tick()
        finally:
            kernel.complete_attempt = complete_attempt
        self.assertEqual("needs_attention", repeated.disposition)
        self.assertEqual("result-commit", repeated.detail["category"])
        self.assertNotEqual(
            first.detail["attention_id"], repeated.detail["attention_id"]
        )
        second = self.ledger.attention(repeated.detail["attention_id"])
        self.assertEqual("open", second["status"])
        self.assertEqual(attempt["attempt_id"], second["attempt_id"])
        self.assertEqual(
            original_dispatch["preparation_id"], second["preparation_id"]
        )
        self.assertEqual(["retry"], second["detail"]["allowed_actions"])
        dispatch = self.ledger.dispatch(repeated.dispatch_id)
        self.assertEqual("attention", dispatch["status"])
        self.assertEqual(original_dispatch["result"], dispatch["result"])
        self.assertEqual(1, len(runner.launches))

        scheduler.remedy_attention(
            execution, attention_id=second["id"], remedy="retry",
            command_id="operator:second-retry",
            expected_attempt_id=attempt["attempt_id"],
        )
        self.assertEqual("resumed", scheduler.tick().disposition)
        self.assertEqual("recovered", scheduler.tick().disposition)
        self.assertEqual("Autoplanning", self.ledger.current(execution)["current_state_id"])
        self.assertEqual(1, len(runner.launches))

    def test_result_rehydration_error_has_a_readable_recovery_category(self):
        (
            _kernel, execution, attempt, preparation, runner, scheduler, first,
        ) = self.result_ready_attention("TASK-REHYDRATION-ERROR")
        scheduler.remedy_attention(
            execution, attention_id=first.detail["attention_id"], remedy="retry",
            command_id="operator:rehydration-retry",
            expected_attempt_id=attempt["attempt_id"],
        )
        self.assertEqual("resumed", scheduler.tick().disposition)
        prepare = preparation.prepare

        def broken_rehydration(_request):
            raise RuntimeError("prepared launch could not be reconstructed")

        preparation.prepare = broken_rehydration
        try:
            repeated = scheduler.tick()
        finally:
            preparation.prepare = prepare
        self.assertEqual("needs_attention", repeated.disposition)
        self.assertEqual("result-recovery", repeated.detail["category"])
        attention = self.ledger.attention(repeated.detail["attention_id"])
        self.assertEqual(["retry"], attention["detail"]["allowed_actions"])
        self.assertEqual(1, len(runner.launches))

    def test_scheduler_attention_rejects_stale_attempt_and_wrong_link(self):
        (
            _kernel, execution, attempt, _preparation, _runner, scheduler, tick,
        ) = self.result_ready_attention("TASK-RESULT-GUARDS")
        with self.assertRaises(StaleAttempt):
            scheduler.remedy_attention(
                execution, attention_id=tick.detail["attention_id"], remedy="retry",
                command_id="operator:stale", expected_attempt_id="stale-attempt",
            )
        self.assertEqual(
            "open", self.ledger.attention(tick.detail["attention_id"])["status"]
        )
        dispatch = self.ledger.dispatch(tick.dispatch_id)
        wrong = self.ledger.open_attention(
            execution_id=execution, attempt_id=attempt["attempt_id"],
            preparation_id=dispatch["preparation_id"],
            dedupe_key="scheduler:wrong-link", category="result-commit",
            provider="scheduler", detail={
                "dispatch_id": tick.dispatch_id, "last_safe_step": "result_ready",
                "allowed_actions": ["retry"],
            },
        )
        with self.assertRaisesRegex(LedgerError, "not linked"):
            scheduler.remedy_attention(
                execution, attention_id=wrong["id"], remedy="retry",
                command_id="operator:wrong-link",
                expected_attempt_id=attempt["attempt_id"],
            )
        foreign = self.ledger.open_attention(
            execution_id=execution, attempt_id=attempt["attempt_id"],
            preparation_id=dispatch["preparation_id"],
            dedupe_key="fixture:wrong-provider", category="result-commit",
            provider="fixture", detail={
                "dispatch_id": tick.dispatch_id, "last_safe_step": "result_ready",
                "allowed_actions": ["retry"],
            },
        )
        with self.assertRaisesRegex(LedgerError, "not scheduler-owned"):
            scheduler.remedy_attention(
                execution, attention_id=foreign["id"], remedy="retry",
                command_id="operator:wrong-provider",
                expected_attempt_id=attempt["attempt_id"],
            )

    def test_resolved_attention_for_inactive_project_is_not_resumed(self):
        self.ledger.register_project(
            "beta", display_name="Beta", tracker_kind="linear",
            tracker_project_id="linear-beta",
        )
        (
            _kernel, execution, attempt, _preparation, _runner,
            beta_scheduler, attention_tick,
        ) = self.result_ready_attention("TASK-BETA-REPLAY", project="beta")
        beta_scheduler.remedy_attention(
            execution, attention_id=attention_tick.detail["attention_id"],
            remedy="retry", command_id="operator:beta:retry",
            expected_attempt_id=attempt["attempt_id"],
        )
        before = self.ledger.dispatch(attention_tick.dispatch_id)
        alpha_kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "default.dot",
            factory_defaults={"runner": "codex", "resources": []},
        )
        tick = self.scheduler(
            alpha_kernel, FakePreparation(self.ledger),
            runner=FakePreparedRunner([]), project="alpha",
        ).tick()
        self.assertEqual("idle", tick.disposition)
        self.assertEqual(before, self.ledger.dispatch(attention_tick.dispatch_id))

    def test_inactive_project_attempt_is_not_claimed(self):
        self.ledger.register_project(
            "beta", display_name="Beta", tracker_kind="linear",
            tracker_project_id="linear-beta",
        )
        _beta_kernel, _execution, claim = self.work(
            "TASK-BETA-CLAIM", project="beta",
        )
        alpha_kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "default.dot",
            factory_defaults={"runner": "codex", "resources": []},
        )
        tick = self.scheduler(
            alpha_kernel, FakePreparation(self.ledger),
            runner=FakePreparedRunner([]), project="alpha",
        ).tick()
        self.assertEqual("idle", tick.disposition)
        self.assertIsNone(self.ledger.dispatch_for_attempt(claim["attempt_id"]))

    def test_stale_resolved_attention_is_superseded_without_wedging(self):
        (
            kernel, execution, attempt, _preparation, _runner,
            scheduler, attention_tick,
        ) = self.result_ready_attention("TASK-STALE-REPLAY")
        scheduler.remedy_attention(
            execution, attention_id=attention_tick.detail["attention_id"],
            remedy="retry", command_id="operator:stale:retry",
            expected_attempt_id=attempt["attempt_id"],
        )
        kernel.complete_attempt(
            execution, preferred_label="blocked", outcome="blocked elsewhere",
            evidence=[{"kind": "decision", "uri": "local://stale-replay"}],
            attempt_id=attempt["attempt_id"],
            fence_token=attempt["fence_token"], owner="owner-TASK-STALE-REPLAY",
            command_id="advance-before-replay",
        )
        tick = scheduler.tick()
        self.assertEqual("superseded", tick.disposition)
        self.assertEqual(
            "superseded", self.ledger.dispatch(attention_tick.dispatch_id)["status"]
        )
        self.assertEqual("Blocked", self.ledger.current(execution)["current_state_id"])

    def test_capacity_limits_are_atomic_and_data_driven(self):
        self.ledger.register_project(
            "beta", display_name="Beta", tracker_kind="linear",
            tracker_project_id="linear-beta",
        )
        _ka1, _ea1, a1 = self.work("TASK-A1", runner="codex")
        _ka2, _ea2, _a2 = self.work("TASK-A2", runner="codex")
        _kb1, _eb1, b1 = self.work("TASK-B1", project="beta", runner="claude")
        _kb2, _eb2, _b2 = self.work("TASK-B2", project="beta", runner="codex")
        limits = {
            "host": 2, "projects": {"alpha": 1, "beta": 2},
            "runners": {"codex": 1, "claude": 1},
        }
        first = self.ledger.claim_dispatch(
            scheduler_owner="one", claim_ttl_seconds=120, limits=limits,
        )
        second = self.ledger.claim_dispatch(
            scheduler_owner="two", claim_ttl_seconds=120, limits=limits,
        )
        third = self.ledger.claim_dispatch(
            scheduler_owner="three", claim_ttl_seconds=120, limits=limits,
        )
        self.assertEqual(a1["attempt_id"], first["dispatch"]["attempt_id"])
        self.assertEqual(b1["attempt_id"], second["dispatch"]["attempt_id"])
        self.assertEqual("capacity", third["disposition"])
        self.assertTrue(any(item["limit"] == "host" for item in third["blocked"]))

    def test_two_ledger_connections_claim_once(self):
        _kernel, _execution, claim = self.work()
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def compete(owner):
            ledger = SQLiteLedger(self.db_path, clock=self.clock)
            try:
                barrier.wait()
                results.append(ledger.claim_dispatch(
                    scheduler_owner=owner, claim_ttl_seconds=120,
                    limits={"host": 1, "projects": {}, "runners": {}},
                ))
            except Exception as error:
                failures.append(error)
            finally:
                ledger.close()

        threads = [threading.Thread(target=compete, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], failures)
        self.assertEqual(1, sum(item["disposition"] == "claimed" for item in results))
        stored = self.ledger.dispatch_for_attempt(claim["attempt_id"])
        self.assertEqual("claimed", stored["status"])

    def test_two_scheduler_processes_invoke_runner_once(self):
        _kernel, execution, _claim = self.work("TASK-RACE")
        barrier = threading.Barrier(2)
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])
        ticks = []
        failures = []

        def compete(owner):
            ledger = SQLiteLedger(self.db_path, clock=self.clock)
            kernel = DurableKernel(
                ledger, ROOT / "workflows" / "default.dot",
                factory_defaults={"runner": "codex", "resources": []},
            )
            try:
                barrier.wait()
                ticks.append(Scheduler(
                    ledger,
                    projects={"alpha": ScheduledProject(
                        kernel, FakePreparation(ledger),
                    )},
                    runner=runner, owner=owner,
                    policy=SchedulerPolicy(host_limit=1),
                ).tick())
            except Exception as error:
                failures.append(error)
            finally:
                ledger.close()

        threads = [
            threading.Thread(target=compete, args=(name,))
            for name in ("scheduler-a", "scheduler-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], failures)
        self.assertEqual(1, len(runner.launches))
        self.assertEqual(1, sum(tick.disposition == "completed" for tick in ticks))
        self.assertEqual("Ready", self.ledger.current(execution)["current_state_id"])

    def test_ambiguous_dispatch_opens_attention_without_rerun(self):
        kernel, _execution, _claim = self.work()
        preparation = FakePreparation(self.ledger)
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])

        def crash(boundary):
            if boundary == "after_dispatch_intent":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.scheduler(kernel, preparation, runner, fault_hook=crash).tick()
        self.assertEqual(0, len(runner.launches))
        recovered = self.scheduler(kernel, preparation, runner).tick()
        self.assertEqual("needs_attention", recovered.disposition)
        self.assertEqual("ambiguous-dispatch", recovered.detail["category"])
        attention = self.ledger.attention(recovered.detail["attention_id"])
        self.assertEqual([], attention["detail"]["allowed_actions"])
        self.assertEqual(0, len(runner.launches))

    def test_unrecorded_runner_result_is_ambiguous_and_not_rerun(self):
        kernel, _execution, _claim = self.work()
        preparation = FakePreparation(self.ledger)
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])

        def crash(boundary):
            if boundary == "after_runner_result":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.scheduler(kernel, preparation, runner, fault_hook=crash).tick()
        self.assertEqual(1, len(runner.launches))
        recovered = self.scheduler(kernel, preparation, runner).tick()
        self.assertEqual("needs_attention", recovered.disposition)
        self.assertEqual("ambiguous-dispatch", recovered.detail["category"])
        self.assertEqual(1, len(runner.launches))

    def test_durable_preparation_result_recovers_before_dispatch(self):
        kernel, execution, _claim = self.work()
        preparation = FakePreparation(self.ledger)
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])

        def crash(boundary):
            if boundary == "after_preparation_result":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.scheduler(kernel, preparation, runner, fault_hook=crash).tick()
        recovered = self.scheduler(kernel, preparation, runner).tick()
        self.assertEqual("recovered", recovered.disposition)
        self.assertEqual("Ready", self.ledger.current(execution)["current_state_id"])
        self.assertEqual(1, len(runner.launches))

    def test_preparing_crash_attention_can_be_retried_through_control(self):
        kernel, execution, claim = self.work()
        preparation = FakePreparation(self.ledger)
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])

        def crash(boundary):
            if boundary == "after_dispatch_preparing":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.scheduler(kernel, preparation, runner, fault_hook=crash).tick()
        self.assertEqual([], preparation.requests)
        recovered = self.scheduler(kernel, preparation, runner).tick()
        self.assertEqual("needs_attention", recovered.disposition)
        self.assertEqual("ambiguous-preparation", recovered.detail["category"])
        attention = self.ledger.attention(recovered.detail["attention_id"])
        self.assertEqual(["retry"], attention["detail"]["allowed_actions"])
        self.assertEqual(0, len(runner.launches))
        controller = PreparationEngine(
            self.ledger, workspace_provider=object(), providers={},
            owner_token="fixture-owner",
        )
        receipt = ControlService(
            self.ledger, kernel, controller,
        ).execute(
            execution, command_id="retry-ambiguous-preparation",
            principal=Principal("operator", "operator", "test"),
            request={
                "action": "attention", "expected_state": "Autoplanning",
                "parameters": {
                    "attention_id": recovered.detail["attention_id"],
                    "remedy": "retry", "expected_attempt_id": claim["attempt_id"],
                },
            },
        )
        self.assertEqual("completed", receipt["status"])
        self.assertEqual("resumed", self.scheduler(
            kernel, preparation, runner,
        ).tick().disposition)
        self.assertEqual("completed", self.scheduler(
            kernel, preparation, runner,
        ).tick().disposition)
        self.assertEqual(1, len(runner.launches))

    def test_other_owner_cannot_take_over_nonstealable_phase(self):
        kernel, _execution, _claim = self.work()
        claimed = self.ledger.claim_dispatch(
            scheduler_owner="scheduler-a", claim_ttl_seconds=120,
            limits={"host": 1, "projects": {}, "runners": {}},
        )["dispatch"]
        preparing = self.ledger.mark_dispatch_preparing(
            claimed["id"], claim_token=claimed["claim_token"],
        )
        other = Scheduler(
            self.ledger,
            projects={"alpha": ScheduledProject(
                kernel, FakePreparation(self.ledger),
            )},
            runner=FakePreparedRunner([]), owner="scheduler-b",
            policy=SchedulerPolicy(host_limit=1),
        )
        tick = other.tick()
        self.assertEqual("idle", tick.disposition)
        self.assertEqual("preparing", self.ledger.dispatch(preparing["id"])["status"])
        self.assertEqual([], self.ledger.run_snapshot(
            preparing["execution_id"]
        )["attention_requests"])

    def test_result_ready_replays_without_rerunning_runner(self):
        kernel, execution, _claim = self.work()
        preparation = FakePreparation(self.ledger)
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])

        def crash(boundary):
            if boundary == "after_result_recorded":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.scheduler(kernel, preparation, runner, fault_hook=crash).tick()
        self.assertEqual(1, len(runner.launches))
        recovered = self.scheduler(kernel, preparation, runner).tick()
        self.assertEqual("recovered", recovered.disposition)
        self.assertEqual("Ready", self.ledger.current(execution)["current_state_id"])
        self.assertEqual(1, len(runner.launches))

    def test_committed_transition_recovers_dispatch_receipt(self):
        kernel, execution, _claim = self.work()
        preparation = FakePreparation(self.ledger)
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])

        def crash(boundary):
            if boundary == "after_workflow_commit":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.scheduler(kernel, preparation, runner, fault_hook=crash).tick()
        self.assertEqual("Ready", self.ledger.current(execution)["current_state_id"])
        recovered = self.scheduler(kernel, preparation, runner).tick()
        self.assertEqual("recovered", recovered.disposition)
        self.assertEqual(1, len(runner.launches))

    def test_recovery_error_after_commit_does_not_open_stale_attention(self):
        kernel, execution, _claim = self.work("TASK-POST-COMMIT-RECOVERY")
        preparation = FakePreparation(self.ledger)
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])

        def stop_after_result(boundary):
            if boundary == "after_result_recorded":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.scheduler(
                kernel, preparation, runner, fault_hook=stop_after_result,
            ).tick()

        def fail_after_commit(boundary):
            if boundary == "after_workflow_commit":
                raise RuntimeError("receipt write was interrupted")

        recovered = self.scheduler(
            kernel, preparation, runner, fault_hook=fail_after_commit,
        ).tick()
        self.assertEqual("recovered", recovered.disposition)
        self.assertEqual("workflow_committed", recovered.detail["phase"])
        self.assertEqual("Ready", self.ledger.current(execution)["current_state_id"])
        self.assertEqual(
            "completed", self.ledger.dispatch(recovered.dispatch_id)["status"]
        )
        self.assertEqual([], self.ledger.run_snapshot(execution)["attention_requests"])
        self.assertEqual(1, len(runner.launches))

    def test_cleaned_result_recovers_without_rerunning_runner(self):
        kernel, execution, _claim = self.work()
        preparation = FakePreparation(self.ledger)
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])

        def crash(boundary):
            if boundary == "after_attempt_cleanup":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.scheduler(kernel, preparation, runner, fault_hook=crash).tick()
        recovered = self.scheduler(kernel, preparation, runner).tick()
        self.assertEqual("recovered", recovered.disposition)
        self.assertEqual("Ready", self.ledger.current(execution)["current_state_id"])
        self.assertEqual(1, len(runner.launches))

    def test_expired_claimed_and_prepared_phases_are_safe_to_reclaim(self):
        for boundary in ("after_dispatch_claimed", "after_dispatch_prepared"):
            identifier = "TASK-" + boundary.rsplit("_", 1)[-1].upper()
            kernel, execution, _claim = self.work(identifier)
            preparation = FakePreparation(self.ledger)
            runner = FakePreparedRunner([
                RunnerResult("succeeded", "complete", EVIDENCE)
            ])

            def crash(current):
                if current == boundary:
                    raise SimulatedCrash()

            with self.assertRaises(SimulatedCrash):
                self.scheduler(kernel, preparation, runner, fault_hook=crash).tick()
            self.clock.advance(121)
            recovered = self.scheduler(kernel, preparation, runner).tick()
            self.assertEqual("completed", recovered.disposition)
            self.assertEqual("Ready", self.ledger.current(execution)["current_state_id"])
            self.assertEqual(1, len(runner.launches))

    def test_stale_scheduler_and_attempt_fences_fail(self):
        kernel, execution, claim = self.work()
        acquired = self.ledger.claim_dispatch(
            scheduler_owner="scheduler", claim_ttl_seconds=120,
            limits={"host": 1, "projects": {}, "runners": {}},
        )["dispatch"]
        with self.assertRaises(StaleAttempt):
            self.ledger.mark_dispatch_preparing(
                acquired["id"], claim_token="stale-token"
            )
        self.ledger.mark_dispatch_preparing(
            acquired["id"], claim_token=acquired["claim_token"]
        )
        kernel.complete_attempt(
            execution, preferred_label="complete", outcome="external",
            evidence=list(EVIDENCE), attempt_id=claim["attempt_id"],
            fence_token=claim["fence_token"], owner="owner-TASK-572",
            command_id="external-complete",
        )
        with self.assertRaises(StaleAttempt):
            self.ledger.heartbeat_dispatch(
                acquired["id"], claim_token=acquired["claim_token"],
                claim_ttl_seconds=120, command_id="stale-heartbeat",
            )

    def test_observer_failure_does_not_rollback_completion(self):
        kernel, execution, _claim = self.work()

        def observer(_tick):
            raise RuntimeError("observer unavailable")

        tick = self.scheduler(
            kernel, FakePreparation(self.ledger), observer=observer,
        ).tick()
        self.assertEqual("completed", tick.disposition)
        self.assertEqual("Ready", self.ledger.current(execution)["current_state_id"])

    def test_scheduler_uses_stored_workflow_after_source_changes(self):
        workflow_path = self.root / "workflow.dot"
        workflow_path.write_text((ROOT / "workflows" / "default.dot").read_text())
        shutil.copytree(ROOT / "workflows" / "prompts", self.root / "prompts")
        kernel = DurableKernel(
            self.ledger, workflow_path,
            factory_defaults={"runner": "codex", "resources": []},
        )
        execution = kernel.begin(
            "alpha", "TASK-SNAPSHOT", {"title": "snapshot"},
            command_id="begin-snapshot",
        )
        kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="owner-snapshot", command_id="enter-snapshot",
        )
        expected_digest = self.ledger.workflow_snapshot(execution)["digest"]
        workflow_path.write_text("digraph broken {\n")
        runner = FakePreparedRunner([
            RunnerResult("succeeded", "complete", EVIDENCE)
        ])
        tick = self.scheduler(
            kernel, FakePreparation(self.ledger), runner,
        ).tick()
        self.assertEqual("completed", tick.disposition)
        self.assertEqual(expected_digest, runner.launches[0].request.workflow_digest)
        self.assertEqual("Ready", self.ledger.current(execution)["current_state_id"])

    def test_stored_scheduler_result_is_redacted(self):
        kernel, _execution, _claim = self.work()
        runner = FakePreparedRunner([RunnerResult(
            "succeeded", "complete",
            ({"kind": "proof", "uri": "local://proof", "token": "private"},),
        )])

        def crash(boundary):
            if boundary == "after_result_recorded":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.scheduler(
                kernel, FakePreparation(self.ledger), runner, fault_hook=crash,
            ).tick()
        dispatch = self.ledger.dispatch_for_attempt(
            self.ledger.current(_execution)["attempt"]["id"]
        )
        self.assertEqual("[REDACTED]", dispatch["result"]["evidence"][0]["token"])


class SchedulerConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.values = json.loads((ROOT / "factory.example.json").read_text())
        self.values["ledger_path"] = str(self.root / "factory.db")
        for project in self.values["projects"].values():
            project.pop("repository_path_env", None)
            project["repository_path"] = str(self.root / project["display_name"])
            tracker = project["tracker"]
            tracker.pop("project_id_env", None)
            tracker["project_id"] = "linear-" + project["display_name"]

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        path = self.root / "factory.json"
        path.write_text(json.dumps(self.values))
        return path

    def test_schema_five_resolves_scheduler_policy(self):
        policy = FactoryConfig.load(self.write()).resolve_scheduler()
        self.assertEqual(3, policy["limits"]["host"])
        self.assertEqual(2, policy["limits"]["projects"]["example-ios"])
        self.assertEqual(2, policy["limits"]["runners"]["codex"])

    def test_invalid_scheduler_limits_block_activation(self):
        for value in (True, 0, 4):
            with self.subTest(value=value):
                self.values["scheduler"]["limits"]["runners"]["codex"] = value
                with self.assertRaises(ValueError):
                    FactoryConfig.load(self.write())
        self.values["scheduler"]["limits"]["runners"]["codex"] = 2
        self.values["scheduler"]["limits"]["projects"]["missing"] = 1
        with self.assertRaisesRegex(ValueError, "unknown projects"):
            FactoryConfig.load(self.write())

    def test_schema_four_uses_safe_scheduler_defaults(self):
        self.values["schema_version"] = 4
        self.values.pop("scheduler")
        policy = FactoryConfig.load(self.write()).resolve_scheduler()
        self.assertEqual(1, policy["limits"]["host"])


class SchemaEightMigrationTests(unittest.TestCase):
    def test_schema_seven_adds_scheduler_dispatches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factory.db"
            ledger = SQLiteLedger(path)
            ledger.connection.execute("DROP TABLE scheduler_dispatches")
            ledger.connection.execute("PRAGMA user_version=7")
            ledger.close()
            migrated = SQLiteLedger(path)
            self.assertEqual(11, migrated.connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0])
            self.assertIsNotNone(migrated.connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='scheduler_dispatches'"
            ).fetchone())
            migrated.close()


if __name__ == "__main__":
    unittest.main()
