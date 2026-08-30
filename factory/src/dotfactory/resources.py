"""Resource preparation boundary between durable attempts and live runners."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Protocol

from .kernel import DurableKernel
from .ledger import LedgerError, ResourceBusy, SQLiteLedger, parse_timestamp
from .runner import RunnerRequest, RunnerResult
from .workspace import (
    GitWorkspaceProvider, WorkspaceConflict, WorkspaceHandle, WorkspaceUnsafeCleanup,
)


class PreparationError(RuntimeError):
    pass


class PreparationBusy(PreparationError):
    def __init__(self, message: str, *, retry_after_seconds: int = 5) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PreparationNeedsAttention(PreparationError):
    def __init__(
        self, message: str, *, category: str, detail: dict[str, Any],
        capability: str | None = None, provider: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.detail = detail
        self.capability = capability
        self.provider = provider


@dataclass(frozen=True)
class CapabilityPlan:
    provider: str
    capability: str
    scope: str
    resource_id: str
    target: str
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderActivation:
    resource_id: str
    environment: tuple[tuple[str, str], ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    urls: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    handle: Any = None


class ResourceProvider(Protocol):
    def plan(
        self, *, capability: str, config: Mapping[str, Any],
        workspace: WorkspaceHandle,
    ) -> CapabilityPlan: ...

    def activate(
        self, plan: CapabilityPlan, *, workspace: WorkspaceHandle,
        owner_token: str,
    ) -> ProviderActivation: ...

    def reconcile(
        self, allocation: Mapping[str, Any], *, workspace: WorkspaceHandle,
        owner_token: str,
    ) -> ProviderActivation: ...

    def cleanup(
        self, activation: ProviderActivation, *, owner_token: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class PreparedLaunch:
    request: RunnerRequest
    preparation_id: str
    preparation_digest: str
    workspace_path: str
    branch_name: str
    environment: tuple[tuple[str, str], ...]
    commands: tuple[tuple[str, ...], ...]
    urls: tuple[str, ...]
    allocation_ids: tuple[str, ...]

    def environment_dict(self) -> dict[str, str]:
        return dict(self.environment)


@dataclass(frozen=True)
class PreparationResult:
    disposition: str
    launch: PreparedLaunch | None = None
    retry_after_seconds: int | None = None
    attention: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None


class PreparedRunner(Protocol):
    def run(self, launch: PreparedLaunch) -> RunnerResult: ...


class FakePreparedRunner:
    def __init__(self, results: list[RunnerResult]) -> None:
        self.results = list(results)
        self.launches: list[PreparedLaunch] = []

    def run(self, launch: PreparedLaunch) -> RunnerResult:
        if not isinstance(launch, PreparedLaunch):
            raise PreparationError("runner requires PreparedLaunch")
        self.launches.append(launch)
        if not self.results:
            raise PreparationError("fake prepared runner has no result")
        return self.results.pop(0)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _workspace_handle(record: Mapping[str, Any]) -> WorkspaceHandle:
    return WorkspaceHandle(
        repository_path=str(record["repository_path"]),
        git_common_dir=str(record["git_common_dir"]), remote=str(record["remote"]),
        base_ref=str(record["base_ref"]), base_sha=str(record["base_sha"]),
        branch_name=str(record["branch_name"]), path=str(record["path"]),
    )


class PreparationEngine:
    def __init__(
        self, ledger: SQLiteLedger, *, workspace_provider: GitWorkspaceProvider,
        providers: Mapping[str, ResourceProvider], owner_token: str,
        fault_hook: Any | None = None,
    ) -> None:
        self.ledger = ledger
        self.workspace_provider = workspace_provider
        self.providers = dict(providers)
        self.owner_token = owner_token
        self.fault_hook = fault_hook
        self._activations: dict[str, ProviderActivation] = {}

    def _fault(self, boundary: str) -> None:
        if self.fault_hook:
            self.fault_hook(boundary)

    def _retry_policy(self, config: Mapping[str, Any]) -> dict[str, int]:
        configured = config.get("retry", {})
        if not isinstance(configured, Mapping):
            configured = {}
        return {
            "initial_seconds": int(configured.get("initial_seconds", 5)),
            "maximum_seconds": int(configured.get("maximum_seconds", 60)),
            "deadline_seconds": int(configured.get("deadline_seconds", 900)),
        }

    def _waiting_retry(
        self, request: RunnerRequest, preparation: Mapping[str, Any],
        preparation_config: Mapping[str, Any],
    ) -> tuple[PreparationResult | None, int]:
        if preparation["status"] != "busy":
            return None, 0
        error = preparation.get("error") or {}
        retry_count = int(error.get("retry_count", 0))
        policy = self._retry_policy(preparation_config)
        now = parse_timestamp(self.ledger.clock())
        deadline_value = error.get("deadline_at")
        deadline = (
            parse_timestamp(str(deadline_value)) if deadline_value
            else parse_timestamp(str(preparation["created_at"]))
            + timedelta(seconds=policy["deadline_seconds"])
        )
        if now >= deadline:
            attention_error = PreparationNeedsAttention(
                "resource preparation deadline was exhausted", category="exhausted",
                detail={
                    "last_safe_step": "contention wait",
                    "retry_count": retry_count,
                    "deadline_at": deadline.isoformat(),
                    "allowed_actions": ["retry", "cancel"],
                },
            )
            return self._attention(request, preparation, attention_error), retry_count
        next_value = error.get("next_retry_at")
        if next_value:
            next_retry = parse_timestamp(str(next_value))
            if now < next_retry:
                return PreparationResult(
                    "busy",
                    retry_after_seconds=max(1, math.ceil((next_retry - now).total_seconds())),
                    error={"message": str(error.get("message", "resource is busy"))},
                ), retry_count
        return None, retry_count

    def _record_busy(
        self, request: RunnerRequest, preparation: Mapping[str, Any],
        preparation_config: Mapping[str, Any], error: PreparationBusy,
        retry_count: int,
    ) -> PreparationResult:
        policy = self._retry_policy(preparation_config)
        now = parse_timestamp(self.ledger.clock())
        deadline = parse_timestamp(str(preparation["created_at"])) + timedelta(
            seconds=policy["deadline_seconds"]
        )
        delay = min(
            policy["maximum_seconds"],
            max(
                policy["initial_seconds"] * (2 ** retry_count),
                error.retry_after_seconds,
            ),
        )
        if now + timedelta(seconds=delay) >= deadline:
            attention_error = PreparationNeedsAttention(
                "resource preparation deadline was exhausted", category="exhausted",
                detail={
                    "last_safe_step": "contention wait",
                    "retry_count": retry_count + 1,
                    "deadline_at": deadline.isoformat(),
                    "allowed_actions": ["retry", "cancel"],
                },
            )
            return self._attention(request, preparation, attention_error)
        self.ledger.fail_preparation(
            str(preparation["id"]), fence_token=request.fence_token, status="busy",
            error={
                "message": str(error), "retry_count": retry_count + 1,
                "retry_after_seconds": delay,
                "next_retry_at": (now + timedelta(seconds=delay)).isoformat(),
                "deadline_at": deadline.isoformat(),
            },
        )
        return PreparationResult(
            "busy", retry_after_seconds=delay, error={"message": str(error)},
        )

    def _workspace(
        self, request: RunnerRequest, project: Mapping[str, Any],
        preparation_config: Mapping[str, Any], preparation_id: str,
    ) -> WorkspaceHandle:
        recorded = self.ledger.workspace_for_execution(request.execution_id)
        if recorded:
            if recorded["owner_token"] != self.owner_token:
                raise PreparationNeedsAttention(
                    "workspace is differently owned", category="unsafe-cleanup",
                    detail={"last_safe_step": "workspace lookup",
                            "allowed_actions": ["retain", "quarantine", "cancel"]},
                )
            handle = _workspace_handle(recorded)
            try:
                reconciled = self.workspace_provider.reconcile(handle)
                self._fault("after_workspace_reconciled")
                return reconciled
            except WorkspaceConflict as error:
                raise PreparationNeedsAttention(
                    str(error), category="unsafe-cleanup",
                    detail={"last_safe_step": "workspace reconciliation",
                            "allowed_actions": ["retain", "quarantine", "cancel"]},
                ) from error
        workspace = preparation_config.get("workspace")
        if not isinstance(workspace, Mapping):
            raise PreparationError("repository-backed work requires workspace configuration")
        attempt = self.ledger.current(request.execution_id)
        prior = self.ledger.preparation(preparation_id)["mutations"]
        step = f"workspace-create-{1 + sum(item['provider'] == 'git-worktree' for item in prior)}"
        target = f"{attempt['work_item_identifier']}-{attempt['execution_number']}"
        mutation = self.ledger.plan_mutation(
            preparation_id, fence_token=request.fence_token, provider="git-worktree",
            step_key=step, action="create", target=target,
            intent={"remote": workspace["remote"], "base_ref": workspace["base_ref"]},
        )
        self.ledger.start_mutation(mutation["id"], fence_token=request.fence_token)
        try:
            handle = self.workspace_provider.materialize(
                repository_path=str(project["repository_path"]),
                root=str(workspace["root"]), remote=str(workspace["remote"]),
                base_ref=str(workspace["base_ref"]),
                issue_identifier=str(attempt["work_item_identifier"]),
                execution_number=int(attempt["execution_number"]),
            )
            self.ledger.register_workspace(
                execution_id=request.execution_id, owner_token=self.owner_token,
                repository_path=handle.repository_path,
                git_common_dir=handle.git_common_dir, remote=handle.remote,
                base_ref=handle.base_ref, base_sha=handle.base_sha,
                branch_name=handle.branch_name, path=handle.path,
                metadata={"preparation_id": preparation_id},
            )
            self.ledger.finish_mutation(
                mutation["id"], fence_token=request.fence_token, status="completed",
                result={"base_sha": handle.base_sha, "branch_name": handle.branch_name},
            )
            self._fault("after_workspace_ready")
            return handle
        except WorkspaceConflict as error:
            self.ledger.finish_mutation(
                mutation["id"], fence_token=request.fence_token, status="failed",
                error={"category": "conflict", "message": str(error)},
            )
            raise PreparationNeedsAttention(
                str(error), category="conflict",
                detail={"last_safe_step": "workspace create",
                        "allowed_actions": ["retry", "retain", "quarantine", "cancel"]},
            ) from error
        except Exception as error:
            self.ledger.finish_mutation(
                mutation["id"], fence_token=request.fence_token, status="failed",
                error={"category": "unavailable", "message": str(error)},
            )
            raise

    def _requested_capabilities(
        self, request: RunnerRequest, preparation_config: Mapping[str, Any]
    ) -> list[tuple[str, Mapping[str, Any]]]:
        requested = request.config.get("resources", [])
        if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested):
            raise PreparationError("resolved resources must be an array of capability names")
        capabilities = preparation_config.get("capabilities", {})
        if not isinstance(capabilities, Mapping):
            raise PreparationError("capability configuration is invalid")
        unknown = sorted(set(requested) - set(capabilities))
        if unknown:
            raise PreparationError("unknown resource capabilities: " + ", ".join(unknown))
        return [(name, capabilities[name]) for name in sorted(set(requested))]

    def _activation_from_allocation(
        self, allocation: Mapping[str, Any], workspace: WorkspaceHandle,
    ) -> ProviderActivation:
        allocation_id = str(allocation["id"])
        if allocation_id in self._activations:
            return self._activations[allocation_id]
        provider_name = str(allocation["provider"])
        provider = self.providers.get(provider_name)
        if not provider:
            raise PreparationError(f"provider is unavailable: {provider_name}")
        activation = provider.reconcile(
            allocation, workspace=workspace, owner_token=self.owner_token,
        )
        self._activations[allocation_id] = activation
        return activation

    def _launch(
        self, request: RunnerRequest, preparation: Mapping[str, Any],
        workspace: WorkspaceHandle,
    ) -> PreparedLaunch:
        environment: list[tuple[str, str]] = []
        commands: list[tuple[str, ...]] = []
        urls: list[str] = []
        allocation_ids: list[str] = []
        for allocation in preparation["allocations"]:
            if allocation["status"] != "active":
                continue
            activation = self._activation_from_allocation(allocation, workspace)
            environment.extend(activation.environment)
            commands.extend(activation.commands)
            urls.extend(activation.urls)
            allocation_ids.append(str(allocation["id"]))
        digest = str(preparation["result_digest"])
        self.ledger.assert_attempt_active(request.attempt_id, request.fence_token)
        return PreparedLaunch(
            request=request, preparation_id=str(preparation["id"]),
            preparation_digest=digest, workspace_path=workspace.path,
            branch_name=workspace.branch_name,
            environment=tuple(sorted(environment)), commands=tuple(commands),
            urls=tuple(sorted(urls)), allocation_ids=tuple(allocation_ids),
        )

    def prepare(
        self, request: RunnerRequest, *, project: Mapping[str, Any],
        preparation_config: Mapping[str, Any],
    ) -> PreparationResult:
        request_digest = _digest({
            "execution_id": request.execution_id, "attempt_id": request.attempt_id,
            "fence_token": request.fence_token, "state_id": request.state_id,
            "workflow_digest": request.workflow_digest, "owner": request.owner,
            "config": request.config, "preparation": preparation_config,
        })
        preparation = self.ledger.begin_preparation(
            attempt_id=request.attempt_id, fence_token=request.fence_token,
            request_digest=request_digest,
        )
        if preparation["status"] == "ready":
            try:
                workspace = self._workspace(
                    request, project, preparation_config, str(preparation["id"]),
                )
                return PreparationResult(
                    "ready", launch=self._launch(request, preparation, workspace),
                )
            except PreparationNeedsAttention as error:
                return self._attention(request, preparation, error)
        if preparation["status"] == "canceled":
            return PreparationResult(
                "fatal", error={"message": "resource preparation was canceled"},
            )
        waiting, retry_count = self._waiting_retry(
            request, preparation, preparation_config,
        )
        if waiting:
            return waiting
        preparation = self.ledger.resume_preparation(
            str(preparation["id"]), fence_token=request.fence_token,
        )
        activated: list[tuple[dict[str, Any], ProviderActivation]] = []
        try:
            workspace = self._workspace(
                request, project, preparation_config, str(preparation["id"]),
            )
            self._fault("after_workspace_prepared")
            for capability, capability_config in self._requested_capabilities(
                request, preparation_config
            ):
                provider_name = str(capability_config["provider"])
                provider = self.providers.get(provider_name)
                if not provider:
                    raise PreparationError(f"provider is unavailable: {provider_name}")
                plan = provider.plan(
                    capability=capability,
                    config=dict(capability_config.get("config", {})),
                    workspace=workspace,
                )
                existing = next((
                    item for item in self.ledger.preparation(
                        str(preparation["id"])
                    )["allocations"]
                    if item["status"] == "active"
                    and item["provider"] == provider_name
                    and item["capability"] == capability
                    and item["resource_id"] == plan.resource_id
                ), None)
                if existing:
                    mutation = next((
                        item for item in reversed(self.ledger.preparation(
                            str(preparation["id"])
                        )["mutations"])
                        if item.get("allocation_id") == existing["id"]
                        and item["action"] == "activate"
                    ), None)
                    if not mutation or mutation["status"] in ("planned", "failed"):
                        self.ledger.release_allocation(
                            str(existing["id"]), fence_token=request.fence_token,
                            result={"recovery": "no external activation recorded"},
                        )
                    else:
                        activation = self._activation_from_allocation(existing, workspace)
                        if mutation["status"] == "started":
                            self.ledger.finish_mutation(
                                str(mutation["id"]), fence_token=request.fence_token,
                                status="completed", result={"recovered": True},
                            )
                        activated.append((existing, activation))
                        self._fault("after_allocation_reconciled")
                        continue
                try:
                    allocation = self.ledger.acquire_allocation(
                        str(preparation["id"]), fence_token=request.fence_token,
                        scope=str(capability_config["scope"]), provider=provider_name,
                        capability=capability, resource_id=plan.resource_id,
                        metadata={"target": plan.target},
                    )
                except ResourceBusy as error:
                    raise PreparationBusy(str(error)) from error
                self._fault("after_allocation_acquired")
                prior = self.ledger.preparation(str(preparation["id"]))["mutations"]
                step = f"activate-{capability}-{1 + sum(item['provider'] == provider_name for item in prior)}"
                mutation = self.ledger.plan_mutation(
                    str(preparation["id"]), fence_token=request.fence_token,
                    provider=provider_name, step_key=step, action="activate",
                    target=plan.target, intent={"capability": capability},
                    allocation_id=str(allocation["id"]),
                )
                self.ledger.start_mutation(mutation["id"], fence_token=request.fence_token)
                try:
                    activation = provider.activate(
                        plan, workspace=workspace, owner_token=self.owner_token,
                    )
                    self._fault("after_provider_activated")
                except Exception as error:
                    self.ledger.finish_mutation(
                        mutation["id"], fence_token=request.fence_token,
                        status="failed", error={"message": str(error)},
                    )
                    self.ledger.release_allocation(
                        str(allocation["id"]), fence_token=request.fence_token,
                        result={"activation": "failed before ready"},
                    )
                    raise
                self._activations[str(allocation["id"])] = activation
                activated.append((allocation, activation))
                self.ledger.record_allocation_ready(
                    str(allocation["id"]), fence_token=request.fence_token,
                    metadata={**dict(activation.metadata),
                              "urls": list(activation.urls),
                              "environment_names": [
                                  key for key, _ in activation.environment
                              ]},
                )
                self._fault("after_allocation_ready")
                self.ledger.finish_mutation(
                    mutation["id"], fence_token=request.fence_token, status="completed",
                    result={"resource_id": activation.resource_id,
                            "urls": list(activation.urls),
                            "environment_names": [key for key, _ in activation.environment]},
                )
            prepared_view = {
                "workspace_branch": workspace.branch_name,
                "environment_names": sorted(
                    key for _, activation in activated for key, _ in activation.environment
                ),
                "urls": sorted(url for _, activation in activated for url in activation.urls),
                "allocation_ids": [str(item[0]["id"]) for item in activated],
            }
            result_digest = _digest(prepared_view)
            preparation = self.ledger.mark_preparation_ready(
                str(preparation["id"]), fence_token=request.fence_token,
                result_digest=result_digest, prepared=prepared_view,
            )
            self._fault("after_preparation_committed")
            return PreparationResult(
                "ready", launch=self._launch(request, preparation, workspace),
            )
        except PreparationBusy as error:
            self._compensate(request, preparation, activated)
            return self._record_busy(
                request, preparation, preparation_config, error, retry_count,
            )
        except PreparationNeedsAttention as error:
            self._compensate(request, preparation, activated)
            return self._attention(request, preparation, error)
        except Exception as error:
            self._compensate(request, preparation, activated)
            self.ledger.fail_preparation(
                str(preparation["id"]), fence_token=request.fence_token,
                status="failed", error={"message": str(error)},
            )
            return PreparationResult("fatal", error={"message": str(error)})

    def _compensate(
        self, request: RunnerRequest, preparation: Mapping[str, Any],
        activated: list[tuple[dict[str, Any], ProviderActivation]],
    ) -> None:
        for allocation, activation in reversed(activated):
            provider_name = str(allocation["provider"])
            provider = self.providers[provider_name]
            step = f"compensate-{allocation['id']}"
            mutation = self.ledger.plan_mutation(
                str(preparation["id"]), fence_token=request.fence_token,
                provider=provider_name, step_key=step, action="cleanup",
                target=str(allocation["resource_id"]), intent={"owned": True},
                allocation_id=str(allocation["id"]),
            )
            self.ledger.start_mutation(mutation["id"], fence_token=request.fence_token)
            try:
                result = provider.cleanup(activation, owner_token=self.owner_token)
                self.ledger.finish_mutation(
                    mutation["id"], fence_token=request.fence_token,
                    status="completed", result=dict(result),
                )
                self.ledger.release_allocation(
                    str(allocation["id"]), fence_token=request.fence_token,
                    result=dict(result),
                )
            except Exception as error:
                self.ledger.finish_mutation(
                    mutation["id"], fence_token=request.fence_token,
                    status="quarantined", error={"message": str(error)},
                )
                raise PreparationNeedsAttention(
                    str(error), category="unsafe-cleanup",
                    detail={"last_safe_step": "compensation",
                            "allowed_actions": ["retry", "retain", "quarantine"]},
                    capability=str(allocation["capability"]), provider=provider_name,
                ) from error

    def _attention(
        self, request: RunnerRequest, preparation: Mapping[str, Any],
        error: PreparationNeedsAttention,
    ) -> PreparationResult:
        attention = self.ledger.open_attention(
            execution_id=request.execution_id, attempt_id=request.attempt_id,
            preparation_id=str(preparation["id"]),
            dedupe_key=f"preparation:{preparation['id']}:{error.category}:"
                       f"{error.provider or 'workspace'}:{error.capability or 'workspace'}",
            category=error.category, capability=error.capability,
            provider=error.provider, detail=error.detail,
        )
        self.ledger.fail_preparation(
            str(preparation["id"]), fence_token=request.fence_token,
            status="needs_attention", error={"message": str(error),
                                             "attention_id": attention["id"]},
        )
        return PreparationResult("needs_attention", attention=attention)

    def cleanup_attempt(self, launch: PreparedLaunch) -> PreparationResult:
        preparation = self.ledger.preparation(launch.preparation_id)
        workspace_record = self.ledger.workspace_for_execution(launch.request.execution_id)
        if not workspace_record:
            return PreparationResult("fatal", error={"message": "workspace not found"})
        workspace = _workspace_handle(workspace_record)
        cleanup_plan = self.ledger.begin_cleanup_plan(
            execution_id=launch.request.execution_id,
            attempt_id=launch.request.attempt_id,
            fence_token=launch.request.fence_token,
            plan={"scope": "attempt", "allocation_ids": list(launch.allocation_ids)},
        )
        self._fault("after_cleanup_planned")
        for allocation in reversed(preparation["allocations"]):
            if allocation["status"] != "active" or allocation["scope"] != "attempt":
                continue
            activation = self._activation_from_allocation(allocation, workspace)
            provider = self.providers[str(allocation["provider"])]
            try:
                result = provider.cleanup(activation, owner_token=self.owner_token)
                self._fault("after_provider_cleanup")
                self.ledger.release_allocation(
                    str(allocation["id"]), fence_token=launch.request.fence_token,
                    result=dict(result),
                )
                self._fault("after_allocation_released")
            except Exception as error:
                self.ledger.finish_cleanup_plan(
                    str(cleanup_plan["id"]), status="quarantined",
                    result={"message": str(error),
                            "allocation_id": str(allocation["id"])},
                )
                attention_error = PreparationNeedsAttention(
                    str(error), category="unsafe-cleanup",
                    detail={"last_safe_step": "attempt cleanup",
                            "allowed_actions": ["retry", "retain", "quarantine"]},
                    capability=str(allocation["capability"]),
                    provider=str(allocation["provider"]),
                )
                return self._attention(launch.request, preparation, attention_error)
        self.ledger.finish_cleanup_plan(
            str(cleanup_plan["id"]), status="completed",
            result={"released_allocation_ids": list(launch.allocation_ids)},
        )
        self._fault("after_cleanup_finished")
        return PreparationResult("ready", launch=launch)

    def cleanup_workspace(self, execution_id: str) -> PreparationResult:
        record = self.ledger.workspace_for_execution(execution_id)
        if not record:
            return PreparationResult("ready")
        if record["owner_token"] != self.owner_token:
            return PreparationResult(
                "needs_attention", error={"message": "workspace is differently owned"},
            )
        if record["status"] == "cleaned":
            return PreparationResult("ready")
        cleanup_plan = self.ledger.begin_cleanup_plan(
            execution_id=execution_id, attempt_id=None,
            plan={"scope": "execution", "workspace_id": record["id"],
                  "guard": "clean-and-provenance-matched"},
        )
        try:
            self.ledger.set_workspace_status(
                execution_id, owner_token=self.owner_token, status="cleanup_pending",
                detail={"cleanup_id": cleanup_plan["id"]},
            )
            self.workspace_provider.cleanup(_workspace_handle(record))
            self.ledger.set_workspace_status(
                execution_id, owner_token=self.owner_token, status="cleaned",
                detail={"cleanup_id": cleanup_plan["id"]},
            )
            self.ledger.finish_cleanup_plan(
                str(cleanup_plan["id"]), status="completed",
                result={"workspace_id": record["id"]},
            )
            return PreparationResult("ready")
        except (WorkspaceConflict, WorkspaceUnsafeCleanup) as error:
            self.ledger.set_workspace_status(
                execution_id, owner_token=self.owner_token, status="quarantined",
                detail={"cleanup_id": cleanup_plan["id"], "message": str(error)},
            )
            self.ledger.finish_cleanup_plan(
                str(cleanup_plan["id"]), status="quarantined",
                result={"workspace_id": record["id"], "message": str(error)},
            )
            attention = self.ledger.open_attention(
                execution_id=execution_id, attempt_id=None, preparation_id=None,
                dedupe_key=f"workspace:{record['id']}:unsafe-cleanup",
                category="unsafe-cleanup", provider="git-worktree",
                detail={"last_safe_step": "workspace cleanup",
                        "message": str(error),
                        "allowed_actions": ["retry", "retain", "quarantine"]},
            )
            return PreparationResult(
                "needs_attention", attention=attention,
                error={"message": str(error)},
            )

    def remedy_attention(
        self, execution_id: str, *, attention_id: str, remedy: str,
        command_id: str, expected_attempt_id: str | None,
    ) -> dict[str, Any]:
        if remedy not in ("retry", "release", "retain", "quarantine", "cancel"):
            raise LedgerError("unsupported attention remedy")
        attention = self.ledger.attention(attention_id)
        if attention["execution_id"] != execution_id:
            raise LedgerError("attention request belongs to another execution")
        if attention["status"] != "open":
            raise LedgerError("attention request is already resolved")
        allowed = attention["detail"].get("allowed_actions", [])
        if remedy not in allowed:
            raise LedgerError(f"{remedy} is not allowed for this attention request")
        current = self.ledger.current(execution_id)
        active_attempt = current.get("attempt")
        attention_attempt_id = attention.get("attempt_id")
        if attention_attempt_id:
            if (
                not active_attempt
                or active_attempt["id"] != attention_attempt_id
                or expected_attempt_id != attention_attempt_id
            ):
                raise LedgerError("attention request is stale for the active attempt")
            fence_token = str(active_attempt["fence_token"])
            self.ledger.assert_attempt_active(attention_attempt_id, fence_token)
        else:
            if expected_attempt_id is not None:
                raise LedgerError("execution-scoped attention does not accept an attempt")
            fence_token = None
        preparation = (
            self.ledger.preparation(str(attention["preparation_id"]))
            if attention.get("preparation_id") else None
        )
        if preparation and preparation["fence_token"] != fence_token:
            raise LedgerError("attention preparation fence is stale")
        if remedy == "retry":
            if preparation:
                self.ledger.authorize_preparation_retry(
                    str(preparation["id"]), fence_token=str(fence_token),
                    command_id=command_id,
                )
        elif remedy == "release" and not preparation:
            cleanup = self.cleanup_workspace(execution_id)
            if cleanup.disposition != "ready":
                raise LedgerError("workspace is not safe to release")
        elif remedy in ("release", "cancel") and preparation:
            workspace_record = self.ledger.workspace_for_execution(execution_id)
            if not workspace_record:
                raise LedgerError("workspace not found for resource release")
            workspace = _workspace_handle(workspace_record)
            targets = [
                allocation for allocation in preparation["allocations"]
                if allocation["status"] == "active"
                and (
                    remedy == "cancel"
                    or (
                        (not attention.get("provider")
                         or allocation["provider"] == attention["provider"])
                        and (not attention.get("capability")
                             or allocation["capability"] == attention["capability"])
                    )
                )
            ]
            for allocation in reversed(targets):
                activation = self._activation_from_allocation(allocation, workspace)
                provider = self.providers[str(allocation["provider"])]
                result = provider.cleanup(activation, owner_token=self.owner_token)
                self.ledger.release_allocation(
                    str(allocation["id"]), fence_token=str(fence_token),
                    result={**dict(result), "attention_command_id": command_id},
                )
        elif remedy == "quarantine":
            if preparation:
                targets = [
                    allocation for allocation in preparation["allocations"]
                    if allocation["status"] == "active"
                    and (not attention.get("provider")
                         or allocation["provider"] == attention["provider"])
                    and (not attention.get("capability")
                         or allocation["capability"] == attention["capability"])
                ]
                for allocation in targets:
                    self.ledger.quarantine_allocation(
                        str(allocation["id"]), fence_token=str(fence_token),
                        reason=f"attention:{attention_id}",
                    )
            workspace = self.ledger.workspace_for_execution(execution_id)
            if (
                workspace and attention.get("provider") in (None, "git-worktree")
                and workspace["status"] != "quarantined"
            ):
                self.ledger.set_workspace_status(
                    execution_id, owner_token=self.owner_token, status="quarantined",
                    detail={"attention_id": attention_id, "command_id": command_id},
                )
        if preparation and remedy != "retry":
            self.ledger.cancel_preparation(
                str(preparation["id"]), fence_token=str(fence_token),
                command_id=command_id, reason=f"attention remedy: {remedy}",
            )
        resolved = self.ledger.resolve_attention(
            attention_id,
            resolution="canceled" if remedy == "cancel" else "resolved",
            detail={"remedy": remedy, "command_id": command_id},
        )
        return {
            "attention": resolved,
            "remedy": remedy,
            "preparation": (
                self.ledger.preparation(str(preparation["id"]))
                if preparation else None
            ),
        }


def run_prepared_attempt(
    kernel: DurableKernel, engine: PreparationEngine, launch: PreparedLaunch,
    runner: PreparedRunner, *, command_id: str,
) -> dict[str, Any]:
    if not isinstance(launch, PreparedLaunch):
        raise PreparationError("live runner dispatch requires PreparedLaunch")
    kernel.ledger.assert_attempt_active(
        launch.request.attempt_id, launch.request.fence_token
    )
    result = runner.run(launch)
    cleanup = engine.cleanup_attempt(launch)
    if cleanup.disposition != "ready":
        raise PreparationError("attempt cleanup requires attention before transition")
    return kernel.complete_attempt(
        launch.request.execution_id, preferred_label=result.preferred_label,
        outcome=result.outcome, evidence=list(result.evidence),
        attempt_id=launch.request.attempt_id,
        fence_token=launch.request.fence_token, owner=launch.request.owner,
        command_id=command_id,
    )
