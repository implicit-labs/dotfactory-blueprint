"""Fail-soft delivery of committed events to external views."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from .ledger import SQLiteLedger
from .observability import canonical_json, stable_span_id


WATERFALL_FACT_VERSION = 1
SUMMARY_FACT_VERSION = 1


def _milliseconds(started_at: str | None, ended_at: str | None) -> int | None:
    if not started_at or not ended_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, round((end - start).total_seconds() * 1000))


def readable_error_groups(
    errors: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse repeated errors while preserving a link to every occurrence."""
    severity_rank = {"debug": 0, "info": 1, "warning": 2, "error": 3, "fatal": 4}
    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    for error in sorted(
        (dict(item) for item in errors),
        key=lambda item: (int(item.get("seq", 0)), str(item.get("error_id", ""))),
    ):
        fingerprint = str(error["fingerprint"])
        key = (int(error.get("fingerprint_version", 1)), fingerprint)
        occurrence = {
            "error_id": error["error_id"],
            "trace_record_id": error["trace_record_id"],
            "trace_seq": int(error.get("trace_seq", error["seq"])),
            "responsible_span_id": error.get("responsible_span_id"),
            "occurred_at": error["occurred_at"],
            "origin": error.get("origin"),
        }
        group = grouped.get(key)
        if group is None:
            grouped[key] = {
            "fingerprint_version": key[0],
            "fingerprint": fingerprint,
            "code": error["code"],
            "category": error["category"],
            "severity": error["severity"],
            "message": error["message"],
            "safe_remedy": error["safe_remedy"],
            "retryable": bool(error["retryable"]),
            "ambiguous_side_effect": bool(error["ambiguous_side_effect"]),
            "first_occurred_at": error["occurred_at"],
            "last_occurred_at": error["occurred_at"],
            "occurrence_count": 1,
            "occurrences": [occurrence],
            }
            continue
        group["last_occurred_at"] = error["occurred_at"]
        group["occurrence_count"] += 1
        group["retryable"] = bool(group["retryable"] and error["retryable"])
        group["ambiguous_side_effect"] = bool(
            group["ambiguous_side_effect"] or error["ambiguous_side_effect"]
        )
        if severity_rank.get(str(error["severity"]), 3) > severity_rank.get(
            str(group["severity"]), 3
        ):
            group["severity"] = str(error["severity"])
        group["occurrences"].append(occurrence)
    return sorted(
        grouped.values(),
        key=lambda item: (int(item["occurrences"][0]["trace_seq"]), item["fingerprint"]),
    )


