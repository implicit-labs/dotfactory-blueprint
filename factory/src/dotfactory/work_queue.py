"""Explicit admission and recovery-first orchestration on the existing owner."""

from __future__ import annotations

import json
from typing import Any

from .budgets import evaluate
from .linear_api import LinearAPIError


def validate_queue(values: Any) -> None:
    if not isinstance(values, dict) or set(values) - {
        "enabled", "admission_label", "excluded_labels",
    }:
        raise ValueError("work_queue accepts enabled, admission_label, excluded_labels")
    if type(values.get("enabled", False)) is not bool:
        raise ValueError("work_queue.enabled must be boolean")
    label = values.get("admission_label", "factory-ready")
    excluded = values.get("excluded_labels", ["TEST", "demo"])
    if not isinstance(label, str) or not label.strip():
        raise ValueError("work_queue.admission_label must be non-empty text")
    if not isinstance(excluded, list) or any(
        not isinstance(item, str) or not item.strip() for item in excluded
    ):
        raise ValueError("work_queue.excluded_labels must be an array of non-empty labels")
    if label.casefold() in {item.casefold() for item in excluded}:
        raise ValueError("admission label cannot also be excluded")


def rejection(issue: dict[str, Any], policy: dict[str, Any], project: str,
              statuses: list[str], team: str | None = None) -> str | None:
    if not issue.get("identifier") or not issue.get("id"):
        return "invalid_identity"
    description = issue.get("description") or ""
    if not isinstance(description, str) or len(description) > 65536:
        return "invalid_description"
    if (issue.get("project") or {}).get("id") != project:
        return "wrong_project"
    if team and (issue.get("team") or {}).get("id") != team:
        return "wrong_team"
    if (issue.get("state") or {}).get("name") not in statuses:
        return "not_pickup"
    for key in ("labels", "inverseRelations"):
        connection = issue.get(key)
        if (not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list)
                or any(not isinstance(node, dict) for node in connection.get("nodes", []))
                or not isinstance(connection.get("pageInfo"), dict)
                or connection["pageInfo"].get("hasNextPage") is not False):
            return "incomplete_eligibility"
    labels = {str(item.get("name", "")).casefold() for item in issue["labels"]["nodes"]}
    if labels & {item.casefold() for item in policy.get("excluded_labels", ["TEST", "demo"])}:
        return "excluded_label"
    if policy.get("admission_label", "factory-ready").casefold() not in labels:
        return "not_admitted"
    for relation in issue["inverseRelations"]["nodes"]:
        if relation.get("type") == "blocks" and (
            (relation.get("issue") or {}).get("state") or {}
        ).get("type") not in ("completed", "canceled"):
            return "blocked_dependency"
    return None


def priority(issue: dict[str, Any]) -> tuple[Any, ...]:
    value = issue.get("priority")
    return (value if type(value) is int and 1 <= value <= 4 else 5,
            str(issue.get("createdAt", "")), str(issue.get("identifier", "")))


