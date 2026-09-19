"""Provider-reported token limits at the single-writer dispatch boundary."""

from __future__ import annotations

import json
from typing import Any

from .token_usage import normalize_token_usage


def validate_budgets(values: Any) -> None:
    if not isinstance(values, dict) or set(values) - {"project_limit", "execution_limit"}:
        raise ValueError("budgets accepts only project_limit and execution_limit")
    for key, value in values.items():
        if type(value) is not int or value < 1:
            raise ValueError(f"budgets.{key} must be a positive integer")


def usage(ledger: Any, project: str, execution: str | None = None) -> dict[str, Any]:
    query = (
        "SELECT rr.* FROM runner_runs rr JOIN workflow_executions we "
        "ON we.id=rr.execution_id JOIN work_items wi ON wi.id=we.work_item_id "
        "WHERE wi.project_key=?"
    )
    parameters = [project]
    if execution:
        query += " AND rr.execution_id=?"
        parameters.append(execution)
    total, measured, unknown = 0, 0, 0
    for run in ledger.connection.execute(query, parameters):
        facts = []
        incomplete = False
        kind = run["adapter_kind"]
        for event in ledger.connection.execute(
            "SELECT protocol_type,payload_json FROM runner_events "
            "WHERE runner_run_id=? ORDER BY seq", (run["id"],),
        ):
            protocol = event["protocol_type"]
            if not ((kind == "codex" and protocol == "turn.completed")
                    or (kind == "claude-code" and protocol == "result")
                    or (kind == "omp-rpc" and protocol == "message_end")):
                continue
            payload = json.loads(event["payload_json"])
            if kind == "omp-rpc" and payload.get("message_role") in ("user", "tool", "toolResult", "system"):
                continue
            count = normalize_token_usage(payload.get("usage"))
            if count and "input_tokens" in count and "output_tokens" in count:
                facts.append(count["total_tokens"])
            else:
                incomplete = True
        # A partial/failed run may have consumed more than it reported. Never
        # pretend that a missing final report or an unknown adapter spent zero.
        complete = (run["status"] == "result_ready" and bool(facts)
                    and not incomplete and not run["dropped_event_count"])
        if kind == "claude-code":
            facts = facts[-1:]  # result is cumulative, not an additional call
        total += sum(facts)
        measured += int(complete)
        unknown += int(not complete)
    return {"used": total, "measured_runs": measured, "unknown_runs": unknown}


def evaluate(ledger: Any, policy: dict[str, Any], project: str,
             execution: str | None = None) -> dict[str, Any]:
    scopes = []
    for scope, identity in (("project", None), ("execution", execution)):
        limit = policy.get(scope + "_limit")
        if limit is None or scope == "execution" and execution is None:
            continue
        measured = usage(ledger, project, identity)
        status = ("usage_unavailable" if measured["unknown_runs"] else
                  "limit_reached" if measured["used"] >= limit else "available")
        scopes.append({"scope": scope, "limit": limit, "status": status, **measured})
    blocked = any(item["status"] != "available" for item in scopes)
    return {
        "status": "blocked" if blocked else "available" if scopes else "unlimited",
        "unit": "provider_reported_tokens", "scopes": scopes,
        "message": (
            "No new dispatch: token limit reached or final usage unavailable. "
            "Inspect status; raise the configured limit only after review. "
            "Missing usage requires accounting repair or explicit removal of the limit."
            if blocked else "Limits gate the next stage, not an in-flight call; no dollar cap."
        ),
    }
