"""Workflow-aware transactional boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .ledger import LedgerError, SQLiteLedger
from .workflow import WorkflowDefinition, load_workflow


class KernelError(LedgerError):
    pass


SUCCESS_LABELS = {"complete", "completed", "ready", "succeeded", "success"}


class DurableKernel:
    def __init__(
        self, ledger: SQLiteLedger, workflow_path: str | Path,
        *, profile_paths: list[str | Path] | None = None,
        factory_defaults: dict[str, Any] | None = None,
    ) -> None:
        self.ledger = ledger
        self.definition: WorkflowDefinition = load_workflow(
            workflow_path, profile_paths=profile_paths or (),
            factory_defaults=factory_defaults,
        )
        self.workflow = self.definition.as_contract()
        self.states = {item["id"]: item for item in self.workflow["states"]}
        self.edges = self.workflow["transitions"] + self.workflow["global_transitions"]

    def graph_for_execution(
        self, execution_id: str
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Return the immutable graph bound when this execution began."""
        snapshot = self.ledger.workflow_snapshot(execution_id)
        if not snapshot or snapshot["digest"] == self.definition.digest:
            return self.workflow, self.states, self.edges
        workflow = dict(snapshot["normalized"])
        workflow["workflow_digest"] = snapshot["digest"]
        states = {item["id"]: item for item in workflow["states"]}
        edges = workflow["transitions"] + workflow["global_transitions"]
        return workflow, states, edges

    def _edge_policy(self, edge: dict[str, Any]) -> dict[str, Any]:
        return {
            "requires_feedback": edge.get("requires_feedback") is True,
            "feedback_kind": edge.get("feedback_kind"),
        }

    def _stored_feedback_ids(
        self, execution_id: str, edge: dict[str, Any]
    ) -> list[str]:
        if not edge.get("requires_feedback"):
            return []
        return [
            item["id"] for item in self.ledger.current_state_feedback(execution_id)
        ]

    def _allows_state_feedback(
        self, state_id: str, edges: list[dict[str, Any]]
    ) -> bool:
        return any(
            edge["from"] == state_id and edge.get("requires_feedback") is True
            for edge in edges
        )

    def begin(
        self, project_key: str, identifier: str, intent: dict[str, Any], *,
        command_id: str, owner: str | None = None, actor: str = "agent",
    ) -> str:
        idempotency_key = (
            f"project:{project_key}:work:{identifier}:begin:{command_id}"
        )
        prior = self.ledger.event_for_command(idempotency_key)
        if prior:
            return str(prior["execution_id"])
        state_id = self.workflow["scope"]["entry_state"]
        state = self.states[state_id]
        return self.ledger.begin_execution(
            project_key=project_key, identifier=identifier, intent=intent,
            workflow_name=self.workflow["name"],
            workflow_version=self.workflow["schema_version"], state_id=state_id,
            state_kind=state["kind"], linear_status=state["linear_status"],
            workflow_snapshot=self.definition.snapshot(),
            resolved_node=state.get("execution", {}),
            owner=owner, actor=actor,
            idempotency_key=idempotency_key,
        )

    def _matches(
        self, from_state: str, to_state: str, *, actor: str, signal: str,
        workflow: dict[str, Any], edges: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        terminal = set(workflow["scope"]["terminal_states"])
        matches = []
        for edge in edges:
            source_matches = edge["from"] == from_state or (
                edge["from"] == "@any_nonterminal" and from_state not in terminal
            )
            if source_matches and edge["to"] == to_state:
                if {"actor": actor, "signal": signal} in edge["evocations"]:
                    matches.append(edge)
        return matches

    def transition(
        self, execution_id: str, to_state: str, *, actor: str, signal: str,
        command_id: str, owner: str | None = None, attempt_id: str | None = None,
        fence_token: str | None = None, outcome: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        feedback: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        idempotency_key = f"execution:{execution_id}:transition:{command_id}"
        prior = self.ledger.decision_for_command(idempotency_key)
        if prior:
            return prior
        current = self.ledger.current(execution_id)
        from_state = current["current_state_id"]
        workflow, states, edges = self.graph_for_execution(execution_id)
        matches = self._matches(
            from_state, to_state, actor=actor, signal=signal,
            workflow=workflow, edges=edges,
        )
        if len(matches) != 1:
            raise KernelError("no uniquely authorized workflow edge")
        target = states[to_state]
        terminal = set(workflow["scope"]["terminal_states"])
        edge = matches[0]
        policy = self._edge_policy(edge)
        stored_feedback_ids = self._stored_feedback_ids(execution_id, edge)
        return self.ledger.accept_transition(
            execution_id=execution_id, edge_id=edge["id"],
            from_state=from_state, to_state=to_state, to_kind=target["kind"],
            desired_linear_status=target["linear_status"], actor=actor, signal=signal,
            owner=owner, attempt_id=attempt_id, fence_token=fence_token,
            outcome=outcome, evidence=evidence or [], idempotency_key=idempotency_key,
            terminal=to_state in terminal, feedback=feedback or [],
            stored_feedback_ids=stored_feedback_ids,
            requires_feedback=policy["requires_feedback"],
            feedback_kind=policy["feedback_kind"],
            resolved_node=target.get("execution", {}),
            workflow_digest=workflow.get("workflow_digest", self.definition.digest),
        )

    def observe_linear_status(
        self, execution_id: str, observed_status: str, *, command_id: str,
        source_event_id: str | None = None, owner: str | None = None,
        attempt_id: str | None = None, fence_token: str | None = None,
        outcome: str | None = None, evidence: list[dict[str, Any]] | None = None,
        feedback: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        idempotency_key = f"execution:{execution_id}:linear:{command_id}"
        prior_decision = self.ledger.decision_for_command(idempotency_key)
        if prior_decision:
            return prior_decision
        prior = self.ledger.event_for_command(idempotency_key)
        if prior:
            return prior
        current = self.ledger.current(execution_id)
        from_state = current["current_state_id"]
        workflow, states, edges = self.graph_for_execution(execution_id)
        observation_feedback = list(feedback or [])
        current_projection = states[from_state].get("linear_status")
        if observed_status == current_projection:
            return self.ledger.record_linear_observation(
                execution_id, observed_status=observed_status, actor="human",
                disposition="no_change", reason="Linear already matches the ledger",
                idempotency_key=idempotency_key, source_event_id=source_event_id,
                feedback=observation_feedback,
                feedback_allowed=self._allows_state_feedback(from_state, edges),
            )
        projected = [
            state_id for state_id, state in states.items()
            if state.get("linear_status") == observed_status
        ]
        candidates = []
        for state_id in projected:
            candidates.extend(self._matches(
                from_state, state_id, actor="human", signal="linear_status_change",
                workflow=workflow, edges=edges,
            ))
        if not projected:
            return self.ledger.record_linear_observation(
                execution_id, observed_status=observed_status, actor="human",
                disposition="rejected", reason="status is not in the workflow contract",
                idempotency_key=idempotency_key, source_event_id=source_event_id,
                feedback=[], feedback_allowed=False,
            )
        if len(candidates) != 1:
            reason = (
                "ambiguous Linear status projection"
                if len(candidates) > 1 else "no uniquely authorized human transition"
            )
            return self.ledger.record_linear_observation(
                execution_id, observed_status=observed_status, actor="human",
                disposition="rejected", reason=reason,
                idempotency_key=idempotency_key, source_event_id=source_event_id,
                feedback=[], feedback_allowed=False,
            )
        edge = candidates[0]
        target_state = str(edge["to"])
        target = states[target_state]
        terminal = set(workflow["scope"]["terminal_states"])
        observation = {
            "source": "linear",
            "source_event_id": source_event_id,
            "observed_status": observed_status,
        }
        policy = self._edge_policy(edge)
        stored_feedback_ids = self._stored_feedback_ids(execution_id, edge)
        if target["kind"] == "work" and target_state not in terminal and not owner:
            try:
                return self.ledger.defer_transition(
                    execution_id=execution_id, edge_id=edge["id"],
                    from_state=from_state, to_state=target_state,
                    actor="human", signal="linear_status_change",
                    feedback=observation_feedback, idempotency_key=idempotency_key,
                    observed_linear_status=observed_status,
                    source_event_id=source_event_id,
                    requires_feedback=policy["requires_feedback"],
                    feedback_kind=policy["feedback_kind"],
                )
            except LedgerError as error:
                return self.ledger.record_linear_observation(
                    execution_id, observed_status=observed_status, actor="human",
                    disposition="rejected", reason=str(error),
                    idempotency_key=idempotency_key, source_event_id=source_event_id,
                    feedback=[], feedback_allowed=False,
                )
        try:
            return self.ledger.accept_transition(
                execution_id=execution_id, edge_id=edge["id"],
                from_state=from_state, to_state=target_state,
                to_kind=target["kind"], desired_linear_status=target["linear_status"],
                actor="human", signal="linear_status_change", owner=owner,
                attempt_id=attempt_id, fence_token=fence_token, outcome=outcome,
                evidence=evidence or [], idempotency_key=idempotency_key,
                terminal=target_state in terminal, feedback=observation_feedback,
                observed_linear_status=observed_status, observation=observation,
                stored_feedback_ids=stored_feedback_ids,
                requires_feedback=policy["requires_feedback"],
                feedback_kind=policy["feedback_kind"],
                resolved_node=target.get("execution", {}),
                workflow_digest=workflow.get("workflow_digest", self.definition.digest),
            )
        except LedgerError as error:
            return self.ledger.record_linear_observation(
                execution_id, observed_status=observed_status, actor="human",
                disposition="rejected", reason=str(error),
                idempotency_key=idempotency_key, source_event_id=source_event_id,
                feedback=[], feedback_allowed=False,
            )

    def complete_attempt(
        self, execution_id: str, *, preferred_label: str, outcome: str,
        evidence: list[dict[str, Any]], attempt_id: str, fence_token: str,
        owner: str, command_id: str,
    ) -> dict[str, Any]:
        current = self.ledger.current(execution_id)
        state_id = str(current["current_state_id"])
        workflow, states, edges = self.graph_for_execution(execution_id)
        execution = states[state_id].get("execution", {})
        label = preferred_label
        if preferred_label == "retry" and "max_retries" in execution:
            visits = sum(
                1 for item in self.ledger.run_history(execution_id)["state_runs"]
                if item["state_id"] == state_id
            )
            if visits > int(execution["max_retries"]):
                candidates = [
                    edge for edge in edges
                    if edge["from"] == state_id and edge.get("on") == "exhausted"
                ]
                label = "exhausted"
            else:
                candidates = [
                    edge for edge in edges
                    if edge["from"] == state_id and edge.get("on") == label
                ]
        else:
            candidates = [
                edge for edge in edges
                if edge["from"] == state_id and edge.get("on") == label
            ]
        if not candidates and preferred_label in SUCCESS_LABELS:
            candidates = [
                edge for edge in edges
                if edge["from"] == state_id and not edge.get("on")
            ]
        candidates = [
            edge for edge in candidates
            if {"actor": "agent", "signal": "agent_handoff"} in edge["evocations"]
        ]
        if len(candidates) != 1:
            raise KernelError(f"no unique agent edge for outcome label {label}")
        target = str(candidates[0]["to"])
        target_owner = owner if states[target]["kind"] == "work" else None
        return self.transition(
            execution_id, target, actor="agent", signal="agent_handoff",
            command_id=command_id, owner=target_owner, attempt_id=attempt_id,
            fence_token=fence_token, outcome=outcome, evidence=evidence,
        )

    def claim_pending_transition(
        self, execution_id: str, *, owner: str, command_id: str
    ) -> dict[str, Any]:
        idempotency_key = f"execution:{execution_id}:claim-pending:{command_id}"
        prior = self.ledger.decision_for_command(idempotency_key)
        if prior:
            return prior
        request = self.ledger.pending_transition(execution_id)
        if not request:
            raise KernelError("execution has no pending human transition")
        current = self.ledger.current(execution_id)
        if current["current_state_id"] != request["from_state"]:
            raise KernelError("pending human transition is stale")
        workflow, states, edges = self.graph_for_execution(execution_id)
        target = states[request["to_state"]]
        terminal = set(workflow["scope"]["terminal_states"])
        edge = next(
            item for item in edges if item["id"] == request["edge_id"]
        )
        policy = self._edge_policy(edge)
        return self.ledger.accept_transition(
            execution_id=execution_id, edge_id=request["edge_id"],
            from_state=request["from_state"], to_state=request["to_state"],
            to_kind=target["kind"], desired_linear_status=target["linear_status"],
            actor=request["actor"], signal=request["signal"], owner=owner,
            attempt_id=None, fence_token=None, outcome=None, evidence=[],
            idempotency_key=idempotency_key,
            terminal=request["to_state"] in terminal, feedback=[],
            observed_linear_status=request["observed_linear_status"],
            observation={
                "source": "linear", "source_event_id": request["source_event_id"],
                "observed_status": request["observed_linear_status"],
                "transition_request_id": request["id"],
            },
            stored_feedback_ids=request["feedback_ids"],
            transition_request_id=request["id"],
            requires_feedback=policy["requires_feedback"],
            feedback_kind=policy["feedback_kind"],
            resolved_node=target.get("execution", {}),
            workflow_digest=workflow.get("workflow_digest", self.definition.digest),
        )
