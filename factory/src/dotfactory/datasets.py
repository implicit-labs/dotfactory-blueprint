"""Deterministic, local-first execution datasets and conformance scorers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .observability import ProjectionReceiptV1, canonical_json


DATASET_VERSION = 1
MANIFEST_VERSION = 1
SECRET = re.compile(
    r"(?i)(?:lin_api_|sk-|ghp_|github_pat_)[a-z0-9_-]{8,}|"
    r"(?:authorization|cookie|password|secret|token|api[_-]?key)\s*[:=]"
)


class DatasetContractError(ValueError):
    pass


def _safe_text(value: Any, field: str) -> str:
    text = str(value or "")
    if text.startswith(("/", "~/")) or SECRET.search(text):
        raise DatasetContractError(f"dataset {field} contains a private path or credential")
    return text


def _safe_links(items: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    result = []
    for item in items:
        uri = str(item.get("uri") or item.get("url") or "")
        if not uri.startswith("https://"):
            continue
        result.append({
            "kind": _safe_text(item.get("kind", "evidence"), "link kind")[:100],
            "url": _safe_text(uri, "link URL")[:1000],
        })
    return sorted(result, key=lambda item: (item["kind"], item["url"]))


def _skills(history: Mapping[str, Any]) -> list[dict[str, Any]]:
    skills: dict[tuple[str, str], dict[str, Any]] = {}
    for event in history.get("events") or []:
        receipt = (event.get("payload") or {}).get("skill_receipt")
        if not isinstance(receipt, dict):
            continue
        for item in receipt.get("resolved") or receipt.get("skills") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = _safe_text(item["name"], "skill name")
            digest = _safe_text(
                item.get("content_hash") or item.get("digest") or "unknown",
                "skill digest",
            )
            skills[(name, digest)] = {
                "name": name, "digest": digest,
                "version": _safe_text(item.get("version", "content-addressed"), "skill version"),
            }
    return [skills[key] for key in sorted(skills)]


def _runners(ledger: Any, execution_id: str) -> list[dict[str, Any]]:
    rows = ledger.connection.execute(
        "SELECT runner_key,adapter_kind,adapter_version,protocol_version,status,"
        "command_digest,prompt_digest FROM runner_runs WHERE execution_id=? "
        "ORDER BY created_at,id", (execution_id,),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def execution_dataset_case(
    ledger: Any, execution_id: str, projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Join one execution using an explicit allowlist and no raw runner content."""
    snapshot = ledger.run_snapshot(execution_id)
    history = ledger.run_history(execution_id)
    intent = history.get("intent") or {}
    summary = projection["summary"]
    waterfall = projection["waterfall"]
    feedback = []
    for item in history.get("feedback") or []:
        body = item.get("body") or {}
        feedback.append({
            "id": str(item["id"]),
            "source": _safe_text(item.get("source"), "feedback source"),
            "kind": _safe_text(body.get("kind", "feedback"), "feedback kind"),
            "author": _safe_text(body.get("author", "unknown"), "feedback author"),
            "url": _safe_text(body.get("url", ""), "feedback URL"),
            "created_at": str(item["created_at"]),
        })
    usage = {
        key: sum(int(value.get(key) or 0) for value in (
            history.get("state_token_usage") or {}
        ).values())
        for key in (
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "total_tokens",
        )
    }
    case = {
        "schema_version": DATASET_VERSION,
        "case_id": hashlib.sha256(
            f"execution:{execution_id}:{summary['digest']}".encode("utf-8")
        ).hexdigest(),
        "inputs": {
            "work_item_identifier": snapshot["work_item_identifier"],
            "intent": {
                "title": _safe_text(intent.get("title"), "intent title"),
                "source": _safe_text(intent.get("source", "unknown"), "intent source"),
            },
            "policy": {
                "workflow_name": history["policy"]["workflow_name"],
                "workflow_version": history["policy"]["workflow_version"],
                "workflow_digest": history["policy"]["workflow_digest"],
            },
        },
        "output": {
            "status": snapshot["status"],
            "current_state": snapshot["current_state_id"],
            "summary_digest": summary["digest"],
            "trace_id": waterfall["trace_id"],
        },
        "metadata": {
            "runners": _runners(ledger, execution_id),
            "skills": _skills(history),
            "usage": usage,
            "errors": summary["errors"],
            "evidence": _safe_links(history.get("artifacts") or []),
            "feedback": feedback,
            "completeness": {
                "normalized": True,
                "raw": waterfall["completeness"],
                "archive": {"available": False, "owner": "archive-capability"},
                "projection": {
                    "logfire_required": False,
                    "linear_evidence": projection.get("linear_evidence"),
                },
            },
        },
    }
    canonical_json(case)
    return case


