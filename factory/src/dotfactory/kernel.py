"""Workflow-aware transactional boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .ledger import LedgerError, SQLiteLedger
from .workflow import WorkflowDefinition, load_workflow


class KernelError(LedgerError):
    pass


SUCCESS_LABELS = {"complete", "completed", "ready", "succeeded", "success"}
RESUME_CONDITION_PREFIX = "resume_state == "


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
        self, execution_id: str, from_state: str, to_state: str, *,
        actor: str, signal: str,
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
        if any(edge.get("condition") for edge in matches):
            resume_state = self._recorded_resume_state(
                execution_id, from_state, edges
            )
            matches = [
                edge for edge in matches
                if self._condition_matches(edge, resume_state)
            ]
        return matches

    def _recorded_resume_state(
        self, execution_id: str, state_id: str, edges: list[dict[str, Any]],
    ) -> str | None:
        history = self.ledger.run_history(execution_id)["state_runs"]
        if not history or history[-1]["state_id"] != state_id:
            raise KernelError("current state run does not match execution")
        recorded = history[-1].get("resume_state_id")
        if recorded:
            return str(recorded)
        origins = {
            str(edge["condition"])[len(RESUME_CONDITION_PREFIX):]
            for edge in edges
            if edge["from"] == state_id
            and str(edge.get("condition", "")).startswith(
                RESUME_CONDITION_PREFIX
            )
        }
        for previous, entered in reversed(list(zip(history, history[1:]))):
            if (
                entered["state_id"] == state_id
                and previous["state_id"] in origins
            ):
                return str(previous["state_id"])
        return None

    @staticmethod
    def _condition_matches(
        edge: dict[str, Any], resume_state: str | None,
    ) -> bool:
        condition = edge.get("condition")
        if not condition:
            return True
        value = str(condition)
        if not value.startswith(RESUME_CONDITION_PREFIX):
            return False
        return resume_state == value[len(RESUME_CONDITION_PREFIX):]

    def condition_matches(
        self, execution_id: str, edge: dict[str, Any],
    ) -> bool:
        condition = edge.get("condition")
        if not condition:
            return True
        if not str(condition).startswith(RESUME_CONDITION_PREFIX):
            return False
        current = self.ledger.current(execution_id)
        _workflow, _states, edges = self.graph_for_execution(execution_id)
        resume_state = self._recorded_resume_state(
            execution_id, str(current["current_state_id"]), edges
        )
        return self._condition_matches(edge, resume_state)

    def _next_resume_state(
        self, execution_id: str, *, from_state: str, to_state: str,
        edge: dict[str, Any], edges: list[dict[str, Any]], terminal: bool,
    ) -> str | None:
        if terminal or edge.get("condition"):
            return None
        origins = {
            str(candidate["condition"])[len(RESUME_CONDITION_PREFIX):]
            for candidate in edges
            if candidate["from"] == to_state
            and str(candidate.get("condition", "")).startswith(
                RESUME_CONDITION_PREFIX
            )
        }
        if from_state in origins:
            return from_state
        source_has_resume = any(
            candidate["from"] == from_state
            and str(candidate.get("condition", "")).startswith(
                RESUME_CONDITION_PREFIX
            )
            for candidate in edges
        )
        if not origins and not source_has_resume:
            return None
        return self._recorded_resume_state(execution_id, from_state, edges)

    def transition(
        self, execution_id: str, to_state: str, *, actor: str, signal: str,
        command_id: str, owner: str | None = None, attempt_id: str | None = None,
        fence_token: str | None = None, outcome: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        feedback: list[dict[str, Any]] | None = None,
        source_kind: str | None = None,
    ) -> dict[str, Any]:
        idempotency_key = f"execution:{execution_id}:transition:{command_id}"
        prior = self.ledger.decision_for_command(idempotency_key)
        if prior:
            return prior
        current = self.ledger.current(execution_id)
        from_state = current["current_state_id"]
        workflow, states, edges = self.graph_for_execution(execution_id)
        matches = self._matches(
            execution_id, from_state, to_state, actor=actor, signal=signal,
            workflow=workflow, edges=edges,
        )
        if len(matches) != 1:
            raise KernelError("no uniquely authorized workflow edge")
        target = states[to_state]
        terminal = set(workflow["scope"]["terminal_states"])
        edge = matches[0]
        policy = self._edge_policy(edge)
        stored_feedback_ids = self._stored_feedback_ids(execution_id, edge)
        resume_state_id = self._next_resume_state(
            execution_id, from_state=from_state, to_state=to_state,
            edge=edge, edges=edges, terminal=to_state in terminal,
        )
        return self.ledger.accept_transition(
            execution_id=execution_id, edge_id=edge["id"],
            from_state=from_state, to_state=to_state, to_kind=target["kind"],
            desired_linear_status=target["linear_status"], actor=actor, signal=signal,
            source_kind=source_kind,
            owner=owner, attempt_id=attempt_id, fence_token=fence_token,
            outcome=outcome, evidence=evidence or [], idempotency_key=idempotency_key,
            terminal=to_state in terminal, feedback=feedback or [],
            stored_feedback_ids=stored_feedback_ids,
            requires_feedback=policy["requires_feedback"],
            feedback_kind=policy["feedback_kind"],
            resolved_node=target.get("execution", {}),
            workflow_digest=workflow.get("workflow_digest", self.definition.digest),
            resume_state_id=resume_state_id,
        )

    def observe_linear_status(
        self, execution_id: str, observed_status: str, *, command_id: str,
        source_event_id: str | None = None, owner: str | None = None,
        attempt_id: str | None = None, fence_token: str | None = None,
        outcome: str | None = None, evidence: list[dict[str, Any]] | None = None,
        feedback: list[dict[str, Any]] | None = None,
        observation_id: str | None = None,
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
                observation_id=observation_id,
            )
        projected = [
            state_id for state_id, state in states.items()
            if state.get("linear_status") == observed_status
        ]
        candidates = []
        for state_id in projected:
            candidates.extend(self._matches(
                execution_id, from_state, state_id, actor="human",
                signal="linear_status_change",
                workflow=workflow, edges=edges,
            ))
        if not projected:
            return self.ledger.record_linear_observation(
                execution_id, observed_status=observed_status, actor="human",
                disposition="rejected", reason="status is not in the workflow contract",
                idempotency_key=idempotency_key, source_event_id=source_event_id,
                feedback=[], feedback_allowed=False,
                observation_id=observation_id,
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
                observation_id=observation_id,
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
        resume_state_id = self._next_resume_state(
            execution_id, from_state=from_state, to_state=target_state,
            edge=edge, edges=edges, terminal=target_state in terminal,
        )
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
                    observation_id=observation_id,
                )
            except LedgerError as error:
                return self.ledger.record_linear_observation(
                    execution_id, observed_status=observed_status, actor="human",
                    disposition="rejected", reason=str(error),
                    idempotency_key=idempotency_key, source_event_id=source_event_id,
                    feedback=[], feedback_allowed=False,
                    observation_id=observation_id,
                )
        try:
            return self.ledger.accept_transition(
                execution_id=execution_id, edge_id=edge["id"],
                from_state=from_state, to_state=target_state,
                to_kind=target["kind"], desired_linear_status=target["linear_status"],
                actor="human", signal="linear_status_change", owner=owner,
                source_kind="human",
                attempt_id=attempt_id, fence_token=fence_token, outcome=outcome,
                evidence=evidence or [], idempotency_key=idempotency_key,
                terminal=target_state in terminal, feedback=observation_feedback,
                observed_linear_status=observed_status, observation=observation,
                stored_feedback_ids=stored_feedback_ids,
                requires_feedback=policy["requires_feedback"],
                feedback_kind=policy["feedback_kind"],
                resolved_node=target.get("execution", {}),
                workflow_digest=workflow.get("workflow_digest", self.definition.digest),
                observation_id=observation_id,
                resume_state_id=resume_state_id,
            )
        except LedgerError as error:
            return self.ledger.record_linear_observation(
                execution_id, observed_status=observed_status, actor="human",
                disposition="rejected", reason=str(error),
                idempotency_key=idempotency_key, source_event_id=source_event_id,
                feedback=[], feedback_allowed=False,
                observation_id=observation_id,
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
        if any(edge.get("condition") for edge in candidates):
            resume_state = self._recorded_resume_state(
                execution_id, state_id, edges
            )
            candidates = [
                edge for edge in candidates
                if self._condition_matches(edge, resume_state)
            ]
        candidates_with_signals = [
            (
                edge,
                sorted({
                    str(evocation["signal"])
                    for evocation in edge["evocations"]
                    if evocation.get("actor") == "agent"
                }),
            )
            for edge in candidates
            if any(
                evocation.get("actor") == "agent"
                for evocation in edge["evocations"]
            )
        ]
        if (
            len(candidates_with_signals) != 1
            or len(candidates_with_signals[0][1]) != 1
        ):
            raise KernelError(f"no unique agent edge for outcome label {label}")
        edge, signals = candidates_with_signals[0]
        target = str(edge["to"])
        signal = signals[0]
        target_owner = owner if states[target]["kind"] == "work" else None
        return self.transition(
            execution_id, target, actor="agent", signal=signal,
            command_id=command_id, owner=target_owner, attempt_id=attempt_id,
            fence_token=fence_token, outcome=outcome, evidence=evidence,
            source_kind="agent",
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
        if not self.condition_matches(execution_id, edge):
            raise KernelError("pending human transition condition no longer matches")
        policy = self._edge_policy(edge)
        resume_state_id = self._next_resume_state(
            execution_id, from_state=str(request["from_state"]),
            to_state=str(request["to_state"]), edge=edge, edges=edges,
            terminal=request["to_state"] in terminal,
        )
        return self.ledger.accept_transition(
            execution_id=execution_id, edge_id=request["edge_id"],
            from_state=request["from_state"], to_state=request["to_state"],
            to_kind=target["kind"], desired_linear_status=target["linear_status"],
            actor=request["actor"], signal=request["signal"], owner=owner,
            source_kind="human",
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
            resume_state_id=resume_state_id,
        )
