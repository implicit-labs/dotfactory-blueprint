"""Deterministic OpenTelemetry projection with fail-soft Logfire delivery."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import unquote

from .ledger import SQLiteLedger
from .telemetry_delivery import TelemetryOutbox
from .telemetry_mapping import (
    OTEL_MAPPING_VERSION, _unix_nano, otel_trace_document,
)


Transport = Callable[[str, Mapping[str, str], bytes, float], Mapping[str, Any]]


class TelemetryProjectionError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, retryable: bool,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous

    def safe_fact(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "ambiguous": self.ambiguous,
        }


@dataclass(frozen=True)
class LogfireSettings:
    endpoint: str
    headers: str
    project: str
    service_name: str = "dotfactory"
    region: str = "us"
    timeout_seconds: int = 15

    def __post_init__(self) -> None:
        if not self.project.strip():
            raise TelemetryProjectionError(
                "LOGFIRE_PROJECT_MISMATCH",
                "Logfire projection requires a project identity",
                retryable=False,
            )
        endpoints = {
            "us": "https://logfire-us.pydantic.dev",
            "eu": "https://logfire-eu.pydantic.dev",
        }
        expected = endpoints.get(self.region)
        if expected is None or self.endpoint.rstrip("/") != expected:
            raise TelemetryProjectionError(
                "LOGFIRE_ENDPOINT_MISMATCH",
                "Logfire projection must use the endpoint for its configured region",
                retryable=False,
            )
        if not self.service_name.strip():
            raise TelemetryProjectionError(
                "LOGFIRE_SERVICE_MISSING", "Logfire service name is missing",
                retryable=False,
            )
        if not 1 <= self.timeout_seconds <= 60:
            raise TelemetryProjectionError(
                "LOGFIRE_TIMEOUT_INVALID", "Logfire timeout must be between 1 and 60 seconds",
                retryable=False,
            )
        if "authorization" not in self.header_map:
            raise TelemetryProjectionError(
                "LOGFIRE_WRITE_TOKEN_MISSING",
                "Logfire projection requires a write-token authorization header",
                retryable=False,
            )

    @property
    def destination(self) -> str:
        return f"logfire:{self.project}:{self.region}:otel-v2"

    @property
    def traces_endpoint(self) -> str:
        return self.endpoint.rstrip("/") + "/v1/traces"

    @property
    def header_map(self) -> dict[str, str]:
        result = {}
        for item in self.headers.split(","):
            key, separator, value = item.partition("=")
            if separator and key.strip() and value.strip():
                result[key.strip().lower()] = unquote(value.strip())
        return result



def _http_transport(
    endpoint: str, headers: Mapping[str, str], body: bytes, timeout: float,
) -> Mapping[str, Any]:
    request = urllib.request.Request(
        endpoint, data=body, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        raise TelemetryProjectionError(
            f"LOGFIRE_HTTP_{error.code}", "Logfire rejected the OTLP trace",
            retryable=error.code in (429, 502, 503, 504),
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise TelemetryProjectionError(
            "LOGFIRE_TRANSPORT_UNKNOWN", "Logfire delivery result is unknown",
            retryable=True, ambiguous=True,
        ) from error
    if not payload:
        return {}
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TelemetryProjectionError(
            "LOGFIRE_RESPONSE_INVALID", "Logfire returned invalid JSON",
            retryable=False, ambiguous=True,
        ) from error
    if not isinstance(result, dict):
        raise TelemetryProjectionError(
            "LOGFIRE_RESPONSE_INVALID", "Logfire returned a non-object response",
            retryable=False, ambiguous=True,
        )
    return result


def _response_outcome(response: Mapping[str, Any], span_count: int) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise TelemetryProjectionError(
            "LOGFIRE_RESPONSE_INVALID", "Invalid OTLP response",
            retryable=False, ambiguous=True,
        )
    partial = response.get("partialSuccess")
    if partial is None:
        return {"accepted_spans": span_count, "rejected_spans": 0, "ambiguous": False}
    try:
        if not isinstance(partial, dict):
            raise ValueError("invalid partialSuccess")
        value = partial.get("rejectedSpans", 0)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("invalid rejectedSpans")
        rejected = int(value)
        if rejected < 0 or rejected > span_count:
            raise ValueError("invalid rejection count")
    except (ValueError, TypeError) as error:
        raise TelemetryProjectionError(
            "LOGFIRE_RESPONSE_INVALID", "Invalid OTLP partial success",
            retryable=False, ambiguous=True,
        ) from error
    result = {
        "accepted_spans": span_count - rejected, "rejected_spans": rejected,
        "ambiguous": bool(rejected), "warning": bool(partial.get("errorMessage")),
    }
    if rejected:
        # Aggregate counts do not identify accepted spans. OTLP forbids retry.
        result.update(code="LOGFIRE_PARTIAL_REJECTION", retryable=False)
    return result


class LogfireProjectionWorker:
    """Freeze a fixed source range; retry saved batches, not rebuilt records."""

    def __init__(
        self, ledger: SQLiteLedger, settings: LogfireSettings, *,
        transport: Transport | None = None, batch_size: int = 1000,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("Logfire batch size must be between 1 and 1000")
        self.ledger = ledger
        self.settings = settings
        self.transport = transport or _http_transport
        self.batch_size = batch_size
        self.outbox = TelemetryOutbox(ledger, destination=settings.destination)

    def publish(
        self, *, command_id: str, from_trace_seq: int = 1,
        through_trace_seq: int | None = None,
    ) -> dict[str, Any]:
        with self.outbox.delivery_lock() as acquired:
            if not acquired:
                return {"status": "paused", "delivery": {
                    "status": "busy", "outcome": {
                        "code": "LOGFIRE_DESTINATION_BUSY", "retryable": True,
                    },
                }}
            return self._publish(
                command_id=command_id, from_trace_seq=from_trace_seq,
                through_trace_seq=through_trace_seq,
            )

    def _publish(
        self, *, command_id: str, from_trace_seq: int,
        through_trace_seq: int | None,
    ) -> dict[str, Any]:
        attempt = self.ledger.start_projection_attempt(
            self.settings.destination, command_id=command_id,
            idempotency_key=f"{self.settings.destination}:{command_id}",
            from_source_seq=from_trace_seq, through_source_seq=through_trace_seq,
        )
        attempt_id = str(attempt["id"])
        if attempt["status"] == "completed" or self.outbox.plan_failure(attempt_id):
            return self.outbox.result(attempt_id)
        try:
            frozen = self.outbox.freeze(
                attempt, service_name=self.settings.service_name, batch_size=self.batch_size,
            )
        except (ValueError, TypeError, KeyError):
            self.outbox.block_invalid_plan(attempt_id)
            return self.outbox.result(attempt_id)
        if not frozen:
            result = self.outbox.result(attempt_id)
            result["delivery"]["outcome"] = {
                "code": "LOGFIRE_DESTINATION_BUSY", "retryable": True,
            }
            return result
        headers = {
            **self.settings.header_map, "content-type": "application/json",
            "user-agent": "dotfactory-otlp/2",
        }
        while True:
            batch = self.outbox.next_batch(attempt_id)
            if batch is None:
                self.outbox.finish(attempt_id)
                return self.outbox.result(attempt_id)
            if batch["status"] == "blocked":
                return self.outbox.result(attempt_id)
            self.outbox.begin_delivery(batch)
            total = len(json.loads(batch["span_ids_json"]))
            try:
                response = self.transport(
                    self.settings.traces_endpoint, headers,
                    batch["body_json"].encode("utf-8"),
                    float(self.settings.timeout_seconds),
                )
                outcome = _response_outcome(response, total)
            except TelemetryProjectionError as error:
                outcome = {
                    "code": error.code, "retryable": error.retryable,
                    "ambiguous": error.ambiguous, "span_count": total,
                    "credential_kind": "write_token",
                    "required_purpose": "otlp_trace_write",
                }
                self.outbox.reject(batch, outcome)
                return self.outbox.result(attempt_id)
            if outcome.get("rejected_spans"):
                self.outbox.reject(batch, outcome)
                return self.outbox.result(attempt_id)
            self.outbox.acknowledge(batch, outcome)

    def drain(self) -> dict[str, Any] | None:
        """Blocked plans stay visible; later data cannot bypass their delivery."""
        prior = self.ledger.connection.execute(
            "SELECT a.* FROM projection_attempts a "
            "LEFT JOIN otlp_plans p ON p.attempt_id=a.id "
            "WHERE a.destination=? AND a.status<>'completed' "
            "ORDER BY CASE WHEN p.attempt_id IS NULL THEN 1 ELSE 0 END,"
            "a.created_at,a.id LIMIT 1", (self.settings.destination,),
        ).fetchone()
        if prior:
            return self.publish(
                command_id=str(prior["command_id"]),
                from_trace_seq=int(prior["from_source_seq"]),
                through_trace_seq=int(prior["through_source_seq"]),
            )
        watermark = self.ledger.connection.execute(
            "SELECT through_source_seq FROM projection_watermarks "
            "WHERE destination=? AND source_kind='trace_record'",
            (self.settings.destination,),
        ).fetchone()
        from_seq = int(watermark[0]) + 1 if watermark else 1
        through_seq = int(self.ledger.connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM trace_records"
        ).fetchone()[0])
        if through_seq < from_seq:
            return None
        return self.publish(
            command_id=f"runtime:{from_seq}:{through_seq}",
            from_trace_seq=from_seq, through_trace_seq=through_seq,
        )
