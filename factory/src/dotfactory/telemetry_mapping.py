"""OTLP v2: immutable structural ownership, unique factual observations."""

from __future__ import annotations

from calendar import timegm
from datetime import datetime, timezone
from typing import Any

from .observability import canonical_json, stable_span_id


OTEL_MAPPING_VERSION = 2


def _unix_nano(value: str | None) -> str:
    if not value:
        return "0"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return str(timegm(parsed.utctimetuple()) * 1_000_000_000 + parsed.microsecond * 1_000)


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    else:
        encoded = {"stringValue": str(value)}
    return {"key": key, "value": encoded}


def ownership(record: dict[str, Any]) -> list[tuple[str, str]]:
    """Only explicit runtime ownership; provider parent IDs are not authority."""
    chain = [("execution", str(record["execution_id"]))]
    if record["entity_kind"] == "execution":
        return chain
    for field, role in (("state_run_id", "state_run"), ("attempt_id", "attempt")):
        if record.get(field):
            chain.append((role, str(record[field])))
    payload = record.get("payload") or {} if record["source_kind"] != "runner_event" else {}
    preparation_id = record.get("_preparation_id") or payload.get("preparation_id")
    if record["entity_kind"] == "preparation":
        preparation_id = record["entity_id"]
    if preparation_id:
        chain.append(("preparation", str(preparation_id)))
    runner_id = record.get("runner_run_id")
    if record["entity_kind"] == "runner_run":
        runner_id = record["entity_id"]
    if runner_id:
        chain.append(("runner_run", str(runner_id)))
    kind, entity_id = str(record["entity_kind"]), str(record["entity_id"])
    if kind == "runner_operation":
        # Provider tool identifiers need the runner scope to prevent collisions.
        chain.append(("runner_operation", canonical_json([runner_id, entity_id])))
    elif (kind, entity_id) not in chain:
        chain.append((kind, entity_id))
    return chain


def record_spans(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidates; callers retain each anchor's earliest-sequence body forever."""
    result = []
    parent = None
    execution_id = str(record["execution_id"])
    timestamp = _unix_nano(str(record["observed_at"]))
    for role, entity_id in ownership(record):
        span_id = stable_span_id("otel-v2-anchor", canonical_json([
            execution_id, role, entity_id,
        ]))
        span = {
            "traceId": str(record["trace_id"]), "spanId": span_id,
            "name": f"dotfactory.{role}", "kind": 1,
            "startTimeUnixNano": timestamp, "endTimeUnixNano": timestamp,
            "attributes": [
                _attribute("dotfactory.mapping.version", OTEL_MAPPING_VERSION),
                _attribute("dotfactory.execution.id", execution_id),
                _attribute("dotfactory.entity.kind", role),
                _attribute("dotfactory.entity.id", stable_span_id("provider-entity", entity_id)
                           if role == "runner_operation" else entity_id),
                _attribute("dotfactory.structural", True),
                _attribute("dotfactory.derived", True),
                _attribute("dotfactory.duration.known", False),
                _attribute("dotfactory.anchor.source_record_id", record["record_id"]),
                _attribute("dotfactory.anchor.source_seq", int(record["seq"])),
            ],
            # An ownership anchor is not a successful or completed operation.
            "status": {"code": 0},
        }
        if parent:
            span["parentSpanId"] = parent
        result.append(span)
        parent = span_id
    start = record.get("started_at")
    end = record.get("ended_at")
    duration_known = bool(start and end)
    if not duration_known:
        start = end = record["observed_at"]
    leaf = {
        "traceId": str(record["trace_id"]),
        "spanId": stable_span_id("otel-v2-observation", str(record["record_id"])),
        "parentSpanId": parent,
        "name": f"{record['domain']}.{record['phase']}.{record['name']}",
        "kind": 1,
        "startTimeUnixNano": _unix_nano(str(start)),
        "endTimeUnixNano": _unix_nano(str(end)),
        "attributes": [
            _attribute("dotfactory.mapping.version", OTEL_MAPPING_VERSION),
            _attribute("dotfactory.record.id", record["record_id"]),
            _attribute("dotfactory.record.seq", int(record["seq"])),
            _attribute("dotfactory.execution.id", execution_id),
            _attribute("dotfactory.domain", record["domain"]),
            _attribute("dotfactory.phase", record["phase"]),
            _attribute("dotfactory.status", record["status"]),
            _attribute("dotfactory.trust_class", record["trust_class"]),
            _attribute("dotfactory.ordering_quality", record["ordering_quality"]),
            _attribute("dotfactory.structural", False),
            _attribute("dotfactory.duration.known", duration_known),
            _attribute("dotfactory.ownership.complete", not (
                (record.get("attempt_id") and not record.get("state_run_id")) or
                (record.get("runner_run_id") and not record.get("attempt_id")) or
                (record["entity_kind"] == "runner_operation" and not record.get("runner_run_id"))
            )),
            _attribute("dotfactory.capture.complete", bool(
                (record.get("completeness") or {}).get("complete", True)
            )),
        ],
        "status": {"code": 2 if record["status"] == "failed" else (
            1 if record["status"] == "completed" else 0
        )},
    }
    for key, value in (record.get("_error") or {}).items():
        if key in ("code", "category", "fingerprint", "ambiguous_side_effect", "capture_complete"):
            leaf["attributes"].append(_attribute(f"dotfactory.error.{key}", value))
    result.append(leaf)
    return result


def span_document(
    spans: list[dict[str, Any]], service_name: str, destination: str,
) -> dict[str, Any]:
    return {"resourceSpans": [{
        "resource": {"attributes": [
            _attribute("service.name", service_name),
            _attribute("dotfactory.projection.destination", destination),
        ]},
        "scopeSpans": [{
            "scope": {"name": "dotfactory", "version": str(OTEL_MAPPING_VERSION)},
            "spans": spans,
        }],
    }]}


def otel_trace_document(
    records: list[dict[str, Any]], *, service_name: str,
    destination: str = "logfire:otel-v2",
) -> dict[str, Any]:
    """Self-contained view; the live worker freezes anchors from ledger history."""
    spans: dict[tuple[str, str], dict[str, Any]] = {}
    for record in sorted(records, key=lambda item: int(item["seq"])):
        for span in record_spans(record):
            spans.setdefault((span["traceId"], span["spanId"]), span)
    return span_document(list(spans.values()), service_name, destination)
