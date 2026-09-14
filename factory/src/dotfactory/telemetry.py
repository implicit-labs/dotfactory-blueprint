"""Deterministic OpenTelemetry projection with fail-soft Logfire delivery."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import unquote

from .ledger import SQLiteLedger
from .observability import ProjectionReceiptV1, canonical_json, stable_span_id


OTEL_MAPPING_VERSION = 1
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
        return f"logfire:{self.project}:{self.region}"

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


def _unix_nano(value: str | None) -> str:
    if not value:
        return "0"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    seconds = timegm(parsed.utctimetuple())
    return str(seconds * 1_000_000_000 + parsed.microsecond * 1_000)


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    else:
        encoded = {"stringValue": str(value)}
    return {"key": key, "value": encoded}


def otel_trace_document(
    records: list[dict[str, Any]], *, service_name: str,
    destination: str = "logfire",
) -> dict[str, Any]:
    """Map canonical records to stable OTLP JSON without raw payloads."""
    spans = []
    for record in sorted(records, key=lambda item: int(item["seq"])):
        started_at = record.get("started_at") or record["observed_at"]
        ended_at = record.get("ended_at") or started_at
        attributes = [
            _attribute("dotfactory.mapping.version", OTEL_MAPPING_VERSION),
            _attribute("dotfactory.record.id", record["record_id"]),
            _attribute("dotfactory.record.seq", int(record["seq"])),
            _attribute("dotfactory.execution.id", record["execution_id"]),
            _attribute("dotfactory.domain", record["domain"]),
            _attribute("dotfactory.phase", record["phase"]),
            _attribute("dotfactory.status", record["status"]),
            _attribute("dotfactory.trust_class", record["trust_class"]),
            _attribute("dotfactory.ordering_quality", record["ordering_quality"]),
            _attribute(
                "dotfactory.capture.complete",
                bool((record.get("completeness") or {}).get("complete", True)),
            ),
        ]
        span = {
            "traceId": str(record["trace_id"]),
            "spanId": str(record.get("span_id") or stable_span_id(
                "trace_record", str(record["record_id"])
            )),
            "name": f"{record['domain']}.{record['phase']}.{record['name']}",
            "kind": 1,
            "startTimeUnixNano": _unix_nano(str(started_at)),
            "endTimeUnixNano": _unix_nano(str(ended_at)),
            "attributes": attributes,
            "status": {"code": 2 if record["status"] == "failed" else 1},
        }
        if record.get("parent_span_id"):
            span["parentSpanId"] = str(record["parent_span_id"])
        spans.append(span)
    return {
        "resourceSpans": [{
            "resource": {"attributes": [
                _attribute("service.name", service_name),
                _attribute("dotfactory.projection.destination", destination),
            ]},
            "scopeSpans": [{
                "scope": {"name": "dotfactory", "version": str(OTEL_MAPPING_VERSION)},
                "spans": spans,
            }],
        }],
    }


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
            retryable=error.code == 429 or error.code >= 500,
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
            retryable=False,
        ) from error
    if not isinstance(result, dict):
        raise TelemetryProjectionError(
            "LOGFIRE_RESPONSE_INVALID", "Logfire returned a non-object response",
            retryable=False,
        )
    return result


class LogfireProjectionWorker:
    """Publish a fixed ledger range and persist one receipt per source record."""

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

    @staticmethod
    def _identity(attempt_id: str, record_id: str, delivery: int) -> tuple[str, str]:
        source = f"{attempt_id}:{record_id}:{delivery}"
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return f"otlp-{digest[:32]}", f"otlp:{digest}"

    def _delivery_number(self, attempt_id: str, record_id: str) -> int:
        return int(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM projection_receipts WHERE attempt_id=? "
            "AND source_record_id=?", (attempt_id, record_id),
        ).fetchone()[0]) + 1

    def publish(
        self, *, command_id: str, from_trace_seq: int = 1,
        through_trace_seq: int | None = None,
    ) -> dict[str, Any]:
        attempt = self.ledger.start_projection_attempt(
            self.settings.destination, command_id=command_id,
            idempotency_key=f"{self.settings.destination}:{command_id}",
            from_source_seq=from_trace_seq, through_source_seq=through_trace_seq,
        )
        attempt_id = str(attempt["id"])
        self.ledger.resume_projection_attempt(attempt_id)
        headers = {
            **self.settings.header_map,
            "content-type": "application/json",
            "user-agent": "dotfactory-otlp/1",
        }
        while True:
            records = self.ledger.pending_projection_records(
                attempt_id, limit=self.batch_size
            )
            if not records:
                self.ledger.advance_projection_watermark(
                    attempt_id,
                    through_source_seq=int(attempt["through_source_seq"]),
                )
                return self.ledger.projection_attempt(attempt_id)
            document = otel_trace_document(
                records, service_name=self.settings.service_name,
                destination=self.settings.destination,
            )
            body = canonical_json(document).encode("utf-8")
            try:
                response = self.transport(
                    self.settings.traces_endpoint, headers, body,
                    float(self.settings.timeout_seconds),
                )
                rejected = int((response.get("partialSuccess") or {}).get(
                    "rejectedSpans", 0
                ))
                if rejected:
                    raise TelemetryProjectionError(
                        "LOGFIRE_PARTIAL_REJECTION",
                        "Logfire rejected part of the OTLP trace batch",
                        retryable=True,
                    )
            except TelemetryProjectionError as error:
                for record in records:
                    delivery = self._delivery_number(
                        attempt_id, str(record["record_id"])
                    )
                    receipt_id, key = self._identity(
                        attempt_id, str(record["record_id"]), delivery
                    )
                    self.ledger.record_projection_receipt(
                        ProjectionReceiptV1(
                            receipt_id=receipt_id, attempt_id=attempt_id,
                            destination=self.settings.destination,
                            source_record_id=str(record["record_id"]),
                            status="rejected", idempotency_key=key,
                            recorded_at=self.ledger.clock(), error_code=error.code,
                            detail={
                                "credential_kind": "write_token",
                                "required_purpose": "otlp_trace_write",
                                "ambiguous": error.ambiguous,
                            },
                        ), rejection=error.safe_fact(),
                    )
                return self.ledger.projection_attempt(attempt_id)
            for record in records:
                delivery = self._delivery_number(attempt_id, str(record["record_id"]))
                receipt_id, key = self._identity(
                    attempt_id, str(record["record_id"]), delivery
                )
                self.ledger.record_projection_receipt(
                    ProjectionReceiptV1(
                        receipt_id=receipt_id, attempt_id=attempt_id,
                        destination=self.settings.destination,
                        source_record_id=str(record["record_id"]), status="accepted",
                        idempotency_key=key, recorded_at=self.ledger.clock(),
                        external_id=str(record["trace_id"]),
                        detail={"mapping_version": OTEL_MAPPING_VERSION},
                    )
                )

    def drain(self) -> dict[str, Any] | None:
        """Resume an unfinished range, otherwise publish the next fixed range."""
        prior = self.ledger.connection.execute(
            "SELECT * FROM projection_attempts WHERE destination=? "
            "AND status<>'completed' ORDER BY created_at,id LIMIT 1",
            (self.settings.destination,),
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
