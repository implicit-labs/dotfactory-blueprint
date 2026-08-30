"""Mobile-safe observation and audited control over the durable kernel."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .kernel import DurableKernel
from .ledger import LedgerError, SQLiteLedger


API_VERSION = "v1"
MAX_PAGE_SIZE = 100


class ControlError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    channel: str

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("principal subject is required")
        if self.role not in ("viewer", "operator", "approver"):
            raise ValueError("principal role must be viewer, operator, or approver")
        if not self.channel.strip():
            raise ValueError("principal channel is required")


def _limit(value: int) -> int:
    if value < 1 or value > MAX_PAGE_SIZE:
        raise ControlError(
            "invalid_limit", f"limit must be between 1 and {MAX_PAGE_SIZE}"
        )
    return value


def _encode_cursor(first: str, second: str) -> str:
    payload = json.dumps([first, second], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if (
            not isinstance(decoded, list) or len(decoded) != 2
            or not all(isinstance(item, str) for item in decoded)
        ):
            raise ValueError
        return decoded[0], decoded[1]
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ControlError("invalid_cursor", "cursor is invalid") from error


def _without_fences(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_fences(item)
            for key, item in value.items()
            if key != "fence_token"
        }
    if isinstance(value, list):
        return [_without_fences(item) for item in value]
    return value


def _condition_matches(
    ledger: SQLiteLedger, execution_id: str, edge: dict[str, Any]
) -> bool:
    condition = edge.get("condition")
    if not condition:
        return True
    prefix = "resume_state == "
    if not str(condition).startswith(prefix):
        return False
    history = ledger.run_history(execution_id)["state_runs"]
    prior_work = [
        item["state_id"] for item in history[:-1] if item["state_kind"] == "work"
    ]
    return bool(prior_work and prior_work[-1] == str(condition)[len(prefix):])


def _control_edges(
    ledger: SQLiteLedger, kernel: DurableKernel, execution_id: str, state: str
) -> list[dict[str, Any]]:
    edges = []
    _workflow, _states, execution_edges = kernel.graph_for_execution(execution_id)
    for edge in execution_edges:
        if edge["from"] not in (state, "@any_nonterminal"):
            continue
        if {"actor": "human", "signal": "control_command"} not in edge["evocations"]:
            continue
        if _condition_matches(ledger, execution_id, edge):
            edges.append(edge)
    return edges


def _control_targets(
    ledger: SQLiteLedger, kernel: DurableKernel, execution_id: str, state: str
) -> set[str]:
    return {
        str(edge["to"]) for edge in _control_edges(ledger, kernel, execution_id, state)
        if edge.get("action", "transition") == "transition"
    }


class ObservationService:
    def __init__(self, ledger: SQLiteLedger, kernel: DurableKernel) -> None:
        self.ledger = ledger
        self.kernel = kernel

    def overview(self) -> dict[str, Any]:
        return {"api_version": API_VERSION, "data": self.ledger.overview()}

    def runs(
        self, *, project_key: str | None = None, status: str | None = None,
        state: str | None = None, limit: int = 25, cursor: str | None = None,
    ) -> dict[str, Any]:
        page_size = _limit(limit)
        before_created_at, before_id = _decode_cursor(cursor)
        items = self.ledger.list_runs(
            project_key=project_key, status=status, state=state, limit=page_size + 1,
            before_created_at=before_created_at, before_id=before_id,
        )
        has_more = len(items) > page_size
        data = items[:page_size]
        next_cursor = None
        if has_more and data:
            next_cursor = _encode_cursor(data[-1]["created_at"], data[-1]["id"])
        return {"api_version": API_VERSION, "data": data, "next_cursor": next_cursor}

    def run(self, execution_id: str) -> dict[str, Any]:
        snapshot = self.ledger.run_snapshot(execution_id)
        snapshot["available_actions"] = self.available_actions(snapshot)
        return {"api_version": API_VERSION, "data": _without_fences(snapshot)}

    def events(
        self, execution_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        self.ledger.current(execution_id)
        if after_seq < 0:
            raise ControlError("invalid_after_seq", "after_seq cannot be negative")
        page_size = _limit(limit)
        items = self.ledger.events_page(
            execution_id, after_seq=after_seq, limit=page_size + 1
        )
        has_more = len(items) > page_size
        data = items[:page_size]
        return {
            "api_version": API_VERSION,
            "data": data,
            "next_after_seq": data[-1]["seq"] if has_more and data else None,
        }

    def artifacts(
        self, execution_id: str, *, kind: str | None = None, limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self.ledger.current(execution_id)
        return self._cursor_page(
            self.ledger.artifacts_page, execution_id=execution_id, limit=limit,
            cursor=cursor, kind=kind, time_field="created_at",
        )

    def feedback(
        self, execution_id: str, *, limit: int = 25, cursor: str | None = None
    ) -> dict[str, Any]:
        self.ledger.current(execution_id)
        return self._cursor_page(
            self.ledger.feedback_page, execution_id=execution_id, limit=limit,
            cursor=cursor, time_field="created_at",
        )

    def resources(
        self, *, status: str | None = None, project_key: str | None = None,
        execution_id: str | None = None, limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        page_size = _limit(limit)
        before_time, before_id = _decode_cursor(cursor)
        items = self.ledger.resource_page(
            status=status, project_key=project_key, execution_id=execution_id,
            limit=page_size + 1, before_acquired_at=before_time, before_id=before_id,
        )
        has_more = len(items) > page_size
        data = _without_fences(items[:page_size])
        next_cursor = None
        if has_more and data:
            next_cursor = _encode_cursor(data[-1]["acquired_at"], data[-1]["id"])
        return {"api_version": API_VERSION, "data": data, "next_cursor": next_cursor}

    def command(self, command_id: str) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "data": _without_fences(self.ledger.control_command(command_id)),
        }

    def _cursor_page(
        self, callback: Any, *, execution_id: str, limit: int,
        cursor: str | None, time_field: str, **filters: Any,
    ) -> dict[str, Any]:
        page_size = _limit(limit)
        before_time, before_id = _decode_cursor(cursor)
        items = callback(
            execution_id, limit=page_size + 1, before_created_at=before_time,
            before_id=before_id, **filters,
        )
        has_more = len(items) > page_size
        data = items[:page_size]
        next_cursor = None
        if has_more and data:
            next_cursor = _encode_cursor(data[-1][time_field], data[-1]["id"])
        return {"api_version": API_VERSION, "data": data, "next_cursor": next_cursor}

    def available_actions(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        if snapshot["status"] != "running":
            return []
        state = str(snapshot["current_state_id"])
        edges = _control_edges(
            self.ledger, self.kernel, str(snapshot["id"]), state
        )
        actions: list[dict[str, Any]] = []
        for action in ("cancel", "approve", "retry"):
            candidates = [
                edge for edge in edges if edge.get("action", "transition") == action
            ]
            if candidates:
                actions.append({
                    "action": action,
                    "confirmation_required": any(
                        edge.get("confirmation") is True for edge in candidates
                    ),
                })
        targets = sorted(
            str(edge["to"]) for edge in edges
            if edge.get("action", "transition") == "transition"
        )
        if targets:
            actions.append({
                "action": "transition", "confirmation_required": "terminal_only",
                "targets": targets,
            })
        for attention in snapshot.get("attention_requests", []):
            remedies = list(attention.get("detail", {}).get("allowed_actions", []))
            if remedies:
                actions.append({
                    "action": "attention",
                    "attention_id": attention["id"],
                    "expected_attempt_id": attention.get("attempt_id"),
                    "remedies": remedies,
                    "confirmation_required": [
                        remedy for remedy in remedies if remedy == "release"
                    ],
                })
        return actions


class ControlService:
    def __init__(
        self, ledger: SQLiteLedger, kernel: DurableKernel,
        resource_controller: Any | None = None,
        attention_controllers: Mapping[str, Any] | None = None,
    ) -> None:
        self.ledger = ledger
        self.kernel = kernel
        self.resource_controller = resource_controller
        self.attention_controllers = dict(attention_controllers or {})
        self.observation = ObservationService(ledger, kernel)

    def execute(
        self, execution_id: str, *, command_id: str, principal: Principal,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", command_id):
            raise ControlError(
                "invalid_command_id",
                "Idempotency-Key must use 1 to 200 URL-safe characters",
            )
        action = request.get("action")
        if action not in ("cancel", "retry", "approve", "transition", "attention"):
            raise ControlError("invalid_action", "action is not supported")
        if not isinstance(request.get("expected_state"), str):
            raise ControlError("expected_state_required", "expected_state is required")
        parameters = request.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ControlError("invalid_parameters", "parameters must be an object")
        normalized = {
            "action": action,
            "expected_state": request["expected_state"],
            "confirmed": request.get("confirmed") is True,
            "parameters": parameters,
        }
        principal_data = asdict(principal)
        request_hash = hashlib.sha256(json.dumps(
            {"execution_id": execution_id, "principal": principal_data, "request": normalized},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        try:
            receipt = self.ledger.begin_control_command(
                command_id=command_id, execution_id=execution_id, action=action,
                principal=principal_data, request_hash=request_hash, request=normalized,
            )
        except LedgerError as error:
            if "different inputs" in str(error):
                raise ControlError("idempotency_conflict", str(error), status=409) from error
            raise
        if receipt["status"] in ("completed", "denied", "failed"):
            return _without_fences(receipt)
        if receipt["status"] == "received":
            allowed, reason = self._authorize(execution_id, principal, normalized)
            receipt = self.ledger.authorize_control_command(
                command_id, allowed=allowed, reason=reason
            )
            if not allowed:
                return receipt
        try:
            prior_decision = (
                None if action == "attention" else self.ledger.decision_for_command(
                    f"execution:{execution_id}:transition:control:{command_id}"
                )
            )
            if prior_decision:
                prior_decision["recovered"] = True
                return self._complete(execution_id, command_id, prior_decision)
            current = self.ledger.current(execution_id)
            if current["current_state_id"] != normalized["expected_state"]:
                raise ControlError(
                    "stale_state",
                    f"expected {normalized['expected_state']}, found "
                    f"{current['current_state_id']}", status=409,
                )
            result = self._apply(
                execution_id, command_id=command_id, principal=principal,
                request=normalized, current=current,
            )
            return self._complete(execution_id, command_id, result)
        except (ControlError, LedgerError) as error:
            code = error.code if isinstance(error, ControlError) else "command_rejected"
            self.ledger.finish_control_command(
                command_id, error={"code": code, "message": str(error)}
            )
            if isinstance(error, ControlError):
                raise
            raise ControlError(code, str(error), status=409) from error

    def _authorize(
        self, execution_id: str, principal: Principal, request: dict[str, Any]
    ) -> tuple[bool, str]:
        action = request["action"]
        if principal.role == "viewer":
            return False, "viewer role is read-only"
        if action == "attention":
            remedy = request["parameters"].get("remedy")
            if remedy not in ("retry", "release", "retain", "quarantine", "cancel"):
                return False, "attention remedy is not supported"
            attention_id = request["parameters"].get("attention_id")
            if not isinstance(attention_id, str) or not attention_id:
                return False, "attention_id is required"
            try:
                attention = self.ledger.attention(attention_id)
            except LedgerError:
                return False, "attention request does not exist"
            if attention["execution_id"] != execution_id:
                return False, "attention request belongs to another execution"
            if remedy == "release" and principal.role != "approver":
                return False, "release requires the approver role"
            if remedy == "release" and not request["confirmed"]:
                return False, "release requires explicit confirmation"
            return True, f"{principal.role} may issue attention {remedy}"
        state = request["expected_state"]
        target = request["parameters"].get("to_state")
        workflow, _states, execution_edges = self.kernel.graph_for_execution(execution_id)
        edges = [
            edge for edge in execution_edges
            if edge["from"] in (state, "@any_nonterminal")
            and {"actor": "human", "signal": "control_command"} in edge["evocations"]
            and edge.get("action", "transition") == action
            and (action != "transition" or edge["to"] == target)
        ]
        if not edges:
            return False, f"{action} is not configured from {state}"
        rank = {"viewer": 0, "operator": 1, "approver": 2}
        roles = {str(edge["required_role"]) for edge in edges if edge.get("required_role")}
        for role in roles:
            if role not in rank or rank[principal.role] < rank[role]:
                return False, f"{action} requires the {role} role"
        terminal = set(workflow["scope"]["terminal_states"])
        confirmation = any(edge.get("confirmation") is True for edge in edges)
        confirmation = confirmation or (
            action == "transition" and any(edge["to"] in terminal for edge in edges)
        )
        if confirmation and not request["confirmed"]:
            return False, f"{action} requires explicit confirmation"
        return True, f"{principal.role} may issue {action}"

    def _complete(
        self, execution_id: str, command_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        snapshot = self.ledger.run_snapshot(execution_id)
        result["run"] = _without_fences(snapshot)
        result["reconciliation"] = {
            "pending": snapshot["desired_linear_status"] != snapshot["observed_linear_status"]
            or snapshot["pending_projection_count"] > 0,
            "desired_linear_status": snapshot["desired_linear_status"],
            "observed_linear_status": snapshot["observed_linear_status"],
            "pending_projection_count": snapshot["pending_projection_count"],
        }
        return _without_fences(
            self.ledger.finish_control_command(command_id, result=result)
        )

    def _apply(
        self, execution_id: str, *, command_id: str, principal: Principal,
        request: dict[str, Any], current: dict[str, Any],
    ) -> dict[str, Any]:
        action = request["action"]
        parameters = request["parameters"]
        state = str(current["current_state_id"])
        if action == "attention":
            attention_id = parameters.get("attention_id")
            remedy = parameters.get("remedy")
            expected_attempt_id = parameters.get("expected_attempt_id")
            if not isinstance(attention_id, str) or not attention_id:
                raise ControlError("attention_id_required", "attention_id is required")
            if remedy not in ("retry", "release", "retain", "quarantine", "cancel"):
                raise ControlError("invalid_remedy", "attention remedy is not supported")
            if expected_attempt_id is not None and not isinstance(expected_attempt_id, str):
                raise ControlError(
                    "invalid_expected_attempt", "expected_attempt_id must be a string"
                )
            attention = self.ledger.attention(attention_id)
            controller = self.attention_controllers.get(
                str(attention.get("provider") or "")
            ) or self.resource_controller
            if controller is None:
                raise ControlError(
                    "attention_control_unavailable",
                    "attention control is not configured for this provider", status=409,
                )
            try:
                return controller.remedy_attention(
                    execution_id, attention_id=attention_id, remedy=remedy,
                    command_id=command_id,
                    expected_attempt_id=expected_attempt_id,
                )
            except LedgerError as error:
                raise ControlError(
                    "attention_action_rejected", str(error), status=409,
                ) from error
        eligible = _control_edges(self.ledger, self.kernel, execution_id, state)

        def action_edge(name: str) -> dict[str, Any]:
            matches = [
                edge for edge in eligible if edge.get("action", "transition") == name
            ]
            if len(matches) != 1:
                raise ControlError(
                    f"{name}_not_available",
                    f"{name} requires one eligible workflow edge", status=409,
                )
            return matches[0]

        if action == "cancel":
            edge = action_edge("cancel")
            return self._transition(
                execution_id, str(edge["to"]), command_id=command_id, current=current,
                outcome="canceled", evidence=[{
                    "kind": "decision", "uri": f"control://commands/{command_id}",
                    "reason": parameters.get("reason"),
                }],
            )
        if action == "approve":
            edge = action_edge("approve")
            note = parameters.get("note")
            if edge.get("requires_feedback") and (
                not isinstance(note, str) or not note.strip()
            ):
                raise ControlError("approval_note_required", "approval note is required")
            feedback = []
            if isinstance(note, str) and note.strip():
                feedback.append({
                    "source": "control_api",
                    "kind": str(edge.get("feedback_kind", "approval")),
                    "author": principal.subject,
                    "body": note.strip(),
                    "url": f"control://commands/{command_id}",
                })
            return self._transition(
                execution_id, str(edge["to"]), command_id=command_id, current=current,
                feedback=feedback,
            )
        if action == "retry":
            edge = action_edge("retry")
            owner = parameters.get("owner")
            target = str(edge["to"])
            _workflow, states, _edges = self.kernel.graph_for_execution(execution_id)
            target_kind = states[target]["kind"]
            if target_kind == "work" and (
                not isinstance(owner, str) or not owner.strip()
            ):
                raise ControlError("owner_required", "retry requires an owner")
            evidence = None
            outcome = None
            if current["attempt"]:
                outcome = "retry_requested"
                evidence = [{
                    "kind": "decision", "uri": f"control://commands/{command_id}",
                    "reason": parameters.get("reason"),
                }]
            return self._transition(
                execution_id, target, command_id=command_id, current=current,
                owner=owner.strip() if isinstance(owner, str) else None,
                outcome=outcome, evidence=evidence,
            )
        target = parameters.get("to_state")
        if not isinstance(target, str) or not target:
            raise ControlError("target_required", "transition requires to_state")
        allowed_edges = [
            edge for edge in eligible
            if edge.get("action", "transition") == "transition"
            and edge["to"] == target
        ]
        if len(allowed_edges) != 1:
            raise ControlError(
                "transition_not_authorized",
                "target is not an authorized control transition", status=409,
            )
        evidence = parameters.get("evidence")
        if evidence is not None and not isinstance(evidence, list):
            raise ControlError("invalid_evidence", "evidence must be an array")
        feedback = parameters.get("feedback")
        if feedback is not None and not isinstance(feedback, list):
            raise ControlError("invalid_feedback", "feedback must be an array")
        return self._transition(
            execution_id, target, command_id=command_id, current=current,
            owner=parameters.get("owner"), outcome=parameters.get("outcome"),
            evidence=evidence, feedback=feedback,
        )

    def _transition(
        self, execution_id: str, target: str, *, command_id: str,
        current: dict[str, Any], owner: str | None = None,
        outcome: str | None = None, evidence: list[dict[str, Any]] | None = None,
        feedback: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        attempt = current.get("attempt")
        return self.kernel.transition(
            execution_id, target, actor="human", signal="control_command",
            command_id=f"control:{command_id}", owner=owner,
            attempt_id=attempt["id"] if attempt else None,
            fence_token=attempt["fence_token"] if attempt else None,
            outcome=outcome, evidence=evidence or [], feedback=feedback or [],
        )
