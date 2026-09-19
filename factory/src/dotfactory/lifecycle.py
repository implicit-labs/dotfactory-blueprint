"""One-process composition for a recoverable dotfactory lifecycle."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import queue
import socket
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .control import ControlService, ObservationService
from .instance import FactoryConfig, FactoryConfigError
from .kernel import DurableKernel
from .ledger import SQLiteLedger
from .linear_agent import LinearAgentSessionWorker, build_agent_projection
from .linear_api import LinearAPIError, LinearConvergenceWorker, LinearGraphQLClient
from .linear_evidence import LinearEvidenceWorker, render_linear_run_summary
from .live_runner import LiveRunner, LiveRunnerRouter, RunnerRoute
from .observability import canonical_json
from .portless import PortlessProvider
from .resources import FakePreparedRunner, PreparationEngine, PreparedRunner
from .runner import RunnerResult
from .scheduler import (
    ProjectPreparation, ScheduledProject, Scheduler, SchedulerPolicy, SchedulerTick,
)
from .telemetry import LogfireProjectionWorker, LogfireSettings
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
    target_state: str | None = None

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


def _runner_routes(
    config: FactoryConfig, *, environment: dict[str, str] | None = None,
) -> dict[str, RunnerRoute]:
    return {
        name: RunnerRoute(**values)
        for name, values in config.resolve_runners(environment=environment).items()
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
        self.drain_requested = False
        self.operator_server: Any | None = None
        self._last_live_poll = 0.0
        self._live_poll_inflight = False
        self._live_poll_results: queue.Queue[Any] = queue.Queue(maxsize=1)
        self.shutdown_reason = "settled"
        self.target_state: str | None = None
        self.target_execution_id: str | None = None
        self.lock = InstanceLock(_ledger_path(config).with_suffix(".lock"))
        self.lock.acquire()
        try:
            self.ledger = SQLiteLedger(_ledger_path(config))
            projections = config.values.get("projections", {})
            linear = projections.get("linear", {})
            logfire = projections.get("logfire", {})
            self.ledger.projection_configuration = {
                "linear": bool(linear.get("enabled", False)),
                "linear_agent": bool(linear.get("enabled", False) and linear.get("agent_sessions_enabled", False)),
                "logfire": bool(projections.get("logfire", {}).get("enabled", False)),
                "logfire_destination": f"logfire:{logfire.get('project', '')}:{logfire.get('region', 'us')}:otel-v2",
            }
            config.configure_ledger(
                self.ledger, environment=self.environment,
                only=list(self.project_keys),
            )
            self.kernels: dict[str, DurableKernel] = {}
            self.engines: dict[str, PreparationEngine] = {}
            self.projects: dict[str, ScheduledProject] = {}
            self.linear_workers: dict[str, LinearConvergenceWorker] = {}
            self.linear_evidence_workers: dict[str, LinearEvidenceWorker] = {}
            self.linear_agent_workers: dict[str, LinearAgentSessionWorker] = {}
            self.logfire_worker: LogfireProjectionWorker | None = None
            self.preflights: list[dict[str, Any]] = []
            self._build_projects()
            routes = _runner_routes(config, environment=self.environment)
            self.execution = None
            if config.values.get("execution"):
                from .execution import ExecutionManager, WorkerPreparation
                self.execution = ExecutionManager(self.ledger, config.values["execution"], routes)
                self.projects = {
                    key: ScheduledProject(value.kernel, WorkerPreparation(value.preparation, self.execution))
                    for key, value in self.projects.items()
                }
            if control_only:
                runner = LiveRunner(
                    self.ledger, routes=routes, environment=self.environment,
                    cancel_requested=self._runner_cancel_requested,
                )
                self.preflights.append({
                    "kind": "runner", "name": "control-only",
                    "available": False, "version": None,
                    "reason": "external runner preflight skipped",
                    "capabilities": [],
                })
            elif runner is None and self.execution is not None:
                runner = LiveRunner(
                    self.ledger, routes=routes, environment=self.environment,
                    cancel_requested=self._runner_cancel_requested,
                    execution=self.execution,
                )
                self.preflights.append({"kind": "worker", "available": None,
                                        "reason": "worker requirements checked before each stage"})
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
                    cancel_requested=self._runner_cancel_requested,
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
                dispatch_budget=self._dispatch_budget,
            )
            if control_only:
                self.preflights.append({
                    "kind": "linear", "available": False,
                    "reason": "external projection preflight skipped",
                })
                self.preflights.append({
                    "kind": "logfire", "available": False,
                    "reason": "external projection preflight skipped",
                })
            else:
                self._build_linear()
                self._build_logfire()
        except Exception:
            if hasattr(self, "ledger"):
                self.ledger.close()
            self.lock.close()
            raise

    def _build_projects(self) -> None:
        skill_directories = {
            name: str(values["skill_directory"])
            for name, values in self.config.resolve_runners(
                environment=self.environment
            ).items()
        }
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
                skill_directories=skill_directories,
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
            self.linear_evidence_workers[project_key] = LinearEvidenceWorker(
                self.ledger, client
            )
            if projection["agent_sessions_enabled"]:
                agent_token = str(self.environment.get(projection["agent_token_env"], "")).strip()
                if agent_token:
                    authorization = agent_token if agent_token.startswith("Bearer ") else "Bearer " + agent_token
                    agent_client = LinearGraphQLClient(
                        authorization, endpoint=projection["endpoint"],
                        timeout_seconds=projection["timeout_seconds"],
                    )
                    self.linear_agent_workers[project_key] = LinearAgentSessionWorker(
                        self.ledger, agent_client
                    )
                self.preflights.append({
                    "kind": "linear_agent", "project_key": project_key,
                    "available": bool(agent_token),
                    "reason": "configured" if agent_token else "agent token unavailable; using comment fallback",
                    "token_env": projection["agent_token_env"],
                })
            self.preflights.append({
                "kind": "linear", "project_key": project_key,
                "available": True, "binding_count": len(bindings),
                "agent_sessions_enabled": projection["agent_sessions_enabled"],
            })

    def _build_logfire(self) -> None:
        projection = self.config.resolve_logfire_projection(
            environment=self.environment
        )
        if not projection["enabled"]:
            self.preflights.append({
                "kind": "logfire", "available": False,
                "reason": "projection disabled",
                "project": projection.get("project"),
            })
            return
        settings = LogfireSettings(
            endpoint=str(projection["endpoint"]),
            headers=str(projection["headers"]),
            service_name=str(projection["service_name"]),
            project=str(projection["project"]), region=str(projection["region"]),
            timeout_seconds=int(projection["timeout_seconds"]),
        )
        self.logfire_worker = LogfireProjectionWorker(self.ledger, settings)
        self.preflights.append({
            "kind": "logfire", "available": True,
            "project": settings.project, "region": settings.region,
            "service_name": settings.service_name,
            "credential_kind": "write_token",
        })

    def request_stop(self, reason: str = "signal") -> None:
        self.stop_requested = True
        self.shutdown_reason = reason

    def enable_operator(self) -> None:
        from .operator import OperatorServer
        if self.operator_server is None:
            self.operator_server = OperatorServer(self)

    def _runner_cancel_requested(self, runner_run_id: str) -> bool:
        if self.operator_server:
            self.operator_server.pump()
        run = self.ledger.runner_run(runner_run_id)
        if self.stop_requested or run["status"] in ("canceled", "superseded"):
            return True
        self._poll_linear_during_run(run)
        run = self.ledger.runner_run(runner_run_id)
        return self.stop_requested or run["status"] in ("canceled", "superseded")

    def _poll_linear_during_run(self, run: Mapping[str, Any]) -> None:
        # Only the network read crosses threads. Observe/reconcile/command work
        # always runs here on the existing ledger writer.
        try:
            execution_id, project_key, issue, error = self._live_poll_results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._live_poll_inflight = False
            self.preflights[:] = [item for item in self.preflights
                                 if item.get("kind") != "linear-live-poll"]
            if error:
                self.preflights.append({"kind": "linear-live-poll", "available": False,
                                        "reason": error})
            elif self.ledger.current(execution_id)["status"] == "running":
                self.linear_workers[project_key].observe_issue(execution_id, issue)
        now = time.monotonic()
        if self._live_poll_inflight or now - self._last_live_poll < 5:
            return
        execution_id = str(run["execution_id"])
        current = self.ledger.current(execution_id)
        project_key = str(current["project_key"])
        worker = self.linear_workers.get(project_key)
        if not worker:
            return
        identifier = str(current["work_item_identifier"])
        client = worker.client
        results = self._live_poll_results
        self._last_live_poll = now
        self._live_poll_inflight = True

        def read() -> None:
            try:
                issue = client.issue(identifier)
                results.put((execution_id, project_key, issue, None))
            except Exception as error:
                reason = error.code if isinstance(error, LinearAPIError) else type(error).__name__
                results.put((execution_id, project_key, None, reason))

        threading.Thread(target=read, name="dotfactory-tracker-read", daemon=True).start()

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stop_requested and not self.drain_requested and time.monotonic() < deadline:
            if self.operator_server:
                self.operator_server.pump()
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

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

    def _next_execution_number(self, project_key: str, identifier: str) -> int:
        row = self.ledger.connection.execute(
            "SELECT COALESCE(MAX(we.execution_number),0)+1 AS next_number "
            "FROM work_items wi LEFT JOIN workflow_executions we "
            "ON we.work_item_id=wi.id "
            "WHERE wi.project_key=? AND wi.identifier=?",
            (project_key, identifier),
        ).fetchone()
        return int(row["next_number"] if row else 1)

    def start_issue(
        self, project_key: str, identifier: str, *, title: str | None = None,
        description: str = "",
        admission_snapshot: dict[str, Any] | None = None,
    ) -> str:
        if project_key not in self.kernels:
            raise LifecycleError(f"project is not enabled: {project_key}")
        existing = self._existing_execution(project_key, identifier)
        if existing:
            return existing
        intent = {"title": title or identifier, "description": description,
                  "source": "admitted_queue" if admission_snapshot is not None else "explicit_issue"}
        if admission_snapshot is not None:
            intent["admission"] = {
                "label": self.config.values.get("work_queue", {}).get("admission_label", "factory-ready"),
                "source_revision": admission_snapshot.get("updatedAt"),
                "eligibility_digest": hashlib.sha256(canonical_json(admission_snapshot).encode()).hexdigest(),
            }
        adopted_state = None
        worker = self.linear_workers.get(project_key)
        if worker:
            issue = admission_snapshot if admission_snapshot is not None else worker.client.issue(identifier)
            if str(issue["identifier"]) != identifier:
                raise LifecycleError("Linear returned a different issue identifier")
            project = self.config.resolve_project(project_key, environment=self.environment)
            if (issue.get("project") or {}).get("id") != project["tracker_project_id"]:
                raise LifecycleError("Linear issue belongs to a different project")
            if project.get("tracker_team_id") and (
                issue.get("team") or {}
            ).get("id") != project["tracker_team_id"]:
                raise LifecycleError("Linear issue belongs to a different team")
            candidates = [
                state["id"] for state in self.kernels[project_key].states.values()
                if state.get("checkpoint_role") == "pickup"
                and state.get("linear_status") == (issue.get("state") or {}).get("name")
            ]
            if len(candidates) != 1:
                raise LifecycleError("Linear issue must be in one eligible pickup checkpoint")
            adopted_state = str(candidates[0])
            intent.update({
                "title": str(issue.get("title") or title or identifier),
                "url": issue.get("url"), "linear_issue_id": issue["id"],
                "description": issue.get("description") or "",
                "source_revision": issue.get("updatedAt"),

            })
        if not isinstance(intent["description"], str) or len(intent["description"]) > 65536:
            raise LifecycleError("issue description must be text of at most 65536 characters")
        execution_number = self._next_execution_number(project_key, identifier)
        execution_id = self.kernels[project_key].begin(
            project_key, identifier, intent,
            command_id=(
                f"runtime-begin:{project_key}:{identifier}:{execution_number}"
            ),
            adopted_state=adopted_state,
        )
        if worker:
            # The adoption snapshot is the first observation. A second read here
            # could turn a concurrent human edit into an unsolicited status write.
            worker.observe_issue(execution_id, issue)
            self._sync_linear_evidence()
        return execution_id

    def discover_issue(self, project_key: str, *, allow_empty: bool = False) -> dict[str, Any] | None:
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
        if allow_empty:
            return None
        raise LifecycleError(
            f"no eligible Linear issue is available for {project_key}"
        )

    def _owned_runs(self, status: str | None = None) -> list[dict[str, Any]]:
        return [dict(row) for row in self.ledger.connection.execute(
            "SELECT we.*,wi.project_key,wi.identifier AS work_item_identifier "
            "FROM workflow_executions we JOIN work_items wi ON wi.id=we.work_item_id "
            "WHERE (? IS NULL OR we.status=?) ORDER BY we.created_at,we.id", (status, status),
        ) if row["project_key"] in self.kernels]

    def _poll_linear(self) -> None:
        for run in self._owned_runs("running"):
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

    def _sync_linear_evidence(self) -> None:
        if not self.linear_evidence_workers:
            return
        agent_workers = getattr(self, "linear_agent_workers", {})
        linear_projection = self.config.resolve_linear_projection(
            environment=self.environment
        ) if hasattr(self, "config") else {"agent_session_url_template": None}
        for run in self._owned_runs():
            execution_id = str(run["id"])
            project_key = str(run["project_key"])
            if project_key not in self.linear_evidence_workers:
                continue
            snapshot = self.ledger.run_snapshot(execution_id)
            issue_id = str(snapshot["intent"].get("linear_issue_id") or "")
            if not issue_id:
                continue
            service = ObservationService(self.ledger, self.kernels[project_key])
            projection = service.execution_projection(execution_id)
            body, digest = render_linear_run_summary(
                snapshot, projection, self.ledger.run_history(execution_id)
            )
            agent_worker = agent_workers.get(project_key)
            if agent_worker:
                template = str(linear_projection["agent_session_url_template"])
                marker_url = template.replace("{execution_id}", execution_id)
                external_urls, activities = build_agent_projection(
                    snapshot, projection, self.ledger.run_history(execution_id),
                    marker_url=marker_url,
                )
                agent_status = agent_worker.sync(
                    execution_id, issue_id=issue_id, marker_url=marker_url,
                    external_urls=external_urls, activities=activities,
                )
                if agent_status != "fallback":
                    continue
            self.ledger.stage_linear_evidence(
                execution_id, issue_id=issue_id, body=body, digest=digest,
            )
        for item in self.ledger.pending_linear_evidence(1000):
            project_key = str(self.ledger.current(
                str(item["execution_id"])
            )["project_key"])
            worker = self.linear_evidence_workers.get(project_key)
            if worker:
                worker.drain_one(item)

    def _drain_logfire(self) -> dict[str, Any] | None:
        if not self.logfire_worker:
            return None
        return self.logfire_worker.drain()

    def _claim_pickups(self) -> list[str]:
        claimed = []
        for run in self._owned_runs("running"):
            project_key = str(run["project_key"])
            kernel = self.kernels.get(project_key)
            if not kernel:
                continue
            current = self.ledger.current(str(run["id"]))
            if current.get("attempt"):
                continue
            if self.ledger.run_snapshot(str(run["id"]))["attention_requests"]:
                continue
            pending = self.ledger.pending_transition(str(run["id"]))
            if pending:
                kernel.claim_pending_transition(
                    str(run["id"]), owner=f"{self.owner}:human-handoff",
                    command_id=f"runtime-handoff:{pending['id']}",
                )
                claimed.append(str(run["id"]))
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
            if states[str(candidates[0]["to"])].get("execution", {}).get("exit_contract") == "implementation-result-v2":
                from .delivery import DeliveryError, guard_planned_transition
                try:
                    guard_planned_transition(self.ledger, str(run["id"]), states, state_id, str(candidates[0]["to"]))
                except DeliveryError:
                    # Keep the owner/operator available for planning or approval repair.
                    continue
            kernel.transition(
                str(run["id"]), str(candidates[0]["to"]), actor="agent",
                signal="listener_claim", owner=f"{self.owner}:{state_id}",
                command_id=f"runtime-claim:{run['id']}:{current['current_state_run_id']}",
            )
            claimed.append(str(run["id"]))
        return claimed

    def _cleanup_terminal_workspaces(self) -> list[dict[str, Any]]:
        results = []
        for run in self._owned_runs():
            if run["status"] != "completed":
                continue
            project_key = str(run["project_key"])
            if project_key not in self.engines:
                continue
            preparation = self.config.resolve_preparation(
                project_key, environment=self.environment
            )
            if preparation["workspace"]["retention"] != "until_terminal":
                continue
            workspace = self.ledger.workspace_for_execution(str(run["id"]))
            if not workspace or workspace["status"] == "cleaned" or (
                workspace["metadata"].get("cleanup_policy") in ("retain", "quarantine")
            ):
                continue
            result = self.engines[project_key].cleanup_workspace(str(run["id"]))
            results.append({
                "execution_id": run["id"], "disposition": result.disposition,
                "attention_id": (
                    result.attention.get("id") if result.attention else None
                ),
            })
        return results

    def _dispatch_budget(self, project: str, execution: str) -> dict[str, Any]:
        from .budgets import evaluate
        decision = evaluate(self.ledger, self.config.values.get("budgets", {}), project, execution)
        self.ledger.record_operating_receipt("budget", project, execution, decision)
        return decision

    def step(self) -> dict[str, Any]:
        if self.operator_server:
            self.operator_server.pump()
        if self.drain_requested or self.stop_requested:
            return {"claimed": [], "scheduler": {"disposition": "idle"}, "cleanup": []}
        self._poll_linear()
        self._drain_linear()
        if self._at_target():
            return {"claimed": [], "scheduler": {"disposition": "idle"}, "cleanup": []}
        claimed = self._claim_pickups()
        self._drain_linear()
        if self._at_target():
            return {"claimed": claimed, "scheduler": {"disposition": "idle"}, "cleanup": []}
        tick = self.scheduler.tick()
        self._drain_linear()
        cleanup = self._cleanup_terminal_workspaces()
        self._sync_linear_evidence()
        logfire = self._drain_logfire()
        return {"claimed": claimed, "scheduler": {
            "disposition": tick.disposition,
            "dispatch_id": tick.dispatch_id,
            "execution_id": tick.execution_id,
            "attempt_id": tick.attempt_id,
            "detail": dict(tick.detail),
        }, "cleanup": cleanup, "logfire": logfire}

    def _settled(self, step: Mapping[str, Any]) -> bool:
        if step["claimed"]:
            return False
        disposition = str(step["scheduler"]["disposition"])
        return disposition in ("idle", "capacity", "needs_attention", "budget_blocked")

    def _at_target(self) -> bool:
        return bool(self.target_execution_id and self.target_state and self.ledger.current(
            self.target_execution_id)["current_state_id"] == self.target_state)

    def run(
        self, execution_ids: list[str], *, watch: bool = False,
        max_ticks: int | None = 100, until_state: str | None = None,
    ) -> LifecycleReceipt:
        if max_ticks is not None and max_ticks < 1:
            raise LifecycleError("max_ticks must be positive")
        self.shutdown_reason = "settled"
        self.target_state = until_state
        self.target_execution_id = execution_ids[0] if until_state and len(execution_ids) == 1 else None
        if until_state:
            if len(execution_ids) != 1:
                raise LifecycleError("until_state requires one execution")
            execution = execution_ids[0]
            current = self.ledger.current(execution)
            _, states, edges = self.kernels[current["project_key"]].graph_for_execution(execution)
            if until_state not in states:
                raise LifecycleError("unknown target state")
            history = self.ledger.run_history(execution)["state_runs"]
            if current["current_state_id"] != until_state and any(
                item["state_id"] == until_state for item in history
            ):
                raise LifecycleError("target state was already passed")
            reachable = {current["current_state_id"]}
            for _ in states:
                reachable.update(edge["to"] for edge in edges if edge["from"] in reachable
                                 or edge["from"] == "@any_nonterminal" and any(
                                     states[item]["kind"] != "terminal" for item in reachable
                                     if item in states))
            if until_state not in reachable:
                raise LifecycleError("target state is unreachable")
        self.started_at = self.ledger.clock()
        ticks = []
        while True:
            if self.stop_requested:
                break
            if until_state and self.ledger.current(execution_ids[0])["current_state_id"] == until_state:
                self._drain_linear()
                self._sync_linear_evidence()
                current = self.ledger.current(execution_ids[0])
                projected = current["project_key"] not in self.linear_workers or (
                    current["desired_linear_status"] == current["observed_linear_status"]
                )
                self.shutdown_reason = "target_state" if projected else "target_projection_pending"
                if not ticks:
                    ticks.append({"claimed": [], "scheduler": {"disposition": "idle"}, "cleanup": []})
                break
            if max_ticks is not None and len(ticks) >= max_ticks:
                self.shutdown_reason = "max_ticks"
                break
            step = self.step()
            ticks.append(step)
            if until_state and self.ledger.current(execution_ids[0])["current_state_id"] == until_state:
                continue
            if self.drain_requested:
                self.shutdown_reason = "drained"
                break
            if not watch and self._settled(step):
                if until_state:
                    self.shutdown_reason = "target_not_reached"
                break
            if watch and self._settled(step):
                self._wait(self.scheduler.policy.poll_interval_ms / 1000)
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
            item = dict(projection["summary"])
            item["linear_evidence"] = projection["linear_evidence"]
            item["linear_agent_session"] = projection["linear_agent_session"]
            item["projection_health"] = self.ledger.run_snapshot(execution_id)["projection_health"]
            executions.append(item)
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
            "target_state": getattr(self, "target_state", None),
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
            target_state=payload["target_state"],
        )

    def close(self) -> None:
        if self.operator_server:
            self.operator_server.close()
            self.operator_server = None
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