def score_execution_case(case: Mapping[str, Any], waterfall: Mapping[str, Any]) -> dict[str, Any]:
    """Return deterministic runtime-conformance scores, separate from quality."""
    items = list(waterfall.get("items") or [])
    span_ids = {str(item["span_id"]) for item in items if item.get("span_id")}
    roots = [
        item for item in items
        if item.get("kind") == "span" and not item.get("parent_span_id")
    ]
    terminal = str(case["output"]["status"]) == "completed"
    errors = list(case["metadata"].get("errors") or [])
    evidence = list(case["metadata"].get("evidence") or [])

    def clock_is_valid(item: Mapping[str, Any]) -> bool:
        started = item.get("started_at")
        ended = item.get("ended_at")
        if not started or not ended:
            return True
        try:
            return datetime.fromisoformat(
                str(ended).replace("Z", "+00:00")
            ) >= datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        except ValueError:
            return False

    checks = {
        "orphan_spans": len(roots) == 1,
        "invalid_parents": all(
            not item.get("parent_span_id")
            or str(item["parent_span_id"]) in span_ids for item in items
        ),
        "missing_terminal": not terminal or int(
            waterfall.get("open_span_count", 0)
        ) == 0,
        "false_success": not terminal or not errors,
        "bad_transition": not any(
            str(error.get("category")) == "transition"
            or str(error.get("code", "")).startswith("INVALID_TRANSITION")
            for error in errors
        ),
        "evidence_gap": not terminal or bool(evidence),
        "capture_loss": bool(
            (waterfall.get("completeness") or {}).get("complete")
        ),
        "clock_anomaly": all(clock_is_valid(item) for item in items),
    }
    return {
        "schema_version": 2,
        "case_id": case["case_id"],
        "passed": all(checks.values()),
        "checks": checks,
    }


def incident_rows(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{
        "schema_version": 1,
        "case_id": case["case_id"],
        "fingerprint": item["fingerprint"],
        "code": item["code"],
        "safe_remedy": item["safe_remedy"],
        "occurrence_count": item["occurrence_count"],
    } for item in case["metadata"].get("errors") or []]


def dataset_bundle(
    cases: list[Mapping[str, Any]], *, dataset_name: str = "dotfactory-executions",
) -> tuple[bytes, bytes]:
    ordered = sorted(cases, key=lambda item: str(item["case_id"]))
    jsonl = b"".join(
        canonical_json(item).encode("utf-8") + b"\n" for item in ordered
    )
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "dataset_name": dataset_name,
        "case_count": len(ordered),
        "case_ids": [item["case_id"] for item in ordered],
        "jsonl_sha256": hashlib.sha256(jsonl).hexdigest(),
        "runtime_conformance_version": 2,
        "quality_observation_version": 1,
    }
    return jsonl, canonical_json(manifest).encode("utf-8") + b"\n"


def write_dataset_bundle(
    directory: str | Path, cases: list[Mapping[str, Any]], *,
    dataset_name: str = "dotfactory-executions",
) -> dict[str, Any]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    jsonl, manifest = dataset_bundle(cases, dataset_name=dataset_name)
    data_path = root / f"{dataset_name}.jsonl"
    manifest_path = root / f"{dataset_name}.manifest.json"
    data_path.write_bytes(jsonl)
    manifest_path.write_bytes(manifest)
    return {
        "dataset": str(data_path), "manifest": str(manifest_path),
        "jsonl_sha256": hashlib.sha256(jsonl).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }


@dataclass(frozen=True)
class HostedDatasetSettings:
    api_key: str
    project: str
    region: str = "us"
    dataset_name: str = "dotfactory-executions"

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise DatasetContractError(
                "hosted dataset publication requires a project API key"
            )
        if not self.project.strip():
            raise DatasetContractError(
                "hosted dataset publication requires a project identity"
            )
        if self.region not in ("us", "eu"):
            raise DatasetContractError(
                "hosted dataset publication region must be us or eu"
            )

    @property
    def destination(self) -> str:
        return f"logfire-dataset:{self.project}:{self.region}"


