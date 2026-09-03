"""Durable preparation-gated scheduler."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Mapping, Protocol

from .kernel import DurableKernel
from .ledger import LedgerError, SQLiteLedger, StaleAttempt, parse_timestamp
from .resources import (
    PreparationEngine, PreparationError, PreparationResult, PreparedLaunch,
    PreparedRunner,
)
from .runner import RunnerNeedsAttention, RunnerRequest, RunnerResult, runner_request


logger = logging.getLogger(__name__)


class PreparationCoordinator(Protocol):
    def prepare(self, request: RunnerRequest) -> PreparationResult: ...

    def cleanup_attempt(self, launch: PreparedLaunch) -> PreparationResult: ...


@dataclass(frozen=True)
class ProjectPreparation:
    engine: PreparationEngine
    project: Mapping[str, Any]
    config: Mapping[str, Any]

    def prepare(self, request: RunnerRequest) -> PreparationResult:
        return self.engine.prepare(
            request, project=self.project, preparation_config=self.config,
        )

    def cleanup_attempt(self, launch: PreparedLaunch) -> PreparationResult:
        return self.engine.cleanup_attempt(launch)


@dataclass(frozen=True)
class ScheduledProject:
    kernel: DurableKernel
    preparation: PreparationCoordinator


@dataclass(frozen=True)
class SchedulerPolicy:
    poll_interval_ms: int = 30000
    claim_ttl_seconds: int = 120
    host_limit: int = 1
    project_limits: Mapping[str, int] = field(default_factory=dict)
    runner_limits: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "SchedulerPolicy":
        limits = config["limits"]
        return cls(
            poll_interval_ms=int(config["poll_interval_ms"]),
            claim_ttl_seconds=int(config["claim_ttl_seconds"]),
            host_limit=int(limits["host"]),
            project_limits=dict(limits.get("projects", {})),
            runner_limits=dict(limits.get("runners", {})),
        )

    def as_limits(self) -> dict[str, Any]:
        return {
            "host": self.host_limit,
            "projects": dict(self.project_limits),
            "runners": dict(self.runner_limits),
        }


@dataclass(frozen=True)
class SchedulerTick:
    disposition: str
    dispatch_id: str | None = None
    execution_id: str | None = None
    attempt_id: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


class Scheduler:
    def __init__(
        self, ledger: SQLiteLedger, *, projects: Mapping[str, ScheduledProject],
        runner: PreparedRunner, owner: str, policy: SchedulerPolicy,
        observer: Callable[[SchedulerTick], None] | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.ledger = ledger
        self.projects = dict(projects)
        self.runner = runner
        self.owner = owner
        self.policy = policy
        self.observer = observer
        self.fault_hook = fault_hook

    def _fault(self, boundary: str) -> None:
        if self.fault_hook:
            self.fault_hook(boundary)

    def _emit(self, tick: SchedulerTick) -> SchedulerTick:
        if self.observer:
            try:
                self.observer(tick)
            except Exception as error:
                logger.warning(
                    "scheduler observer failed for %s: %s",
                    tick.disposition, error.__class__.__name__,
                )
        return tick

    def _project(self, dispatch: Mapping[str, Any]) -> ScheduledProject:
        project_key = str(dispatch["project_key"])
        if project_key not in self.projects:
            raise LedgerError(f"scheduler project is not active: {project_key}")
        return self.projects[project_key]

    def remedy_attention(
        self, execution_id: str, *, attention_id: str, remedy: str,
        command_id: str, expected_attempt_id: str | None,
    ) -> dict[str, Any]:
        """Authorize replay from a scheduler-owned durable safe point."""
        if remedy != "retry":
            raise LedgerError("scheduler attention currently supports only retry")
        if not command_id.strip():
            raise LedgerError("scheduler attention requires a command ID")
        attention = self.ledger.attention(attention_id)
        if attention["execution_id"] != execution_id:
            raise LedgerError("attention request belongs to another execution")
        if attention.get("provider") != "scheduler":
            raise LedgerError("attention request is not scheduler-owned")
        detail = dict(attention["detail"])
        resolution = detail.get("resolution", {})
        attempt_id = str(attention.get("attempt_id") or "")
        if not attempt_id or expected_attempt_id != attempt_id:
            raise StaleAttempt("scheduler attention does not match the expected attempt")
        if attention["status"] != "open":
            if (
                attention["status"] == "resolved"
                and isinstance(resolution, dict)
                and resolution.get("remedy") == remedy
                and resolution.get("command_id") == command_id
            ):
                dispatch_id = detail.get("dispatch_id")
                return {
                    "attention": attention, "remedy": remedy,
                    "dispatch": (
                        self.ledger.dispatch(str(dispatch_id))
                        if isinstance(dispatch_id, str) and dispatch_id else None
                    ),
                }
            raise LedgerError("scheduler attention is already resolved")
        if remedy not in detail.get("allowed_actions", []):
            raise LedgerError(f"{remedy} is not allowed for this attention request")
        dispatch_id = detail.get("dispatch_id")
        if not isinstance(dispatch_id, str) or not dispatch_id:
            raise LedgerError("scheduler attention has no durable dispatch")
        dispatch = self.ledger.dispatch(dispatch_id)
        self._project(dispatch)
        if (
            dispatch["execution_id"] != execution_id
            or dispatch["attempt_id"] != attempt_id
            or dispatch["attention_id"] != attention_id
            or dispatch["status"] != "attention"
        ):
            raise LedgerError("scheduler attention is not linked to its dispatch")
        error = dispatch.get("error")
        resume_phase = error.get("resume_phase") if isinstance(error, dict) else None
        if (
            resume_phase != detail.get("last_safe_step")
            or resume_phase not in ("claimed", "preparing", "prepared", "result_ready")
        ):
            raise LedgerError("scheduler attention has no retryable safe phase")
        self.ledger.assert_attempt_active(
            attempt_id, str(dispatch["attempt_fence_token"])
        )
        if resume_phase == "result_ready":
            result = dispatch.get("result")
            evidence = result.get("evidence") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("outcome"), str)
                or not isinstance(result.get("preferred_label"), str)
                or not isinstance(evidence, list) or not evidence
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("kind"), str) or not item["kind"]
                    or not isinstance(item.get("uri"), str) or not item["uri"]
                    for item in evidence
                )
            ):
                raise LedgerError("scheduler result-ready payload is invalid")
            preparation_id = dispatch.get("preparation_id")
            if (
                not isinstance(preparation_id, str)
                or attention.get("preparation_id") != preparation_id
            ):
                raise LedgerError("scheduler result is not linked to its preparation")
            preparation = self.ledger.preparation(preparation_id)
            if (
                preparation["status"] != "ready"
                or preparation["attempt_id"] != attempt_id
                or preparation["result_digest"] != dispatch["preparation_digest"]
            ):
                raise LedgerError("scheduler result preparation is not replayable")
        resolved = self.ledger.resolve_attention(
            attention_id, resolution="resolved",
            detail={"remedy": remedy, "command_id": command_id},
        )
        return {
            "attention": resolved, "remedy": remedy,
            "dispatch": self.ledger.dispatch(dispatch_id),
        }

    def _request(
        self, dispatch: Mapping[str, Any], project: ScheduledProject,
    ) -> RunnerRequest:
        request = runner_request(project.kernel, str(dispatch["execution_id"]))
        if (
            request.attempt_id != dispatch["attempt_id"]
            or request.fence_token != dispatch["attempt_fence_token"]
        ):
            raise StaleAttempt("scheduler dispatch no longer matches the active attempt")
        return request

    def _attention(
        self, dispatch: Mapping[str, Any], *, category: str, message: str,
        resume_phase: str,
    ) -> SchedulerTick:
        allowed_actions = (
            ("retry",) if resume_phase in (
                "claimed", "preparing", "prepared", "result_ready",
            ) else ()
        )
        preparation = self.ledger.preparation_for_attempt(
            str(dispatch["attempt_id"])
        )
        if (
            "retry" in allowed_actions and preparation
            and preparation["status"] in ("failed", "busy", "preparing")
        ):
            preparation = self.ledger.fail_preparation(
                str(preparation["id"]),
                fence_token=str(dispatch["attempt_fence_token"]),
                status="needs_attention",
                error={"message": message, "category": category},
            )
        attention = self.ledger.open_attention(
            execution_id=str(dispatch["execution_id"]),
            attempt_id=str(dispatch["attempt_id"]),
            preparation_id=str(preparation["id"]) if preparation else None,
            dedupe_key=f"scheduler:{dispatch['id']}:{category}",
            category=category,
            provider="scheduler",
            detail={
                "dispatch_id": str(dispatch["id"]),
                "last_safe_step": resume_phase,
                "allowed_actions": list(allowed_actions),
                "message": message,
            },
        )
        current = self.ledger.dispatch(str(dispatch["id"]))
        self.ledger.mark_dispatch_attention(
            str(dispatch["id"]), claim_token=str(current["claim_token"]),
            attention_id=str(attention["id"]),
            error={"message": message, "category": category,
                   "resume_phase": resume_phase},
        )
        return self._emit(SchedulerTick(
            "needs_attention", dispatch_id=str(dispatch["id"]),
            execution_id=str(dispatch["execution_id"]),
            attempt_id=str(dispatch["attempt_id"]),
            detail={"attention_id": attention["id"], "category": category},
        ))

    def dispatch_prepared(self, launch: PreparedLaunch) -> RunnerResult:
        if not isinstance(launch, PreparedLaunch):
            raise PreparationError("scheduler dispatch requires PreparedLaunch")
        result = self.runner.run(launch)
        if not isinstance(result, RunnerResult):
            raise PreparationError("runner did not return RunnerResult")
        return result

    def _commit_result(
        self, dispatch: Mapping[str, Any], project: ScheduledProject,
        launch: PreparedLaunch, result: RunnerResult, *, recovered: bool,
    ) -> SchedulerTick:
        cleanup = project.preparation.cleanup_attempt(launch)
        if cleanup.disposition != "ready":
            attention_id = (
                str(cleanup.attention["id"]) if cleanup.attention else None
            )
            if attention_id:
                current = self.ledger.dispatch(str(dispatch["id"]))
                self.ledger.mark_dispatch_attention(
                    str(dispatch["id"]), claim_token=str(current["claim_token"]),
                    attention_id=attention_id,
                    error={"message": "attempt cleanup requires attention",
                           "category": "cleanup", "resume_phase": "result_ready"},
                )
                return self._emit(SchedulerTick(
                    "needs_attention", dispatch_id=str(dispatch["id"]),
                    execution_id=str(dispatch["execution_id"]),
                    attempt_id=str(dispatch["attempt_id"]),
                    detail={"attention_id": attention_id, "category": "cleanup"},
                ))
            raise PreparationError("attempt cleanup failed before result commit")
        self._fault("after_attempt_cleanup")
        decision = project.kernel.complete_attempt(
            str(dispatch["execution_id"]),
            preferred_label=result.preferred_label, outcome=result.outcome,
            evidence=list(result.evidence), attempt_id=str(dispatch["attempt_id"]),
            fence_token=str(dispatch["attempt_fence_token"]),
            owner=launch.request.owner,
            command_id=f"scheduler:{dispatch['id']}:complete",
        )
        self._fault("after_workflow_commit")
        current = self.ledger.dispatch(str(dispatch["id"]))
        self.ledger.complete_dispatch(
            str(dispatch["id"]), claim_token=str(current["claim_token"])
        )
        self._fault("after_dispatch_complete")
        return self._emit(SchedulerTick(
            "recovered" if recovered else "completed",
            dispatch_id=str(dispatch["id"]),
            execution_id=str(dispatch["execution_id"]),
            attempt_id=str(dispatch["attempt_id"]),
            detail={"to_state": decision["to_state"]},
        ))

    def _handle_preparation(
        self, dispatch: Mapping[str, Any], project: ScheduledProject,
        request: RunnerRequest, *, recovered: bool,
    ) -> SchedulerTick:
        preparation = project.preparation.prepare(request)
        self._fault("after_preparation_result")
        current = self.ledger.dispatch(str(dispatch["id"]))
        token = str(current["claim_token"])
        if preparation.disposition == "busy":
            delay = max(1, int(preparation.retry_after_seconds or 1))
            available_at = (
                parse_timestamp(self.ledger.clock()) + timedelta(seconds=delay)
            ).isoformat()
            self.ledger.defer_dispatch(
                str(dispatch["id"]), claim_token=token,
                available_at=available_at,
                error=dict(preparation.error or {"message": "preparation is busy"}),
            )
            return self._emit(SchedulerTick(
                "busy", dispatch_id=str(dispatch["id"]),
                execution_id=str(dispatch["execution_id"]),
                attempt_id=str(dispatch["attempt_id"]),
                detail={"available_at": available_at},
            ))
        if preparation.disposition == "needs_attention":
            if not preparation.attention:
                raise PreparationError("preparation attention result has no request")
            self.ledger.mark_dispatch_attention(
                str(dispatch["id"]), claim_token=token,
                attention_id=str(preparation.attention["id"]),
                error={"message": "resource preparation requires attention",
                       "category": "preparation", "resume_phase": "preparing"},
            )
            return self._emit(SchedulerTick(
                "needs_attention", dispatch_id=str(dispatch["id"]),
                execution_id=str(dispatch["execution_id"]),
                attempt_id=str(dispatch["attempt_id"]),
                detail={"attention_id": preparation.attention["id"]},
            ))
        if preparation.disposition == "fatal":
            message = str((preparation.error or {}).get(
                "message", "resource preparation failed"
            ))
            return self._attention(
                current, category="preparation-fatal", message=message,
                resume_phase="preparing",
            )
        if preparation.disposition != "ready" or not preparation.launch:
            raise PreparationError("preparation returned an invalid disposition")
        launch = preparation.launch
        if not isinstance(launch, PreparedLaunch):
            raise PreparationError("preparation did not return PreparedLaunch")
        current = self.ledger.mark_dispatch_prepared(
            str(dispatch["id"]), claim_token=token,
            preparation_id=launch.preparation_id,
            preparation_digest=launch.preparation_digest,
            claim_ttl_seconds=self.policy.claim_ttl_seconds,
        )
        self._fault("after_dispatch_prepared")
        self.ledger.assert_dispatch(
            str(dispatch["id"]), claim_token=str(current["claim_token"]),
            statuses=("prepared",),
        )
        current = self.ledger.mark_dispatching(
            str(dispatch["id"]), claim_token=str(current["claim_token"])
        )
        self._fault("after_dispatch_intent")
        try:
            result = self.dispatch_prepared(launch)
        except Exception:
            runner_run = self.ledger.runner_run_for_attempt(
                str(dispatch["attempt_id"])
            )
            if not runner_run or runner_run["status"] not in ("failed", "canceled"):
                raise
            result = RunnerResult(
                outcome="failed", preferred_label="failed",
                evidence=({
                    "kind": "runner_error",
                    "uri": f"ledger://runner-runs/{runner_run['id']}",
                },),
            )
        self._fault("after_runner_result")
        result_payload = {
            "outcome": result.outcome,
            "preferred_label": result.preferred_label,
            "evidence": list(result.evidence),
        }
        current = self.ledger.record_dispatch_result(
            str(dispatch["id"]), claim_token=str(current["claim_token"]),
            result=result_payload,
        )
        self._fault("after_result_recorded")
        return self._commit_result(
            current, project, launch, result, recovered=recovered,
        )

    def reconcile(self) -> SchedulerTick | None:
        project_keys = tuple(sorted(self.projects))
        resumable = self.ledger.resumable_dispatches(project_keys=project_keys)
        if resumable:
            dispatch = resumable[0]
            self._project(dispatch)
            try:
                resumed = self.ledger.resume_attention_dispatch(
                    str(dispatch["id"]), scheduler_owner=self.owner,
                    project_keys=project_keys,
                )
            except StaleAttempt:
                try:
                    self.ledger.supersede_dispatch(
                        str(dispatch["id"]),
                        claim_token=str(dispatch["claim_token"]),
                        reason="active attempt changed before attention replay",
                    )
                except StaleAttempt:
                    pass
                return self._emit(SchedulerTick(
                    "superseded", dispatch_id=str(dispatch["id"]),
                    execution_id=str(dispatch["execution_id"]),
                    attempt_id=str(dispatch["attempt_id"]),
                ))
            return self._emit(SchedulerTick(
                "resumed", dispatch_id=str(resumed["id"]),
                execution_id=str(resumed["execution_id"]),
                attempt_id=str(resumed["attempt_id"]),
                detail={"phase": resumed["status"]},
            ))
        for dispatch in self.ledger.recoverable_dispatches(
            scheduler_owner=self.owner, project_keys=project_keys,
        ):
            command_key = (
                f"execution:{dispatch['execution_id']}:transition:"
                f"scheduler:{dispatch['id']}:complete"
            )
            recovery_category = "scheduler-recovery"
            try:
                project = self._project(dispatch)
                if dispatch["status"] == "dispatching":
                    return self._attention(
                        dispatch, category="ambiguous-dispatch",
                        message="runner launch intent exists without a durable result",
                        resume_phase="dispatching",
                    )
                if dispatch["status"] == "preparing":
                    preparation = self.ledger.preparation_for_attempt(
                        str(dispatch["attempt_id"])
                    )
                    if not preparation or preparation["status"] == "preparing":
                        return self._attention(
                            dispatch, category="ambiguous-preparation",
                            message="preparation stopped before a durable disposition",
                            resume_phase="preparing" if preparation else "claimed",
                        )
                    recovered_dispatch = self.ledger.takeover_preparing_dispatch(
                        str(dispatch["id"]), scheduler_owner=self.owner,
                    )
                    request = self._request(recovered_dispatch, project)
                    return self._handle_preparation(
                        recovered_dispatch, project, request, recovered=True,
                    )
                if self.ledger.decision_for_command(command_key):
                    completed = self.ledger.complete_dispatch(
                        str(dispatch["id"]), claim_token=str(dispatch["claim_token"])
                    )
                    return self._emit(SchedulerTick(
                        "recovered", dispatch_id=str(completed["id"]),
                        execution_id=str(completed["execution_id"]),
                        attempt_id=str(completed["attempt_id"]),
                        detail={"phase": "workflow_committed"},
                    ))
                recovered_dispatch = self.ledger.takeover_result_dispatch(
                    str(dispatch["id"]), scheduler_owner=self.owner,
                )
                request = self._request(recovered_dispatch, project)
                recovery_category = "result-recovery"
                preparation = project.preparation.prepare(request)
                if preparation.disposition != "ready" or not preparation.launch:
                    return self._attention(
                        recovered_dispatch, category="result-recovery",
                        message="stored result could not rehydrate its prepared launch",
                        resume_phase="result_ready",
                    )
                payload = recovered_dispatch["result"]
                if not isinstance(payload, dict):
                    raise LedgerError("stored scheduler result is missing")
                result = RunnerResult(
                    outcome=str(payload["outcome"]),
                    preferred_label=str(payload["preferred_label"]),
                    evidence=tuple(dict(item) for item in payload["evidence"]),
                )
                recovery_category = "result-commit"
                return self._commit_result(
                    recovered_dispatch, project, preparation.launch, result,
                    recovered=True,
                )
            except StaleAttempt:
                current = self.ledger.dispatch(str(dispatch["id"]))
                try:
                    self.ledger.supersede_dispatch(
                        str(dispatch["id"]),
                        claim_token=str(current["claim_token"]),
                        reason="active attempt changed during scheduler recovery",
                    )
                except StaleAttempt:
                    pass
                return self._emit(SchedulerTick(
                    "superseded", dispatch_id=str(dispatch["id"]),
                    execution_id=str(dispatch["execution_id"]),
                    attempt_id=str(dispatch["attempt_id"]),
                ))
            except Exception as error:
                current = self.ledger.dispatch(str(dispatch["id"]))
                if (
                    current["status"] == "result_ready"
                    and self.ledger.decision_for_command(command_key)
                ):
                    completed = self.ledger.complete_dispatch(
                        str(current["id"]),
                        claim_token=str(current["claim_token"]),
                    )
                    return self._emit(SchedulerTick(
                        "recovered", dispatch_id=str(completed["id"]),
                        execution_id=str(completed["execution_id"]),
                        attempt_id=str(completed["attempt_id"]),
                        detail={"phase": "workflow_committed"},
                    ))
                phase = str(current["status"])
                if phase == "dispatching":
                    category = "ambiguous-dispatch"
                elif phase == "result_ready":
                    category = recovery_category
                else:
                    category = "scheduler-recovery"
                resume_phase = (
                    phase if phase in (
                        "claimed", "preparing", "prepared", "dispatching",
                        "result_ready",
                    ) else "claimed"
                )
                return self._attention(
                    current, category=category, message=str(error),
                    resume_phase=resume_phase,
                )
        return None

    def tick(self) -> SchedulerTick:
        recovered = self.reconcile()
        if recovered:
            return recovered
        claim = self.ledger.claim_dispatch(
            scheduler_owner=self.owner,
            claim_ttl_seconds=self.policy.claim_ttl_seconds,
            limits=self.policy.as_limits(),
            project_keys=tuple(sorted(self.projects)),
        )
        if claim["disposition"] != "claimed":
            return self._emit(SchedulerTick(
                str(claim["disposition"]), detail={"blocked": claim.get("blocked", [])},
            ))
        dispatch = claim["dispatch"]
        self._fault("after_dispatch_claimed")
        try:
            project = self._project(dispatch)
            request = self._request(dispatch, project)
            dispatch = self.ledger.mark_dispatch_preparing(
                str(dispatch["id"]), claim_token=str(dispatch["claim_token"])
            )
            self._fault("after_dispatch_preparing")
            return self._handle_preparation(
                dispatch, project, request, recovered=False,
            )
        except RunnerNeedsAttention as error:
            current = self.ledger.dispatch(str(dispatch["id"]))
            self.ledger.mark_dispatch_attention(
                str(dispatch["id"]), claim_token=str(current["claim_token"]),
                attention_id=error.attention_id,
                error={"message": str(error), "category": "runner-input",
                       "runner_run_id": error.runner_run_id,
                       "resume_phase": error.resume_phase},
            )
            return self._emit(SchedulerTick(
                "needs_attention", dispatch_id=str(dispatch["id"]),
                execution_id=str(dispatch["execution_id"]),
                attempt_id=str(dispatch["attempt_id"]),
                detail={"attention_id": error.attention_id,
                        "runner_run_id": error.runner_run_id},
            ))
        except StaleAttempt:
            try:
                self.ledger.supersede_dispatch(
                    str(dispatch["id"]), claim_token=str(dispatch["claim_token"]),
                    reason="active attempt changed before dispatch",
                )
            except StaleAttempt:
                pass
            return self._emit(SchedulerTick(
                "superseded", dispatch_id=str(dispatch["id"]),
                execution_id=str(dispatch["execution_id"]),
                attempt_id=str(dispatch["attempt_id"]),
            ))
        except Exception as error:
            current = self.ledger.dispatch(str(dispatch["id"]))
            phase = str(current["status"])
            if phase == "dispatching":
                return self._attention(
                    current, category="ambiguous-dispatch", message=str(error),
                    resume_phase="dispatching",
                )
            if phase == "result_ready":
                return self._attention(
                    current, category="result-commit", message=str(error),
                    resume_phase="result_ready",
                )
            return self._attention(
                current, category="scheduler-infrastructure", message=str(error),
                resume_phase=phase if phase in ("claimed", "preparing", "prepared")
                else "claimed",
            )

    def heartbeat(self, dispatch_id: str, *, claim_token: str, command_id: str) -> None:
        self.ledger.heartbeat_dispatch(
            dispatch_id, claim_token=claim_token,
            claim_ttl_seconds=self.policy.claim_ttl_seconds,
            command_id=command_id,
        )
