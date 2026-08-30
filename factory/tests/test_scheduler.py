import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotfactory import (
    ControlService, DurableKernel, FactoryConfig, FakePreparedRunner,
    PreparationEngine, PreparationResult, PreparedLaunch, Principal,
    RunnerResult, ScheduledProject, Scheduler, SchedulerPolicy, SQLiteLedger,
)
from dotfactory.ledger import StaleAttempt
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

    def scheduler(self, kernel, preparation, runner=None, fault_hook=None, observer=None):
        return Scheduler(
            self.ledger,
            projects={"alpha": ScheduledProject(kernel, preparation)},
            runner=runner or FakePreparedRunner([
                RunnerResult("succeeded", "complete", EVIDENCE)
            ]),
            owner="scheduler-a",
            policy=SchedulerPolicy(
                claim_ttl_seconds=120, host_limit=2,
                project_limits={"alpha": 2}, runner_limits={"codex": 2},
            ),
            fault_hook=fault_hook, observer=observer,
        )

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
            self.assertEqual(10, migrated.connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0])
            self.assertIsNotNone(migrated.connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='scheduler_dispatches'"
            ).fetchone())
            migrated.close()


if __name__ == "__main__":
    unittest.main()