class WorkQueue:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.policy = runtime.config.values.get("work_queue", {})
        if not self.policy.get("enabled", False):
            raise ValueError("work requires work_queue.enabled=true and an explicit admission label")
        if runtime.scheduler.policy.host_limit != 1:
            raise ValueError("work supports one active child; scheduler.limits.host must be 1")
        if any(key not in runtime.linear_workers for key in runtime.project_keys):
            raise ValueError("work requires enabled Linear projection for every selected project")

    def _record(self, project: str, value: dict[str, Any]) -> dict[str, Any]:
        return self.runtime.ledger.record_operating_receipt("queue", project, None, value)

    def _continue_discovery(self) -> bool:
        if self.runtime.operator_server:
            self.runtime.operator_server.pump()
        return not self.runtime.stop_requested and not self.runtime.drain_requested

    def _uncertain_child(self) -> bool:
        runtime = self.runtime
        # Canceling an ambiguous dispatch is not proof that its child exited.
        # Preserve the single-child boundary even after the graph is terminal.
        for row in runtime.ledger.connection.execute(
            "SELECT rr.execution_id,rr.error_json,wi.project_key FROM runner_runs rr "
            "JOIN workflow_executions we ON we.id=rr.execution_id "
            "JOIN work_items wi ON wi.id=we.work_item_id WHERE rr.status='canceled'"
        ):
            # Host capacity is shared across project selections. Selecting a
            # different project must not bypass an unknown surviving child.
            if json.loads(row["error_json"] or "{}").get("ambiguous_side_effect"):
                workspace = runtime.ledger.workspace_for_execution(str(row["execution_id"]))
                if not workspace or workspace["status"] != "cleaned":
                    return True
        return False

    def _pending_work(self) -> bool:
        runtime = self.runtime
        # No pagination cap: old attention or active runs must not disappear as
        # an instance accumulates review checkpoints and completed work.
        for row in runtime.ledger.connection.execute(
            "SELECT we.id,wi.project_key FROM workflow_executions we "
            "JOIN work_items wi ON wi.id=we.work_item_id WHERE we.status='running'"
        ):
            project = str(row["project_key"])
            if project not in runtime.kernels:
                continue
            execution = str(row["id"])
            current = runtime.ledger.current(execution)
            _, states, _ = runtime.kernels[project].graph_for_execution(execution)
            state = states[str(current["current_state_id"])]
            if (current.get("attempt") or state.get("kind") == "work"
                    or state.get("checkpoint_role") == "pickup"
                    or runtime.ledger.pending_transition(execution)
                    or runtime.ledger.run_snapshot(execution)["attention_requests"]):
                return True
        return False

    def step(self) -> dict[str, Any]:
        runtime = self.runtime
        if runtime.operator_server:
            runtime.operator_server.pump()
        if runtime.stop_requested or runtime.drain_requested:
            return {"status": "drained" if runtime.drain_requested else "stopped"}
        if self._uncertain_child():
            runtime._cleanup_terminal_workspaces()
            for project in runtime.project_keys:
                self._record(project, {"status": "needs_attention", "message":
                    "Canceled runner exit is unproven. Inspect it and explicitly release its workspace before continuing."})
            return {"status": "needs_attention"}
        # Always service durable work before fetching candidates. A failing
        # discovery endpoint must not suppress local crash reconciliation.
        try:
            result = runtime.step()
        except LinearAPIError as error:
            recovered = runtime.scheduler.reconcile()
            for project in runtime.project_keys:
                self._record(project, {"status": "tracker_unavailable", "code": error.code})
            return {"status": "tracker_unavailable", "recovery": recovered.disposition if recovered else None}
        if runtime.stop_requested or runtime.drain_requested:
            return {"status": "drained" if runtime.drain_requested else "stopped", "step": result}
        if self._pending_work():
            disposition = result["scheduler"]["disposition"]
            return {"status": disposition if disposition in ("budget_blocked", "needs_attention") else "servicing_existing", "step": result}
        candidates = []
        blocked = []
        for project in runtime.project_keys:
            if not self._continue_discovery():
                break
            budget = evaluate(runtime.ledger, runtime.config.values.get("budgets", {}), project)
            runtime.ledger.record_operating_receipt("budget", project, None, budget)
            if budget["status"] == "blocked":
                self._record(project, {"status": "budget_blocked", "budget": budget})
                blocked.append("budget_blocked")
                continue
            config = runtime.config.resolve_project(project, environment=runtime.environment)
            statuses = sorted({str(state["linear_status"]) for state in runtime.kernels[project].states.values()
                               if state.get("checkpoint_role") == "pickup" and state.get("linear_status")})
            client = runtime.linear_workers[project].client
            try:
                issues = client.queue_issues(project_id=config["tracker_project_id"], status_names=statuses,
                                             should_continue=self._continue_discovery)
            except LinearAPIError as error:
                self._record(project, {"status": "tracker_unavailable", "code": error.code})
                blocked.append("tracker_unavailable")
                continue
            excluded = {}
            eligible_count = 0
            for issue in issues:
                reason = ("already_executed" if runtime._has_execution(project, str(issue.get("identifier", "")))
                          else rejection(issue, self.policy, config["tracker_project_id"], statuses,
                                         config.get("tracker_team_id")))
                if reason is None:
                    candidates.append((priority(issue), project, issue, statuses, config))
                    eligible_count += 1
                else:
                    excluded[reason] = excluded.get(reason, 0) + 1
            self._record(project, {"status": "scanned", "observed_count": len(issues),
                                   "eligible_count": eligible_count, "excluded": excluded})
        for _, project, issue, statuses, config in sorted(candidates, key=lambda item: (item[0], item[1])):
            if runtime.operator_server:
                runtime.operator_server.pump()
            if runtime.stop_requested or runtime.drain_requested:
                break
            client = runtime.linear_workers[project].client
            try:
                fresh = client.queue_issue(str(issue["identifier"]))
                reason = rejection(fresh, self.policy, config["tracker_project_id"], statuses,
                                   config.get("tracker_team_id"))
                if reason:
                    self._record(project, {"status": "admission_changed", "issue": issue["identifier"], "reason": reason})
                    continue
                execution = runtime.start_issue(project, str(fresh["identifier"]), admission_snapshot=fresh)
            except LinearAPIError as error:
                self._record(project, {"status": "tracker_unavailable", "code": error.code})
                blocked.append("tracker_unavailable")
                continue
            receipt = self._record(project, {"status": "admitted", "issue": fresh["identifier"],
                                            "execution_id": execution, "source_revision": fresh.get("updatedAt")})
            return {"status": "admitted", "receipt": receipt, "step": result}
        status = ("drained" if runtime.drain_requested else "stopped" if runtime.stop_requested
                  else blocked[0] if blocked else "idle")
        return {"status": status, "step": result}
