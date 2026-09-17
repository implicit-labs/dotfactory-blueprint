"""Bounded canonical context for the next attempt; never fetch artifact URIs."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def build_handoff(db: sqlite3.Connection, execution_id: str, attempt_id: str) -> dict[str, Any]:
    clipped: list[str] = []

    def bounded(value: Any, path: str) -> Any:
        if isinstance(value, str) and len(value) > 4096:
            clipped.append(path)
            return value[:4096]
        if isinstance(value, dict):
            return {k: bounded(v, f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            return [bounded(v, f"{path}.{i}") for i, v in enumerate(value)]
        return value

    def rows(query: str, params: tuple[Any, ...], label: str) -> list[dict[str, Any]]:
        values = [dict(row) for row in db.execute(query, params)]
        if len(values) > 16:
            clipped.append(label)
        return values[:16]

    outcomes = rows(
        "SELECT a.id,a.outcome,a.status,s.state_id FROM attempts a "
        "JOIN state_runs s ON s.id=a.state_run_id "
        "WHERE s.execution_id=? AND a.id!=? AND a.status!='active' "
        "ORDER BY s.ordinal DESC,a.started_at DESC,a.id DESC LIMIT 17",
        (execution_id, attempt_id), "prior_outcomes",
    )
    artifacts = rows(
        "SELECT id,attempt_id,kind,uri FROM artifacts "
        "WHERE execution_id=? ORDER BY created_at DESC,id DESC LIMIT 17",
        (execution_id,), "artifacts",
    )
    feedback = rows(
        "SELECT id,target_id,source,created_at,body_json FROM feedback WHERE execution_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT 17", (execution_id,), "feedback",
    )
    for item in feedback:
        item["body"] = json.loads(item.pop("body_json"))
        item["target_is_current_attempt"] = item["target_id"] == attempt_id
    errors = rows(
        "SELECT id,attempt_id,error_json FROM runner_runs "
        "WHERE execution_id=? AND error_json IS NOT NULL "
        "ORDER BY created_at DESC,id DESC LIMIT 17", (execution_id,), "failures",
    )
    for error in errors:
        error["error"] = json.loads(error.pop("error_json"))
    context = bounded({
        "schema_version": 1, "attempt_id": attempt_id,
        "prior_outcomes": outcomes, "artifacts": artifacts,
        "feedback": feedback, "failures": errors,
    }, "handoff")
    context["omitted_or_truncated"] = clipped
    context["source_policy"] = "Ledger facts; artifact URIs are references, not fetched content."
    return context