def execution_waterfall(
    records: list[dict[str, Any]], errors: list[dict[str, Any]],
    completion_facts: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a deterministic, payload-free waterfall from durable facts."""
    if not records:
        raise ValueError("a waterfall requires at least one trace record")
    ordered = sorted(records, key=lambda item: int(item["seq"]))
    execution_ids = {str(item["execution_id"]) for item in ordered}
    if len(execution_ids) != 1:
        raise ValueError("waterfall records must belong to one execution")
    trace_ids = {str(item["trace_id"]) for item in ordered if item.get("trace_id")}
    if len(trace_ids) != 1:
        raise ValueError("waterfall records must share one trace id")
    error_by_record = {str(item["trace_record_id"]): item for item in errors}
    spans: dict[str, dict[str, Any]] = {}
    explicit_span_ends: dict[str, str] = {}
    points = []
    for record in ordered:
        parent = record.get("parent_span_id")
        if record.get("span_id") and record.get("ended_at"):
            span_id = str(record["span_id"])
            explicit_span_ends[span_id] = str(record["ended_at"])
        item = {
            "seq": int(record["seq"]),
            "record_id": record["record_id"],
            "kind": record["record_kind"],
            "domain": record["domain"],
            "phase": record["phase"],
            "name": record["name"],
            "status": record["status"],
            "span_id": record.get("span_id"),
            "parent_span_id": parent,
            "started_at": record.get("started_at") or record["observed_at"],
            "ended_at": record.get("ended_at"),
            "duration_ms": _milliseconds(
                record.get("started_at"), record.get("ended_at")
            ),
            "trust_class": record["trust_class"],
            "ordering_quality": record["ordering_quality"],
            "links": list(record.get("links") or []),
        }
        error = error_by_record.get(str(record["record_id"]))
        if error:
            item["error"] = {
                "error_id": error["error_id"], "code": error["code"],
                "fingerprint": error["fingerprint"],
            }
        if record["record_kind"] != "span" or not record.get("span_id"):
            points.append(item)
            continue
        span_id = str(record["span_id"])
        span = spans.get(span_id)
        if span is None:
            span = dict(item)
            span["record_ids"] = [record["record_id"]]
            span["phases"] = [record["phase"]]
            spans[span_id] = span
        else:
            span["record_ids"].append(record["record_id"])
            if record["phase"] not in span["phases"]:
                span["phases"].append(record["phase"])
            if record.get("started_at") and (
                not span.get("started_at")
                or str(record["started_at"]) < str(span["started_at"])
            ):
                span["started_at"] = record["started_at"]
            if record.get("ended_at") and (
                not span.get("ended_at")
                or str(record["ended_at"]) > str(span["ended_at"])
            ):
                span["ended_at"] = record["ended_at"]
            span["status"] = record["status"]
            if item.get("error"):
                span["error"] = item["error"]
    for span in spans.values():
        span["started_at"] = span.get("started_at") or ordered[0]["observed_at"]
        explicit_end = explicit_span_ends.get(str(span["span_id"]))
        if explicit_end:
            span["ended_at"] = explicit_end
    root_span = next(
        (span_id for span_id, span in spans.items()
         if span.get("parent_span_id") is None), None
    )
    # State-run and attempt spans are deterministic structure, not new source
    # facts. Derive any omitted structural parent from durable IDs on its children.
    for entity_kind, field, parent_kind in (
        ("state_run", "state_run_id", "execution"),
        ("attempt", "attempt_id", "state_run"),
    ):
        entity_ids = sorted({
            str(record[field]) for record in ordered if record.get(field)
        })
        for entity_id in entity_ids:
            span_id = stable_span_id(entity_kind, entity_id)
            if span_id in spans:
                continue
            members = [
                record for record in ordered if str(record.get(field) or "") == entity_id
            ]
            if not members:
                continue
            state_run_id = next(
                (str(item["state_run_id"]) for item in members
                 if item.get("state_run_id")), None
            )
            parent_span_id = (
                root_span if parent_kind == "execution"
                else stable_span_id("state_run", str(state_run_id))
            )
            starts = [
                str(item.get("started_at") or item["observed_at"]) for item in members
            ]
            ends = [str(item["ended_at"]) for item in members if item.get("ended_at")]
            first = min(members, key=lambda item: int(item["seq"]))
            spans[span_id] = {
                "seq": int(first["seq"]),
                "record_id": f"derived-{entity_kind}:{entity_id}",
                "record_ids": [], "kind": "span", "domain": "workflow",
                "phase": entity_kind, "phases": [entity_kind],
                "name": entity_kind.replace("_", " "), "status": "derived",
                "span_id": span_id, "parent_span_id": parent_span_id,
                "started_at": min(starts), "ended_at": max(ends) if ends else None,
                "duration_ms": None, "trust_class": "trusted-runtime",
                "ordering_quality": "exact", "links": [], "derived": True,
            }
    # Trace records are immutable. Older producers did not emit explicit close
    # records for execution and structural state-run spans, so apply their
    # canonical ledger completion times at projection time.
    for fact in completion_facts:
        entity_kind = str(fact.get("entity_kind") or "")
        entity_id = str(fact.get("entity_id") or "")
        completed_at = fact.get("completed_at")
        if not entity_kind or not entity_id or not completed_at:
            continue
        span_id = str(
            fact.get("span_id") or stable_span_id(entity_kind, entity_id)
        )
        span = spans.get(span_id)
        if span is not None and not span.get("ended_at"):
            span["ended_at"] = str(completed_at)
    span_ids = set(spans)
    items = sorted(
        [*spans.values(), *points], key=lambda item: (int(item["seq"]), item["record_id"])
    )
    for item in items:
        parent = item.get("parent_span_id")
        item["parent_known"] = parent is None or str(parent) in span_ids
        item["duration_ms"] = _milliseconds(
            item.get("started_at"), item.get("ended_at")
        )
    completeness_reasons = {
        str(reason)
        for record in ordered
        for reason in (record.get("completeness") or {}).get("reasons", [])
    }
    if any(not item["parent_known"] for item in items):
        completeness_reasons.add("missing_parent_span")
    completeness_reasons = sorted(completeness_reasons)
    fact = {
        "schema_version": WATERFALL_FACT_VERSION,
        "execution_id": ordered[0]["execution_id"],
        "trace_id": next(iter(trace_ids)),
        "from_trace_seq": int(ordered[0]["seq"]),
        "through_trace_seq": int(ordered[-1]["seq"]),
        "record_count": len(ordered),
        "item_count": len(items),
        "open_span_count": sum(
            1 for item in spans.values() if item["started_at"] and not item["ended_at"]
        ),
        "ordering_quality": (
            "exact" if all(item["ordering_quality"] == "exact" for item in items)
            else "reconstructed"
        ),
        "completeness": {
            "complete": not completeness_reasons,
            "reasons": completeness_reasons,
        },
        "items": items,
    }
    fact["digest"] = hashlib.sha256(
        canonical_json(fact).encode("utf-8")
    ).hexdigest()
    return fact


def summary_fact(
    current: dict[str, Any], waterfall: dict[str, Any],
    error_groups: list[dict[str, Any]], *, links: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Produce the small, stable fact block consumed by Linear projection."""
    state = str(current["current_state_id"])
    identifier = str(current["work_item_identifier"])
    primary_error = error_groups[-1] if error_groups else None
    headline = f"{identifier}: {state}"
    if primary_error and current["status"] != "completed":
        headline += f" - {primary_error['code']}"
    fact = {
        "schema_version": SUMMARY_FACT_VERSION,
        "execution_id": current["id"],
        "execution_key": current["execution_key"],
        "work_item_identifier": identifier,
        "status": current["status"],
        "current_state": state,
        "headline": headline,
        "trace": {
            "trace_id": waterfall["trace_id"],
            "from_seq": waterfall["from_trace_seq"],
            "through_seq": waterfall["through_trace_seq"],
            "record_count": waterfall["record_count"],
            "item_count": waterfall["item_count"],
            "open_span_count": waterfall["open_span_count"],
            "ordering_quality": waterfall["ordering_quality"],
            "complete": waterfall["completeness"]["complete"],
        },
        "errors": [{
            "code": item["code"], "message": item["message"],
            "safe_remedy": item["safe_remedy"],
            "occurrence_count": item["occurrence_count"],
            "fingerprint": item["fingerprint"],
            "first_trace_seq": item["occurrences"][0]["trace_seq"],
            "last_trace_seq": item["occurrences"][-1]["trace_seq"],
        } for item in error_groups],
        "links": sorted(links or [], key=lambda item: (item["kind"], item["url"])),
    }
    fact["digest"] = hashlib.sha256(
        canonical_json(fact).encode("utf-8")
    ).hexdigest()
    return fact


def projection_envelope(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_seq": record["event_seq"],
        "event_id": record["event_id"],
        "event_type": record["event_type"],
        "schema_version": record["schema_version"],
        "occurred_at": record["occurred_at"],
        "event_command_id": record["event_command_id"],
        "execution_id": record["execution_id"],
        "execution_key": record["execution_key"],
        "project_key": record["project_key"],
        "work_item_identifier": record["work_item_identifier"],
        "intent": json.loads(record["intent_snapshot_json"]),
        "policy": {
            "workflow_name": record["workflow_name"],
            "workflow_version": record["workflow_version"],
        },
        "payload": json.loads(record["payload_json"]),
    }


class RunProjection:
    """Provider-neutral, idempotent current-state and run-history projection."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}

    def apply(self, envelope: dict[str, Any]) -> None:
        event_id = str(envelope["event_id"])
        self.events.setdefault(event_id, dict(envelope))

    def run(self, execution_id: str) -> dict[str, Any]:
        events = sorted(
            (
                item for item in self.events.values()
                if item["execution_id"] == execution_id
            ),
            key=lambda item: int(item["event_seq"]),
        )
        if not events:
            raise KeyError(execution_id)
        state = None
        evidence = []
        feedback = []
        outcomes = []
        work_by_id: dict[str, dict[str, Any]] = {}
        for event in events:
            payload = event["payload"]
            if event["event_type"] == "execution_started":
                state = payload["state"]
            elif event["event_type"] == "transition_accepted":
                state = payload["to"]
                evidence.extend(payload.get("evidence") or [])
                feedback.extend(payload.get("feedback") or [])
                if payload.get("outcome") is not None:
                    outcomes.append(payload["outcome"])
                completed = payload.get("completed_attempt")
                if completed:
                    attempt_id = str(completed["attempt_id"])
                    item = work_by_id.setdefault(attempt_id, {})
                    item.update(completed)
                    item["status"] = "completed"
                entered = payload.get("entered_attempt")
                if entered:
                    attempt_id = str(entered["attempt_id"])
                    item = work_by_id.setdefault(attempt_id, {})
                    item.update(entered)
                    item["status"] = "active"
        first = events[0]
        return {
            "project_key": first["project_key"],
            "work_item_identifier": first["work_item_identifier"],
            "execution_key": first["execution_key"],
            "intent": first["intent"],
            "policy": first["policy"],
            "current_state": state,
            "evidence": evidence,
            "outcomes": outcomes,
            "feedback": feedback,
            "work": sorted(
                work_by_id.values(), key=lambda item: int(item["ordinal"])
            ),
            "events": events,
        }


class ProjectionWorker:
    def __init__(self, ledger: SQLiteLedger, destination: str,
                 sink: Callable[[dict[str, Any]], None]) -> None:
        self.ledger = ledger
        self.destination = destination
        self.sink = sink

    def drain(self, limit: int = 100) -> int:
        delivered = 0
        for record in self.ledger.pending(self.destination, limit):
            envelope = projection_envelope(record)
            try:
                self.sink(envelope)
            except Exception as error:
                self.ledger.mark_failed(record["id"], str(error))
                break
            self.ledger.mark_delivered(record["id"])
            delivered += 1
        return delivered

    def rebuild(
        self, *, command_id: str, requested_by: str,
        from_event_seq: int = 1, batch_size: int = 100
    ) -> dict[str, Any]:
        if batch_size < 1:
            raise ValueError("projection rebuild batch size must be positive")
        replay = self.ledger.start_projection_rebuild(
            self.destination, command_id=command_id, requested_by=requested_by,
            from_event_seq=from_event_seq,
        )
        replay_id = str(replay["id"])
        self.ledger.resume_projection_rebuild(replay_id)
        delivered_this_call = 0
        while True:
            records = self.ledger.pending_rebuild(replay_id, batch_size)
            if not records:
                break
            stopped = False
            for record in records:
                envelope = projection_envelope(record)
                try:
                    self.sink(envelope)
                except Exception as error:
                    self.ledger.mark_rebuild_failed(
                        replay_id, str(record["outbox_id"]), str(error)
                    )
                    stopped = True
                    break
                self.ledger.mark_rebuild_delivered(
                    replay_id, str(record["outbox_id"])
                )
                delivered_this_call += 1
            if stopped:
                break
        result = self.ledger.projection_rebuild(replay_id)
        result["delivered_this_call"] = delivered_this_call
        return result