class HostedDatasetPublisher:
    """Optional edge; importing Logfire never becomes a runtime dependency."""

    def __init__(
        self, settings: HostedDatasetSettings, *,
        client_factory: Callable[[str], Any] | None = None, ledger: Any | None = None,
    ) -> None:
        self.settings = settings
        self.client_factory = client_factory or self._client
        self.ledger = ledger

    @staticmethod
    def _client(api_key: str) -> Any:
        try:
            from logfire.experimental.api_client import LogfireAPIClient
        except ImportError as error:
            raise DatasetContractError(
                "hosted datasets require the optional logfire datasets client"
            ) from error
        return LogfireAPIClient(api_key=api_key)

    @staticmethod
    def _receipt_identity(
        attempt_id: str, case_id: str, delivery: int,
    ) -> tuple[str, str]:
        digest = hashlib.sha256(
            f"{attempt_id}:{case_id}:{delivery}".encode("utf-8")
        ).hexdigest()
        return f"dataset-{digest[:32]}", f"dataset:{digest}"

    def _record(
        self, attempt_id: str, cases: list[Mapping[str, Any]], *,
        status: str, error_code: str | None = None,
    ) -> None:
        if not self.ledger:
            return
        for item in cases:
            case_id = str(item["case_id"])
            delivery = int(self.ledger.connection.execute(
                "SELECT COUNT(*) FROM projection_receipts WHERE attempt_id=? "
                "AND source_record_id=?", (attempt_id, case_id),
            ).fetchone()[0]) + 1
            receipt_id, key = self._receipt_identity(
                attempt_id, case_id, delivery
            )
            self.ledger.record_projection_receipt(
                ProjectionReceiptV1(
                    receipt_id=receipt_id, attempt_id=attempt_id,
                    destination=self.settings.destination,
                    source_record_id=case_id, status=status,
                    idempotency_key=key, recorded_at=self.ledger.clock(),
                    external_id=(
                        f"{self.settings.dataset_name}:{case_id}"
                        if status == "accepted" else None
                    ),
                    error_code=error_code,
                    detail={
                        "dataset_name": self.settings.dataset_name,
                        "credential_kind": "project_api_key",
                        "required_scopes": [
                            "project:read_datasets", "project:write_datasets",
                        ],
                        "ambiguous": status == "rejected",
                    },
                ), rejection=(
                    {
                        "code": error_code,
                        "message": "hosted dataset delivery result is unknown",
                        "retryable": True,
                    }
                    if status == "rejected" else None
                ),
            )

    def publish(
        self, cases: list[Mapping[str, Any]], *, command_id: str | None = None,
    ) -> dict[str, Any]:
        ordered = sorted(cases, key=lambda item: str(item["case_id"]))
        attempt = None
        if self.ledger:
            if not command_id:
                raise DatasetContractError(
                    "hosted dataset receipt persistence requires a command ID"
                )
            attempt = self.ledger.start_projection_attempt(
                self.settings.destination, command_id=command_id,
                idempotency_key=(
                    f"{self.settings.destination}:{command_id}"
                ),
                source_kind="dataset_case", from_source_seq=1,
                through_source_seq=len(ordered),
            )
            if attempt["status"] == "completed":
                return {
                    "dataset_name": self.settings.dataset_name,
                    "case_ids": [str(item["case_id"]) for item in ordered],
                    "projection_attempt_id": attempt["id"],
                    "status": "completed",
                }
            self.ledger.resume_projection_attempt(str(attempt["id"]))
        client = None
        try:
            try:
                client = self.client_factory(self.settings.api_key)
                try:
                    client.get_dataset(self.settings.dataset_name)
                except Exception as error:
                    status = getattr(
                        getattr(error, "response", None), "status_code", None
                    )
                    if error.__class__.__name__ != "NotFoundError" and status != 404:
                        raise
                    client.create_dataset(
                        name=self.settings.dataset_name,
                        description="Deterministic dotfactory execution facts",
                    )
                hosted_cases = [{
                    "name": str(item["case_id"]),
                    "inputs": item["inputs"],
                    "expected_output": item["output"],
                    "metadata": item["metadata"],
                } for item in ordered]
                client.add_cases(self.settings.dataset_name, cases=hosted_cases)
                remote = client.list_cases(self.settings.dataset_name)
                expected = [str(item["case_id"]) for item in ordered]
                observed = sorted(str(
                    item.get("name") or item.get("case_id") or ""
                ) for item in remote)
                if expected != observed:
                    raise DatasetContractError(
                        "hosted dataset read-back did not match local cases"
                    )
            except Exception:
                if attempt:
                    self._record(
                        str(attempt["id"]), ordered, status="rejected",
                        error_code="HOSTED_DATASET_PUBLISH_FAILED",
                    )
                raise
        finally:
            close = getattr(client, "close", None)
            if close:
                close()
        if attempt:
            self._record(str(attempt["id"]), ordered, status="accepted")
            self.ledger.advance_projection_watermark(
                str(attempt["id"]), through_source_seq=len(ordered)
            )
        return {
            "dataset_name": self.settings.dataset_name, "case_ids": expected,
            **({
                "projection_attempt_id": attempt["id"], "status": "completed",
            } if attempt else {}),
        }
