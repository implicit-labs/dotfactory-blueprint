"""Read-only delivery health; configuration is not proof of remote delivery."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

def _safe_error(value: Any) -> dict[str, str] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        parsed = {}
    code = parsed.get("code", "PROJECTION_DELIVERY_ERROR") if isinstance(parsed, dict) else "PROJECTION_DELIVERY_ERROR"
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
        code = "PROJECTION_DELIVERY_ERROR"
    # Never return transport messages, response bodies, credentials, or URLs.
    return {"code": code, "message": "Delivery needs inspection; inspect the local delivery receipt before retrying."}


def _state(row: dict[str, Any]) -> str:
    status = row["status"]
    if status in ("confirmed", "delivered", "accepted", "active", "superseded"):
        return "confirmed" if status != "superseded" else "superseded"
    if status in ("ambiguous", "sending", "in_flight"):
        return "ambiguous"
    if status in ("failed", "blocked", "rejected"):
        return "failed"
    if status == "fallback":
        return "fallback"
    if status in ("retryable", "retry") or row.get("next_attempt_at"):
        return "retry"
    return "pending"


def _summarize(destination: str, kind: str, enabled: bool | None,
               rows: Any, now: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "destination": destination, "projection_type": kind, "enabled": enabled,
        "count_unit": "source_records", "pending": 0, "retry": 0,
        "ambiguous": 0, "failed": 0, "fallback": 0, "confirmed": 0, "superseded": 0,
        "oldest_pending_at": None, "oldest_pending_age_seconds": None,
        "last_confirmed_receipt": None, "safe_error": None,
    }
    error_at = ""
    for source in rows:
        row = dict(source)
        state = _state(row)
        result[state] += 1
        when = row.get("confirmed_at")
        if when and (not result["last_confirmed_receipt"] or when > result["last_confirmed_receipt"]["confirmed_at"]):
            result["last_confirmed_receipt"] = {"id": row.get("receipt_id") or row["id"], "confirmed_at": when}
        if state not in ("confirmed", "superseded"):
            when = row["created_at"]
            if not result["oldest_pending_at"] or when < result["oldest_pending_at"]:
                result["oldest_pending_at"] = when
            if row.get("error") and row.get("updated_at", when) >= error_at:
                error_at = row.get("updated_at", when)
                result["safe_error"] = _safe_error(row["error"])
    if result["oldest_pending_at"]:
        result["oldest_pending_age_seconds"] = max(0, int((
            datetime.fromisoformat(now.replace("Z", "+00:00")) -
            datetime.fromisoformat(result["oldest_pending_at"].replace("Z", "+00:00"))
        ).total_seconds()))
    result["status"] = (
        "disabled" if enabled is False else "unknown" if enabled is None else
        next((key for key in ("failed", "ambiguous", "retry", "fallback", "pending") if result[key]),
             "healthy" if result["confirmed"] else "idle")
    )
    return result


def projection_health(ledger: Any, execution_id: str | None = None) -> dict[str, Any]:
    db = ledger.connection
    configuration = getattr(ledger, "projection_configuration", {})
    linear = configuration.get("linear")
    logfire = configuration.get("logfire")
    sessions = configuration.get("linear_agent")
    logfire_destination = configuration.get("logfire_destination")
    now = ledger.clock()
    channels = []
    scope = " WHERE execution_id=?" if execution_id else ""
    arguments = (execution_id,) if execution_id else ()
    for table, kind, enabled, identity in (
        ("linear_mutations", "status", linear, "id"),
        ("linear_evidence", "evidence_comment", linear, "execution_id"),
        ("linear_agent_sessions", "agent_session", sessions, "execution_id"),
        ("linear_agent_activities", "agent_activity", sessions, "id"),
    ):
        rows = db.execute(
            f"SELECT {identity} AS id,status,created_at,updated_at,confirmed_at,"
            f"next_attempt_at,last_error_json AS error FROM {table}" + scope, arguments,
        )
        channels.append(_summarize("linear", kind, enabled, rows, now))
    for destination in ("linear", "logfire"):
        rows = db.execute(
            "SELECT o.id,o.status,o.created_at,o.delivered_at AS confirmed_at,"
            "o.last_error AS error FROM outbox o JOIN events e ON e.seq=o.event_seq "
            "WHERE o.destination=?" + (" AND e.execution_id=?" if execution_id else ""),
            (destination,) + arguments,
        )
        channel = _summarize(destination, "legacy_event", False, rows, now)
        channel["disabled_reason"] = "No generic event sender is installed in the composed runtime; dedicated delivery is reported separately."
        channels.append(channel)

    # A source record is counted once. Unconfirmed records inherit the worst
    # unresolved batch disposition of their fixed delivery attempt, conservatively.
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    attempts = []
    for attempt in db.execute("SELECT * FROM projection_attempts WHERE destination=? AND source_kind='trace_record' ORDER BY created_at,id", (logfire_destination,)):
        item = dict(attempt)
        item.update(delivery_status="pending", error=None)
        if "otlp_batches" in tables:
            frozen = db.execute("SELECT 1 FROM otlp_plans WHERE attempt_id=?", (item["id"],)).fetchone()
            failure = db.execute("SELECT error_code FROM otlp_plan_failures WHERE attempt_id=?", (item["id"],)).fetchone()
            if not frozen and not failure:
                continue  # An unfrozen competing attempt cannot replace delivery ownership.
            batches = list(db.execute("SELECT status,outcome_json FROM otlp_batches WHERE attempt_id=?", (item["id"],)))
            rank = {"pending": 0, "accepted": 0, "retryable": 1, "in_flight": 2, "ambiguous": 2, "blocked": 3}
            for batch in batches:
                outcome = json.loads(batch["outcome_json"] or "{}")
                status = "ambiguous" if outcome.get("ambiguous") else batch["status"]
                if rank.get(status, 0) >= rank.get(item["delivery_status"], 0) and status != "accepted":
                    item.update(delivery_status=status, error=batch["outcome_json"])
            if failure:
                item.update(delivery_status="blocked", error={"code": failure[0]})
        else:
            continue
        attempts.append(item)

    def trace_rows():
        rows = db.execute(
            "SELECT t.record_id AS id,t.seq,t.observed_at AS created_at,"
            "(SELECT r.receipt_id FROM projection_receipts r WHERE r.source_record_id=t.record_id "
            "AND r.destination=? AND r.status='accepted' ORDER BY r.recorded_at DESC,r.seq DESC LIMIT 1) AS receipt_id,"
            "(SELECT MAX(r.recorded_at) FROM projection_receipts r WHERE r.source_record_id=t.record_id "
            "AND r.destination=? AND r.status='accepted') AS confirmed_at FROM trace_records t" +
            (" WHERE t.execution_id=?" if execution_id else ""), (logfire_destination, logfire_destination) + arguments,
        )
        for source in rows:
            row = dict(source)
            row["status"] = "confirmed" if row["confirmed_at"] else "pending"
            if not row["confirmed_at"]:
                attempt = next((a for a in reversed(attempts) if a["from_source_seq"] <= row["seq"] <= a["through_source_seq"]), None)
                if attempt:
                    row.update(status=attempt["delivery_status"], error=attempt["error"], updated_at=attempt["updated_at"])
            yield row
    trace = _summarize("logfire", "trace", logfire, trace_rows(), now)
    trace["unconfirmed_status_scope"] = "delivery_attempt"
    channels.append(trace)
    return {"schema_version": 1, "generated_at": now, "channels": channels}
