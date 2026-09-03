"""Pure contracts and policy for reconciling Linear with the durable kernel."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


SHA256 = re.compile(r"^[0-9a-f]{64}$")
STATUS_TYPES = {"backlog", "unstarted", "started", "completed", "canceled", "duplicate"}
OBSERVATION_SOURCES = {"poll", "webhook"}
TRANSITION_SOURCES = {"agent", "human", "system", "recovery"}
RESERVED_LABEL_DIMENSIONS = {
    "assignee", "cycle", "delegate", "ownership", "priority", "project", "status", "team",
}


class LinearContractError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def poll_observation_key(issue_id: str, remote_updated_at: str, status_id: str) -> str:
    return "poll:" + content_hash({
        "issue_id": issue_id,
        "remote_updated_at": remote_updated_at,
        "status_id": status_id,
    })


def webhook_observation_key(delivery_id: str) -> str:
    if not delivery_id.strip():
        raise LinearContractError("Linear webhook delivery ID is required")
    return "webhook:" + delivery_id.strip()


@dataclass(frozen=True)
class LinearStatusBindingV1:
    project_key: str
    workflow_digest: str
    team_id: str
    status_id: str
    status_name: str
    status_type: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        for field_name in (
            "project_key", "workflow_digest", "team_id", "status_id", "status_name"
        ):
            value = str(getattr(self, field_name))
            if not value.strip():
                raise LinearContractError(f"Linear status binding {field_name} is required")
        if not SHA256.fullmatch(self.workflow_digest):
            raise LinearContractError("Linear status binding workflow_digest must be SHA-256")
        if self.status_type not in STATUS_TYPES:
            raise LinearContractError("Linear status binding status_type is unsupported")
        if self.schema_version != 1:
            raise LinearContractError("unsupported Linear status binding version")

    @property
    def binding_digest(self) -> str:
        return content_hash(asdict(self))

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["binding_digest"] = self.binding_digest
        return value


@dataclass(frozen=True)
class LinearObservationV1:
    execution_id: str
    project_key: str
    issue_id: str
    issue_identifier: str
    status_id: str
    status_name: str
    remote_updated_at: str
    observed_at: str
    payload_hash: str
    source: str
    observation_key: str
    delivery_id: str | None = None
    actor_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        for field_name in (
            "execution_id", "project_key", "issue_id", "issue_identifier",
            "status_id", "status_name", "remote_updated_at", "observed_at",
            "observation_key",
        ):
            if not str(getattr(self, field_name)).strip():
                raise LinearContractError(f"Linear observation {field_name} is required")
        if not SHA256.fullmatch(self.payload_hash):
            raise LinearContractError("Linear observation payload_hash must be SHA-256")
        if self.source not in OBSERVATION_SOURCES:
            raise LinearContractError("Linear observation source must be poll or webhook")
        if self.source == "webhook" and not self.delivery_id:
            raise LinearContractError("Linear webhook observation requires delivery_id")
        if self.schema_version != 1:
            raise LinearContractError("unsupported Linear observation version")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LinearTrackerPolicyV1:
    allowed_labels: Mapping[str, tuple[str, ...]]
    runner_overrides: Mapping[str, str]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise LinearContractError("unsupported Linear tracker policy version")
        normalized: set[str] = set()
        for group, values in self.allowed_labels.items():
            if not group.strip():
                raise LinearContractError("Linear label group name is required")
            if group.strip().lower() in RESERVED_LABEL_DIMENSIONS:
                raise LinearContractError(
                    f"Linear native field {group} cannot be duplicated as a label group"
                )
            for value in values:
                if not value.strip():
                    raise LinearContractError("Linear allowed label is empty")
                normalized.add(value)
        for label, runner in self.runner_overrides.items():
            if label not in normalized:
                raise LinearContractError(
                    f"runner override label {label} is not in the project allowlist"
                )
            if not runner.strip():
                raise LinearContractError("runner override must name a configured runner")

    def runner_for_labels(
        self, labels: list[str], *, configured_runners: set[str],
    ) -> str | None:
        selected = {
            self.runner_overrides[label]
            for label in labels if label in self.runner_overrides
        }
        if len(selected) > 1:
            raise LinearContractError("Linear labels select conflicting runner overrides")
        if not selected:
            return None
        runner = next(iter(selected))
        if runner not in configured_runners:
            raise LinearContractError(
                f"Linear runner override names unconfigured runner {runner}"
            )
        return runner


class LinearReconciler:
    """Network-free observation admission and kernel reconciliation."""

    def __init__(self, ledger: Any, kernel: Any) -> None:
        self.ledger = ledger
        self.kernel = kernel

    def ingest(self, observation: LinearObservationV1) -> dict[str, Any]:
        return self.ledger.record_linear_observation_input(observation.as_dict())

    def reconcile(
        self, observation_id: str, *, current_status_id: str,
        current_remote_updated_at: str, owner: str | None = None,
        allow_transition: bool = True,
    ) -> dict[str, Any]:
        observation = self.ledger.linear_observation(observation_id)
        if (
            observation["status_id"] != current_status_id
            or observation["remote_updated_at"] != current_remote_updated_at
        ):
            return self.ledger.resolve_linear_observation(
                observation_id, disposition="stale",
                reason="remote issue changed before reconciliation",
            )
        if not allow_transition:
            return self.ledger.resolve_linear_observation(
                observation_id, disposition="self_authored",
                reason="factory-authored observation cannot propose a transition",
            )
        binding = self.ledger.linear_status_binding(
            observation["project_key"], observation["workflow_digest"],
            observation["status_id"],
        )
        if not binding:
            return self.ledger.resolve_linear_observation(
                observation_id, disposition="rejected",
                reason="status ID is not bound to the execution workflow",
            )
        return self.kernel.observe_linear_status(
            observation["execution_id"], binding["status_name"],
            command_id=f"linear-observation:{observation_id}",
            source_event_id=observation["observation_key"], owner=owner,
            observation_id=observation_id,
        )
