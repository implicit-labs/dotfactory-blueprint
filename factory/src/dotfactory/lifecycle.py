"""One-process composition for a recoverable dotfactory lifecycle."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .control import ControlService, ObservationService
from .instance import FactoryConfig, FactoryConfigError
from .kernel import DurableKernel
from .ledger import SQLiteLedger
from .linear_api import LinearConvergenceWorker, LinearGraphQLClient
from .live_runner import LiveRunner, LiveRunnerRouter, RunnerRoute
from .observability import canonical_json
from .portless import PortlessProvider
from .resources import FakePreparedRunner, PreparationEngine, PreparedRunner
from .runner import RunnerResult
from .scheduler import (
    ProjectPreparation, ScheduledProject, Scheduler, SchedulerPolicy, SchedulerTick,
)
from .workspace import GitWorkspaceProvider


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class LifecycleReceipt:
    schema_version: int
    factory_id: str
    mode: str
    owner: str
    sqlite_version: str
    concurrent_writers_allowed: bool
    sqlite_concurrency_safe: bool
    started_at: str
    completed_at: str
    ticks: tuple[Mapping[str, Any], ...]
    executions: tuple[Mapping[str, Any], ...]
    preflights: tuple[Mapping[str, Any], ...]
    shutdown_reason: str
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.handle.close()
            self.handle = None
            raise LifecycleError(
                f"another dotfactory process owns {self.path}"
            ) from error
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()} host={socket.gethostname()}\n")
        self.handle.flush()

    def close(self) -> None:
        if self.handle is None:
            return
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def _ledger_path(config: FactoryConfig) -> Path:
    candidate = Path(str(config.values["ledger_path"])).expanduser()
    if not candidate.is_absolute():
        candidate = config.path.parent / candidate
    return candidate.resolve()


def _runner_routes(config: FactoryConfig) -> dict[str, RunnerRoute]:
    return {
        name: RunnerRoute(**values) for name, values in config.resolve_runners().items()
    }


def _sqlite_concurrency_safe() -> bool:
    version = sqlite3.sqlite_version_info
    return not ((3, 51, 0) <= version < (3, 51, 3))


class FactoryRuntime:
    def __init__(
        self, config: FactoryConfig, *, environment: Mapping[str, str] | None = None,
        runner: PreparedRunner | None = None, owner: str | None = None,
        project_keys: list[str] | None = None, control_only: bool = False,
    ) -> None:
        self.config = config
        if config.values["schema_version"] != 6:
            raise LifecycleError("composed lifecycle requires factory config schema_version 6")
        self.environment = dict(os.environ if environment is None else environment)
        self.owner = owner or f"{config.values['factory_id']}:{socket.gethostname()}"
        self.project_keys = config.selected_project_keys(project_keys)
        self.control_only = control_only
        self.started_at = ""
        self.stop_requested = False
        self.shutdown_reason = "settled"
        self.lock = InstanceLock(_ledger_path(config).with_suffix(".lock"))
        self.lock.acquire()
        try:
            self.ledger = SQLiteLedger(_ledger_path(config))
            config.configure_ledger(
                self.ledger, environment=self.environment,
                only=list(self.project_keys),
            )
            self.kernels: dict[str, DurableKernel] = {}
            self.engines: dict[str, PreparationEngine] = {}
            self.projects: dict[str, ScheduledProject] = {}
            self.linear_workers: dict[str, LinearConvergenceWorker] = {}
            self.preflights: list[dict[str, Any]] = []
            self._build_projects()
            routes = _runner_routes(config)
            if control_only:
                runner = LiveRunner(
                    self.ledger, routes=routes, environment=self.environment,
                    cancel_requested=lambda _run_id: self.stop_requested,
                )
                self.preflights.append({
                    "kind": "runner", "name": "control-only",
                    "available": False, "version": None,
                    "reason": "external runner preflight skipped",
                    "capabilities": [],
                })
            elif runner is None:
                router = LiveRunnerRouter(self.ledger, routes)
                reports = router.preflight_all()
                self.preflights.extend({
                    "kind": "runner", "name": name,
                    "available": report.available, "version": report.version,
                    "reason": report.reason,
                    "capabilities": list(report.capabilities),
                } for name, report in reports.items())
                required = sorted({
                    str(state.get("execution", {}).get("runner"))
                    for kernel in self.kernels.values()
                    for state in kernel.states.values()
                    if state.get("kind") == "work"
                })
                missing = [name for name in required if name not in reports]
                unavailable = [
                    name for name in required
                    if name in reports and not reports[name].available
                ]
                if missing:
                    raise LifecycleError(
                        "workflow requires unconfigured runners: " + ", ".join(missing)
                    )
                if unavailable:
                    raise LifecycleError(
                        "workflow runners failed preflight: " + ", ".join(unavailable)
                    )
                runner = LiveRunner(
                    self.ledger, routes=routes, environment=self.environment,
                    cancel_requested=lambda _run_id: self.stop_requested,
                )
            else:
                self.preflights.append({
                    "kind": "runner", "name": "injected-fixture",
                    "available": True, "version": "fixture",
                    "reason": None, "capabilities": [],
                })
            self.scheduler = Scheduler(
                self.ledger, projects=self.projects, runner=runner,
                owner=self.owner,
                policy=SchedulerPolicy.from_config(config.resolve_scheduler()),
            )
            if control_only:
                self.preflights.append({
                    "kind": "linear", "available": False,
                    "reason": "external projection preflight skipped",
                })
            else:
                self._build_linear()
        except Exception:
            if hasattr(self, "ledger"):
                self.ledger.close()
            self.lock.close()
            raise

    def _build_projects(self) -> None:
        for project_key in self.project_keys:
            project = self.config.resolve_project(
                project_key, environment=self.environment
            )
            workflow = self.config.resolve_workflow(project_key)
            kernel = DurableKernel(
                self.ledger, workflow["path"],
                profile_paths=workflow["profile_paths"],
                factory_defaults=workflow["defaults"],
            )
            for state in kernel.states.values():
                execution = state.get("execution", {})
                resources = execution.get("resources", [])
                self.config.validate_resource_names(project_key, resources)
                if state.get("kind") == "work" and not str(
                    execution.get("prompt", "")
                ).strip():
                    raise LifecycleError(
                        f"workflow work state {state['id']} has no immutable prompt"
                    )
            preparation = self.config.resolve_preparation(
                project_key, environment=self.environment
            )
            providers = {}
            for name, values in preparation["providers"].items():
                if values["kind"] != "portless":
                    raise FactoryConfigError(
                        f"unsupported preparation provider: {values['kind']}"
                    )
                providers[name] = PortlessProvider(
                    command=str(values["command"]), version=str(values["version"]),
                    node_minimum=int(values["node_minimum"]),
                    preflight_command=str(values["preflight_command"]),
                )
            engine = PreparationEngine(
                self.ledger, workspace_provider=GitWorkspaceProvider(),
                providers=providers, owner_token=self.owner,
            )
            self.kernels[project_key] = kernel
            self.engines[project_key] = engine
            self.projects[project_key] = ScheduledProject(
                kernel, ProjectPreparation(engine, project, preparation)
            )

    def _build_linear(self) -> None:
        projection = self.config.resolve_linear_projection(
            environment=self.environment
        )
        if not projection["enabled"]:
            self.preflights.append({
                "kind": "linear", "available": False,
                "reason": "projection disabled",
            })
            return
        client = LinearGraphQLClient(
            self.config.linear_authorization(environment=self.environment),
            endpoint=projection["endpoint"],
            timeout_seconds=projection["timeout_seconds"],
        )
        for project_key, kernel in self.kernels.items():
            project = self.config.resolve_project(
                project_key, environment=self.environment
            )
            team_id = project.get("tracker_team_id")
            if not team_id:
                raise LifecycleError(
                    f"Linear project {project_key} has no configured team ID"
                )
            worker = LinearConvergenceWorker(self.ledger, kernel, client)
            bindings = worker.preflight_project(
                project_key=project_key, team_id=str(team_id),
                project_id=str(project["tracker_project_id"]),
            )
            self.linear_workers[project_key] = worker
            self.preflights.append({
                "kind": "linear", "project_key": project_key,
                "available": True, "binding_count": len(bindings),
            })

    def request_stop(self, reason: str = "signal") -> None:
        self.stop_requested = True
        self.shutdown_reason = reason

    def control_service(self, project_key: str) -> ControlService:
        if project_key not in self.kernels:
            raise LifecycleError(f"project is not enabled: {project_key}")
        controllers: dict[str, Any] = {"scheduler": self.scheduler}
        if hasattr(self.scheduler.runner, "remedy_attention"):
            for runner_key in self.config.resolve_runners():
                controllers[runner_key] = self.scheduler.runner
        return ControlService(
            self.ledger, self.kernels[project_key],
            resource_controller=self.engines[project_key],
            attention_controllers=controllers,
            project_key=project_key,
        )

    def _existing_execution(self, project_key: str, identifier: str) -> str | None:
        row = self.ledger.connection.execute(
            "SELECT we.id FROM workflow_executions we "
            "JOIN work_items wi ON wi.id=we.work_item_id "
            "WHERE wi.project_key=? AND wi.identifier=? AND we.status='running' "
            "ORDER BY we.execution_number DESC LIMIT 1",
            (project_key, identifier),
        ).fetchone()
        return str(row["id"]) if row else None

    def _has_execution(self, project_key: str, identifier: str) -> bool:
        return self.ledger.connection.execute(
            "SELECT 1 FROM workflow_executions we "
            "JOIN work_items wi ON wi.id=we.work_item_id "
            "WHERE wi.project_key=? AND wi.identifier=? LIMIT 1",
            (project_key, identifier),
        ).fetchone() is not None

    def start_issue(
        self, project_key: str, identifier: str, *, title: str | None = None,
    ) -> str:
        if project_key not in self.kernels:
            raise LifecycleError(f"project is not enabled: {project_key}")
        existing = self._existing_execution(project_key, identifier)
        if existing:
            return existing
        intent = {"title": title or identifier, "source": "explicit_issue"}
        worker = self.linear_workers.get(project_key)
        if worker:
            issue = worker.client.issue(identifier)
            if str(issue["identifier"]) != identifier:
                raise LifecycleError("Linear returned a different issue identifier")
            intent.update({
                "title": str(issue.get("title") or title or identifier),
                "url": issue.get("url"), "linear_issue_id": issue["id"],
            })
        execution_id = self.kernels[project_key].begin(
            project_key, identifier, intent,
            command_id=f"runtime-begin:{project_key}:{identifier}",
        )
        if worker:
            self._drain_linear()
            worker.poll(execution_id, identifier)
        return execution_id

    def discover_issue(self, project_key: str) -> dict[str, Any]:
        worker = self.linear_workers.get(project_key)
        if not worker:
            raise LifecycleError(
                "automatic issue discovery requires enabled Linear projection"
            )
        project = self.config.resolve_project(
            project_key, environment=self.environment
        )
        pickup_statuses = sorted({
            str(state["linear_status"])
            for state in self.kernels[project_key].states.values()
            if state.get("checkpoint_role") == "pickup" and state.get("linear_status")
        })
        issues = worker.client.eligible_issues(
            project_id=str(project["tracker_project_id"]),
            status_names=pickup_statuses,
        )
        for issue in issues:
            identifier = str(issue.get("identifier", ""))
            if identifier and not self._has_execution(project_key, identifier):
                return issue
        raise LifecycleError(
            f"no eligible Linear issue is available for {project_key}"
        )

    def _poll_linear(self) -> None:
        for run in self.ledger.list_runs(status="running", limit=1000):
            worker = self.linear_workers.get(str(run["project_key"]))
            if worker:
                worker.poll(str(run["id"]), str(run["work_item_identifier"]))

    def _drain_linear(self) -> None:
        if not self.linear_workers:
            return
        # Re-read after every delivery. Confirming one mutation can reconcile and
        # confirm another pending mutation for the same desired status.
        for _index in range(100):
            pending = self.ledger.pending_linear_mutations(100)
            owned = [
                item for item in pending
                if self.ledger.current(str(item["execution_id"]))["project_key"]
                in self.linear_workers
            ]
            if not owned:
                break
            item = owned[0]
            project_key = str(self.ledger.current(
                str(item["execution_id"])
            )["project_key"])
            self.linear_workers[project_key].drain_one(item)

    def _claim_pickups(self) -> list[str]:
        claimed = []
        for run in reversed(self.ledger.list_runs(status="running", limit=1000)):
            project_key = str(run["project_key"])
            kernel = self.kernels.get(project_key)
            if not kernel:
                continue
            current = self.ledger.current(str(run["id"]))
            if current.get("attempt"):
                continue
            _workflow, states, edges = kernel.graph_for_execution(str(run["id"]))
            state_id = str(current["current_state_id"])
            state = states[state_id]
            if state.get("checkpoint_role") != "pickup":
                continue
            candidates = [
                edge for edge in edges
                if edge["from"] == state_id
                and {"actor": "agent", "signal": "listener_claim"}
                in edge["evocations"]
            ]
            if len(candidates) != 1:
                raise LifecycleError(
                    f"pickup state {state_id} requires exactly one agent claim edge"
                )
            kernel.transition(
                str(run["id"]), str(candidates[0]["to"]), actor="agent",
                signal="listener_claim", owner=f"{self.owner}:{state_id}",
                command_id=f"runtime-claim:{run['id']}:{state_id}",
            )
            claimed.append(str(run["id"]))
        return claimed

    def _cleanup_terminal_workspaces(self) -> list[dict[str, Any]]:
        results = []
        for run in self.ledger.list_runs(limit=1000):
            if run["status"] != "completed":
                continue
            project_key = str(run["project_key"])
            preparation = self.config.resolve_preparation(
                project_key, environment=self.environment
            )
            if preparation["workspace"]["retention"] != "until_terminal":
                continue
            workspace = self.ledger.workspace_for_execution(str(run["id"]))
            if not workspace or workspace["status"] == "cleaned":
                continue
            result = self.engines[project_key].cleanup_workspace(str(run["id"]))
            results.append({
                "execution_id": run["id"], "disposition": result.disposition,
                "attention_id": (
                    result.attention.get("id") if result.attention else None
                ),
            })
        return results

    def step(self) -> dict[str, Any]:
        self._poll_linear()
        self._drain_linear()
        claimed = self._claim_pickups()
        self._drain_linear()
        tick = self.scheduler.tick()
        self._drain_linear()
        cleanup = self._cleanup_terminal_workspaces()
        return {"claimed": claimed, "scheduler": {
            "disposition": tick.disposition,
            "dispatch_id": tick.dispatch_id,
            "execution_id": tick.execution_id,
            "attempt_id": tick.attempt_id,
            "detail": dict(tick.detail),
        }, "cleanup": cleanup}

    def _settled(self, step: Mapping[str, Any]) -> bool:
        if step["claimed"]:
            return False
        disposition = str(step["scheduler"]["disposition"])
        return disposition in ("idle", "capacity", "needs_attention")

    def run(
        self, execution_ids: list[str], *, watch: bool = False,
        max_ticks: int = 100,
    ) -> LifecycleReceipt:
        self.started_at = self.ledger.clock()
        ticks = []
        for _index in range(max_ticks):
            if self.stop_requested:
                break
            step = self.step()
            ticks.append(step)
            if not watch and self._settled(step):
                break
            if watch and self._settled(step):
                time.sleep(self.scheduler.policy.poll_interval_ms / 1000)
        else:
            self.shutdown_reason = "max_ticks"
        return self.receipt(execution_ids, ticks)

    def receipt(
        self, execution_ids: list[str], ticks: list[dict[str, Any]],
    ) -> LifecycleReceipt:
        executions = []
        for execution_id in execution_ids:
            service = ObservationService(
                self.ledger,
                self.kernels[str(self.ledger.current(execution_id)["project_key"])],
            )
            projection = service.execution_projection(execution_id)
            executions.append(projection["summary"])
        payload: dict[str, Any] = {
            "schema_version": 1,
            "factory_id": str(self.config.values["factory_id"]),
            "mode": "single-process-single-writer",
            "owner": self.owner,
            "sqlite_version": sqlite3.sqlite_version,
            "concurrent_writers_allowed": False,
            "sqlite_concurrency_safe": _sqlite_concurrency_safe(),
            "started_at": self.started_at,
            "completed_at": self.ledger.clock(),
            "ticks": ticks,
            "executions": executions,
            "preflights": self.preflights,
            "shutdown_reason": self.shutdown_reason,
        }
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        return LifecycleReceipt(
            schema_version=1, factory_id=payload["factory_id"], mode=payload["mode"],
            owner=payload["owner"], sqlite_version=payload["sqlite_version"],
            concurrent_writers_allowed=payload["concurrent_writers_allowed"],
            sqlite_concurrency_safe=payload["sqlite_concurrency_safe"],
            started_at=payload["started_at"], completed_at=payload["completed_at"],
            ticks=tuple(ticks), executions=tuple(executions),
            preflights=tuple(self.preflights),
            shutdown_reason=payload["shutdown_reason"], digest=digest,
        )

    def close(self) -> None:
        self.ledger.close()
        self.lock.close()

    def __enter__(self) -> "FactoryRuntime":
        return self

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
        self.close()


def fixture_runner(outcomes: int = 3) -> FakePreparedRunner:
    return FakePreparedRunner([
        RunnerResult(
            "succeeded", "complete",
            ({"kind": "demo", "uri": f"local://demo/{index + 1}"},),
        )
        for index in range(outcomes)
    ])
