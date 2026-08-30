"""Versioned, dependency-free contracts for durable factory observability."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


TRACE_RECORD_VERSION = 1
ERROR_FACT_VERSION = 1
PROJECTION_RECEIPT_VERSION = 1

RECORD_KINDS = frozenset(("span", "event", "error", "completeness"))
TRUST_CLASSES = frozenset((
    "trusted-runtime", "trusted-kernel", "provider-claimed",
    "untrusted-provider", "reconstructed",
))
ORDERING_QUALITIES = frozenset(("exact", "reconstructed"))
RECEIPT_STATUSES = frozenset(("accepted", "rejected", "duplicate"))


class ObservabilityContractError(ValueError):
    pass


def _required(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObservabilityContractError(f"{field_name} must be a non-empty string")
    return value


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ObservabilityContractError("non-finite numbers are not canonical JSON")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ObservabilityContractError("canonical JSON object keys must be strings")
        return {
            key: _canonical_value(value[key]) for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise ObservabilityContractError(
        f"unsupported canonical JSON value: {value.__class__.__name__}"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def stable_trace_id(execution_id: str) -> str:
    return hashlib.sha256(f"execution:{execution_id}".encode("utf-8")).hexdigest()[:32]


def stable_record_id(source_kind: str, source_id: str) -> str:
    digest = hashlib.sha256(
        f"{source_kind}:{source_id}".encode("utf-8")
    ).hexdigest()[:32]
    return f"tr-{digest}"


def stable_span_id(source_kind: str, source_id: str) -> str:
    return hashlib.sha256(
        f"{source_kind}:{source_id}".encode("utf-8")
    ).hexdigest()[:16]


def error_fingerprint(
    *, domain: str, phase: str, code: str, message: str, version: int = 1,
) -> str:
    stable_message = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", message.lower())
    stable_message = re.sub(r"\b\d+\b", "<n>", stable_message)
    source = canonical_json({
        "version": version, "domain": domain, "phase": phase,
        "code": code, "message": stable_message[:512],
    })
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TraceRecordV1:
    record_id: str
    execution_id: str
    source_kind: str
    source_id: str
    record_kind: str
    domain: str
    phase: str
    name: str
    status: str
    entity_kind: str
    entity_id: str
    origin: str
    trust_class: str
    observed_at: str
    state_run_id: str | None = None
    attempt_id: str | None = None
    runner_run_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    source_occurred_at: str | None = None
    ordering_quality: str = "exact"
    links: tuple[Mapping[str, Any], ...] = ()
    completeness: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = TRACE_RECORD_VERSION

    def __post_init__(self) -> None:
        for name in (
            "record_id", "execution_id", "source_kind", "source_id", "domain",
            "phase", "name", "status", "entity_kind", "entity_id",
            "origin", "observed_at",
        ):
            _required(getattr(self, name), name)
        if self.schema_version != TRACE_RECORD_VERSION:
            raise ObservabilityContractError("unsupported trace-record version")
        if self.record_kind not in RECORD_KINDS:
            raise ObservabilityContractError("invalid trace record kind")
        if self.trust_class not in TRUST_CLASSES:
            raise ObservabilityContractError("invalid trace trust class")
        if self.ordering_quality not in ORDERING_QUALITIES:
            raise ObservabilityContractError("invalid ordering quality")
        canonical_json(self.links)
        canonical_json(self.completeness)
        canonical_json(self.payload)

    def canonical(self) -> str:
        return canonical_json(asdict(self))


@dataclass(frozen=True)
class ErrorFactV1:
    error_id: str
    execution_id: str
    trace_record_id: str
    domain: str
    phase: str
    code: str
    category: str
    severity: str
    retryable: bool
    ambiguous_side_effect: bool
    fingerprint: str
    message: str
    safe_remedy: str
    occurred_at: str
    origin: str
    trust_class: str
    responsible_span_id: str | None = None
    last_good_span_id: str | None = None
    first_failed_span_id: str | None = None
    capture_complete: bool = True
    completeness: Mapping[str, Any] = field(default_factory=dict)
    fingerprint_version: int = 1
    schema_version: int = ERROR_FACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "error_id", "execution_id", "trace_record_id", "domain", "phase",
            "code", "category", "severity", "fingerprint", "message",
            "safe_remedy", "occurred_at", "origin",
        ):
            _required(getattr(self, name), name)
        if self.schema_version != ERROR_FACT_VERSION:
            raise ObservabilityContractError("unsupported error-fact version")
        if self.trust_class not in TRUST_CLASSES:
            raise ObservabilityContractError("invalid error trust class")
        canonical_json(self.completeness)

    def canonical(self) -> str:
        return canonical_json(asdict(self))


@dataclass(frozen=True)
class ProjectionReceiptV1:
    receipt_id: str
    attempt_id: str
    destination: str
    source_record_id: str
    status: str
    idempotency_key: str
    recorded_at: str
    external_id: str | None = None
    error_code: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = PROJECTION_RECEIPT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "receipt_id", "attempt_id", "destination", "source_record_id",
            "idempotency_key", "recorded_at",
        ):
            _required(getattr(self, name), name)
        if self.status not in RECEIPT_STATUSES:
            raise ObservabilityContractError("invalid projection receipt status")
        if self.schema_version != PROJECTION_RECEIPT_VERSION:
            raise ObservabilityContractError("unsupported projection receipt version")
        canonical_json(self.detail)

    def canonical(self) -> str:
        return canonical_json(asdict(self))
