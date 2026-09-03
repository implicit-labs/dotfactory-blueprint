"""Typed workflow loading, resolution, validation, and snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .dot import DotEdge, DotGraph, DotSyntaxError, LocatedAttributes, load_dot


class WorkflowError(ValueError):
    pass


NODE_TYPES = {"agent", "checkpoint", "human", "start", "terminal", "tool", "work"}
WORK_TYPES = {"agent", "tool", "work"}
CHECKPOINT_TYPES = {"checkpoint", "human", "terminal"}
NODE_ATTRIBUTES = {
    "capabilities", "checkpoint_role", "evidence", "exit_contract", "kind",
    "label", "linear_status", "max_retries", "meaning", "model", "prompt",
    "profile", "reasoning_effort", "resources", "role", "runner", "shape", "skills",
    "timeout", "type", "work_role",
}
EDGE_ATTRIBUTES = {
    "action", "authority", "condition", "confirmation", "evocations",
    "feedback_kind", "id", "meaning", "on", "required_role",
    "requires_evidence", "requires_feedback", "requires_outcome", "signal",
    "weight",
}
GRAPH_ATTRIBUTES = {
    "conventions", "entry", "external_linear_states", "goal",
    "linear_statuses", "main_checkpoints", "name", "schema_version",
}
PROFILE_ATTRIBUTES = NODE_ATTRIBUTES - {
    "checkpoint_role", "kind", "label", "linear_status", "meaning", "role",
    "profile", "shape", "type", "work_role",
}
BOOLEAN_ATTRIBUTES = {
    "confirmation", "requires_evidence", "requires_feedback", "requires_outcome"
}
INTEGER_ATTRIBUTES = {"max_retries", "weight"}
LIST_ATTRIBUTES = {"capabilities", "resources", "skills"}
DURATION = re.compile(r"^[1-9][0-9]*(ms|s|m|h)$")


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    schema_version: int
    states: tuple[dict[str, Any], ...]
    transitions: tuple[dict[str, Any], ...]
    global_transitions: tuple[dict[str, Any], ...]
    scope: dict[str, Any]
    source_format: str
    source_text: str
    normalized: dict[str, Any]
    digest: str

    def as_contract(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "semantics": {
                "checkpoint": {
                    "can_rest": True,
                    "requires_active_attempt": False,
                    "meaning": "A durable condition that may wait without an active attempt.",
                },
                "work": {
                    "can_rest": False,
                    "requires_active_attempt": True,
                    "required_attempt_fields": [
                        "owner", "attempt_id", "started_at", "heartbeat_at"
                    ],
                    "meaning": "An owned operation that must complete, recover, or block.",
                },
                "ordinary_comments": "context_only",
            },
            "scope": self.scope,
            "states": [dict(item) for item in self.states],
            "transitions": [dict(item) for item in self.transitions],
            "global_transitions": [dict(item) for item in self.global_transitions],
            "workflow_digest": self.digest,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schema_version": self.schema_version,
            "source_format": self.source_format,
            "source_text": self.source_text,
            "normalized": self.normalized,
            "digest": self.digest,
        }


def _error(graph: DotGraph, line: int, column: int, message: str) -> WorkflowError:
    return WorkflowError(f"{graph.source_name}:{line}:{column}: {message}")


def _unknown_attributes(
    graph: DotGraph, values: dict[str, str], allowed: set[str], *,
    line: int, column: int, subject: str,
) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise _error(
            graph, line, column,
            f"{subject} has unknown attribute {unknown[0]!r}; remove it or add it to the schema",
        )


def _boolean(graph: DotGraph, key: str, value: str, line: int, column: int) -> bool:
    if value not in ("true", "false"):
        raise _error(graph, line, column, f"{key} must be true or false")
    return value == "true"


def _integer(graph: DotGraph, key: str, value: str, line: int, column: int) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise _error(graph, line, column, f"{key} must be an integer") from error
    if parsed < 0:
        raise _error(graph, line, column, f"{key} must be zero or greater")
    return parsed


def _list(value: str) -> list[str]:
    if not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _typed_attributes(
    graph: DotGraph, values: dict[str, str], *, line: int, column: int,
) -> dict[str, Any]:
    typed: dict[str, Any] = {}
    for key, value in values.items():
        if key in BOOLEAN_ATTRIBUTES:
            typed[key] = _boolean(graph, key, value, line, column)
        elif key in INTEGER_ATTRIBUTES:
            typed[key] = _integer(graph, key, value, line, column)
        elif key in LIST_ATTRIBUTES:
            typed[key] = _list(value)
        else:
            typed[key] = value
    if "timeout" in typed and not DURATION.fullmatch(str(typed["timeout"])):
        raise _error(graph, line, column, "timeout must be a positive duration such as 30m")
    return typed


def _load_profiles(paths: Iterable[str | Path]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for path in paths:
        resolved = Path(path)
        values = json.loads(resolved.read_text(encoding="utf-8"))
        if values.get("schema_version") != 1 or not isinstance(values.get("profiles"), dict):
            raise WorkflowError(f"{resolved}: profiles require schema_version 1 and profiles")
        for name, profile in values["profiles"].items():
            if name in profiles:
                raise WorkflowError(f"{resolved}: duplicate profile {name}")
            if not isinstance(profile, dict):
                raise WorkflowError(f"{resolved}: profile {name} must be an object")
            unknown = sorted(set(profile) - PROFILE_ATTRIBUTES)
            if unknown:
                raise WorkflowError(f"{resolved}: profile {name} has unknown attribute {unknown[0]}")
            for key in LIST_ATTRIBUTES & set(profile):
                if not isinstance(profile[key], list) or not all(
                    isinstance(item, str) and item for item in profile[key]
                ):
                    raise WorkflowError(f"{resolved}: profile {name} {key} must be a list of strings")
            if "max_retries" in profile and (
                isinstance(profile["max_retries"], bool)
                or not isinstance(profile["max_retries"], int)
                or profile["max_retries"] < 0
            ):
                raise WorkflowError(
                    f"{resolved}: profile {name} max_retries must be a non-negative integer"
                )
            if "timeout" in profile and not DURATION.fullmatch(str(profile["timeout"])):
                raise WorkflowError(
                    f"{resolved}: profile {name} timeout must be a positive duration such as 30m"
                )
            profiles[name] = dict(profile)
    return profiles


def _node_type(values: dict[str, Any]) -> str:
    shape = values.get("shape")
    if shape == "Mdiamond":
        return "start"
    if shape == "Msquare":
        return "terminal"
    return str(values.get("type", "checkpoint"))


def _state_kind(node_type: str, values: dict[str, Any]) -> str:
    explicit = values.get("kind")
    if explicit:
        if explicit not in ("work", "checkpoint"):
            raise WorkflowError("kind must be work or checkpoint")
        return str(explicit)
    if node_type in WORK_TYPES:
        return "work"
    return "checkpoint"


def _actors(source_type: str, values: dict[str, Any]) -> list[str]:
    actors = _list(str(values.get("authority", "")))
    if actors:
        return actors
    if values.get("on") in {"approve", "cancel", "enter", "resume", "revise"}:
        return ["human"]
    if source_type in {"agent", "tool", "work"}:
        return ["agent"]
    return ["human"]


def _linear_evocations(actors: list[str], on: str) -> list[dict[str, str]]:
    unknown = sorted(set(actors) - {"agent", "human"})
    if unknown:
        raise WorkflowError(f"linear convention does not support authority {unknown[0]}")
    evocations: list[dict[str, str]] = []
    if "human" in actors:
        evocations.append({"actor": "human", "signal": "linear_status_change"})
    if "agent" in actors:
        agent_signal = {
            "claim": "listener_claim",
            "failed": "recovery_requested",
        }.get(on, "agent_handoff")
        evocations.append({"actor": "agent", "signal": agent_signal})
    if "human" in actors and on in {"resume", "revise"}:
        evocations.append({"actor": "human", "signal": "structured_comment"})
    if "human" in actors and on != "enter":
        evocations.append({"actor": "human", "signal": "control_command"})
    return evocations


def _evocations(
    source_type: str, values: dict[str, Any], *, linear_convention: bool,
) -> list[dict[str, str]]:
    if values.get("evocations"):
        pairs = []
        for item in re.split(r"[|,]", str(values["evocations"])):
            actor, separator, signal = item.strip().partition(":")
            if not separator or not actor or not signal:
                raise WorkflowError("evocations must use actor:signal pairs")
            pairs.append({"actor": actor, "signal": signal})
        return pairs
    actors = _list(str(values.get("authority", "")))
    signals = _list(str(values.get("signal", "")))
    if actors and signals:
        return [{"actor": actor, "signal": signal} for actor in actors for signal in signals]
    if linear_convention and values.get("on"):
        return _linear_evocations(_actors(source_type, values), str(values["on"]))
    defaults = {
        "agent": ("agent", "agent_handoff"),
        "tool": ("agent", "agent_handoff"),
        "work": ("agent", "agent_handoff"),
        "human": ("human", "control_command"),
        "checkpoint": ("human", "control_command"),
        "condition": ("agent", "agent_handoff"),
        "start": ("agent", "listener_claim"),
    }
    actor, signal = defaults.get(source_type, ("human", "control_command"))
    return [{"actor": actor, "signal": signal}]


def _resume_edges(
    graph: DotGraph, raw_types: dict[str, str], *, linear_convention: bool,
) -> list[DotEdge]:
    expanded: list[DotEdge] = []
    for edge in graph.edges:
        if edge.target != "@resume":
            expanded.append(edge)
            continue
        if not linear_convention:
            raise _error(
                graph, edge.line, edge.column,
                "@resume requires graph convention linear",
            )
        if edge.attributes.values.get("on") != "retry":
            raise _error(
                graph, edge.line, edge.column,
                "@resume requires on=retry",
            )
        if "condition" in edge.attributes.values:
            raise _error(
                graph, edge.line, edge.column,
                "@resume generates its condition; remove condition",
            )
        failures = [
            candidate for candidate in graph.edges
            if candidate.target == edge.source
            and candidate.attributes.values.get("on") == "failed"
            and not candidate.source.startswith("@")
        ]
        if not failures:
            raise _error(
                graph, edge.line, edge.column,
                f"@resume from {edge.source} requires an incoming on=failed edge",
            )
        seen: set[str] = set()
        for failure in failures:
            if failure.source in seen:
                raise _error(
                    graph, edge.line, edge.column,
                    f"@resume is ambiguous: multiple on=failed edges from {failure.source}",
                )
            seen.add(failure.source)
            values = dict(edge.attributes.values)
            values["condition"] = f"resume_state == {failure.source}"
            if "authority" not in values and "evocations" not in values:
                failure_values = dict(failure.attributes.values)
                failure_evocations = _evocations(
                    raw_types.get(failure.source, "checkpoint"),
                    failure_values,
                    linear_convention=True,
                )
                actors = list(dict.fromkeys(
                    item["actor"] for item in failure_evocations
                ))
                values["evocations"] = "|".join(
                    f"{item['actor']}:{item['signal']}"
                    for item in _linear_evocations(actors, "retry")
                )
            expanded.append(DotEdge(
                edge.source,
                failure.source,
                LocatedAttributes(
                    values, edge.attributes.line, edge.attributes.column
                ),
                edge.line,
                edge.column,
            ))
    return expanded


def _edge_slug(value: str) -> str:
    plain = value.lstrip("@").replace("-", "_")
    plain = re.sub(r"(?<!^)(?=[A-Z])", "_", plain).lower()
    return re.sub(r"[^a-z0-9_]+", "_", plain).strip("_") or "edge"


def _generated_edge_id(source: str, target: str, used: set[str]) -> str:
    base = f"{_edge_slug(source)}.{_edge_slug(target)}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}.{suffix}"
        suffix += 1
    return candidate


def _validate_reverse_linear_routes(
    states: list[dict[str, Any]], edges: list[dict[str, Any]],
) -> None:
    """A human status observation must select at most one edge per source."""
    state_by_id = {str(item["id"]): item for item in states}
    terminal = {
        state_id for state_id, item in state_by_id.items()
        if item["node_type"] == "terminal"
    }
    for source in sorted(state_by_id):
        by_status: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            source_matches = edge["from"] == source or (
                edge["from"] == "@any_nonterminal" and source not in terminal
            )
            if not source_matches or {
                "actor": "human", "signal": "linear_status_change"
            } not in edge["evocations"]:
                continue
            target = state_by_id[str(edge["to"])]
            status = str(target.get("linear_status", ""))
            if not status:
                raise WorkflowError(
                    f"human Linear route {edge['id']} targets a state without linear_status"
                )
            by_status[status].append(str(edge["id"]))
        for status, edge_ids in sorted(by_status.items()):
            if len(edge_ids) > 1:
                raise WorkflowError(
                    "ambiguous human Linear reverse route from "
                    f"{source} for status {status}: {', '.join(sorted(edge_ids))}"
                )


def _state_definition_v1(
    state: dict[str, Any], edges: list[dict[str, Any]],
) -> dict[str, Any]:
    state_id = str(state["id"])
    incoming = sorted(
        (edge for edge in edges if edge["to"] == state_id),
        key=lambda item: str(item["id"]),
    )
    outgoing = sorted(
        (
            edge for edge in edges
            if edge["from"] == state_id or edge["from"] == "@any_nonterminal"
        ),
        key=lambda item: str(item["id"]),
    )
    execution = dict(state.get("execution", {}))
    entry_guards = []
    for edge in incoming:
        guard = {"edge_id": edge["id"], "from": edge["from"]}
        for key in (
            "condition", "confirmation", "required_role", "requires_evidence",
            "requires_feedback", "requires_outcome",
        ):
            if key in edge:
                guard[key] = edge[key]
        entry_guards.append(guard)
    retry_edges = [
        edge["id"] for edge in outgoing
        if edge.get("on") == "retry" or edge.get("action") == "retry"
    ]
    exhausted_edges = [
        edge["id"] for edge in outgoing if edge.get("on") == "exhausted"
    ]
    return {
        "version": 1,
        "id": state_id,
        "node_type": state["node_type"],
        "kind": state["kind"],
        "linear_status": state.get("linear_status") or None,
        "entry_guards": entry_guards,
        "exit_contract": execution.get("exit_contract"),
        "retry_policy": {
            "max_retries": execution.get("max_retries"),
            "retry_edge_ids": retry_edges,
            "exhausted_edge_ids": exhausted_edges,
        },
        "evidence_policy": {
            "declared": execution.get("evidence"),
            "required_exit_edge_ids": [
                edge["id"] for edge in outgoing
                if edge.get("requires_evidence") is True
            ],
        },
        "runner_policy": {
            key: execution[key] for key in (
                "profile", "runner", "model", "reasoning_effort", "timeout"
            ) if key in execution
        },
        "skills": list(execution.get("skills", [])),
        "capabilities": list(execution.get("capabilities", [])),
        "resources": list(execution.get("resources", [])),
        "config_sources": dict(state.get("config_sources", {})),
    }


def compile_dot(
    graph: DotGraph, *, profile_paths: Iterable[str | Path] = (),
    factory_defaults: dict[str, Any] | None = None,
) -> WorkflowDefinition:
    try:
        schema_version = int(graph.attributes.get("schema_version", "1"))
    except ValueError as error:
        raise WorkflowError("schema_version must be an integer") from error
    if schema_version not in {1, 2}:
        raise WorkflowError(
            f"unsupported DOT workflow schema_version {schema_version}; supported: 1, 2"
        )
    _unknown_attributes(
        graph, graph.attributes, GRAPH_ATTRIBUTES,
        line=1, column=1, subject="graph",
    )
    if schema_version == 1 and (
        "conventions" in graph.attributes or "linear_statuses" in graph.attributes
    ):
        raise WorkflowError(
            "conventions and linear_statuses require DOT workflow schema_version 2"
        )
    conventions = set(_list(graph.attributes.get("conventions", "")))
    unsupported_conventions = sorted(conventions - {"linear"})
    if unsupported_conventions:
        raise WorkflowError(
            f"unsupported workflow convention {unsupported_conventions[0]}"
        )
    linear_convention = "linear" in conventions
    linear_statuses = graph.attributes.get("linear_statuses", "explicit")
    if linear_statuses not in {"explicit", "node_ids"}:
        raise WorkflowError("linear_statuses must be explicit or node_ids")
    profiles = _load_profiles(profile_paths)
    factory_defaults = dict(factory_defaults or {})
    unknown_defaults = sorted(set(factory_defaults) - PROFILE_ATTRIBUTES)
    if unknown_defaults:
        raise WorkflowError(f"factory defaults have unknown attribute {unknown_defaults[0]}")
    _unknown_attributes(
        graph, graph.node_defaults, NODE_ATTRIBUTES,
        line=1, column=1, subject="workflow node defaults",
    )
    workflow_defaults = _typed_attributes(
        graph, graph.node_defaults, line=1, column=1
    )
    raw_types: dict[str, str] = {}
    states: list[dict[str, Any]] = []
    start_nodes: list[str] = []
    for node_id, located in graph.nodes.items():
        _unknown_attributes(
            graph, located.values, NODE_ATTRIBUTES,
            line=located.line, column=located.column, subject=f"node {node_id}",
        )
        local = _typed_attributes(
            graph, located.values, line=located.line, column=located.column
        )
        profile_name = local.get("profile", workflow_defaults.get("profile"))
        profile = profiles.get(str(profile_name), {}) if profile_name else {}
        if profile_name and not profile:
            raise _error(
                graph, located.line, located.column,
                f"node {node_id} references unknown profile {profile_name}",
            )
        values = dict(factory_defaults)
        sources = {key: "factory" for key in values}
        for key, value in workflow_defaults.items():
            values[key] = value
            sources[key] = "workflow"
        for key, value in profile.items():
            values[key] = value
            sources[key] = f"profile:{profile_name}"
        for key, value in local.items():
            values[key] = value
            sources[key] = "node"
        node_type = _node_type(values)
        if node_type not in NODE_TYPES:
            raise _error(
                graph, located.line, located.column,
                f"node {node_id} has unsupported type {node_type}",
            )
        raw_types[node_id] = node_type
        if node_type == "start":
            start_nodes.append(node_id)
            continue
        kind = _state_kind(node_type, values)
        state: dict[str, Any] = {
            "id": node_id,
            "label": str(values.get("label", node_id)),
            "node_type": node_type,
            "kind": kind,
            "linear_status": str(values.get(
                "linear_status", node_id if linear_statuses == "node_ids" else ""
            )),
            "meaning": str(values.get("meaning", "")),
        }
        role = values.get("role") or values.get(
            "work_role" if kind == "work" else "checkpoint_role"
        )
        if role:
            state["work_role" if kind == "work" else "checkpoint_role"] = str(role)
        elif kind == "work":
            state["work_role"] = node_type
        else:
            state["checkpoint_role"] = "terminal" if node_type == "terminal" else node_type
        execution = {
            key: value for key, value in values.items()
            if key in PROFILE_ATTRIBUTES and key != "profile"
        }
        if profile_name:
            execution["profile"] = profile_name
        if execution:
            prompt = execution.get("prompt")
            if prompt and graph.source_name != "<dot>":
                prompt_path = Path(graph.source_name).parent / str(prompt)
                if not prompt_path.is_file():
                    raise _error(
                        graph, located.line, located.column,
                        f"node {node_id} prompt does not exist: {prompt}",
                    )
                execution["prompt"] = prompt_path.read_text(encoding="utf-8")
            state["execution"] = execution
            state["config_sources"] = {
                key: sources[key] for key in execution if key in sources
            }
        states.append(state)
    if len(start_nodes) != 1:
        raise WorkflowError("workflow requires exactly one start node")
    state_ids = {item["id"] for item in states}
    start = start_nodes[0]
    compiled_edges: list[dict[str, Any]] = []
    used_edge_ids: set[str] = set()
    outgoing_from_start: list[str] = []
    edges = _resume_edges(
        graph, raw_types, linear_convention=linear_convention
    )
    for edge in edges:
        _unknown_attributes(
            graph, edge.attributes.values, EDGE_ATTRIBUTES,
            line=edge.attributes.line, column=edge.attributes.column,
            subject=f"edge {edge.source} -> {edge.target}",
        )
        values = _typed_attributes(
            graph, edge.attributes.values,
            line=edge.attributes.line, column=edge.attributes.column,
        )
        source_type = raw_types.get(edge.source, "checkpoint")
        if source_type == "human" and values.get("on") == "approve":
            values.setdefault("action", "approve")
            values.setdefault("required_role", "approver")
            values.setdefault("requires_feedback", True)
            values.setdefault("feedback_kind", "approval")
        if source_type == "human" and values.get("on") == "revise":
            values.setdefault("requires_feedback", True)
            values.setdefault("feedback_kind", "changes_requested")
        if linear_convention and values.get("on") in {"resume", "retry"}:
            values.setdefault("action", "retry")
            values.setdefault("confirmation", True)
        if linear_convention and values.get("on") == "cancel":
            values.setdefault("action", "cancel")
            values.setdefault("confirmation", True)
        if linear_convention and values.get("on") == "duplicate":
            values.setdefault("confirmation", True)
        source = "@outside" if edge.source == start else edge.source
        if edge.source == start:
            outgoing_from_start.append(edge.target)
        if source not in state_ids and not source.startswith("@"):
            raise _error(graph, edge.line, edge.column, f"unknown edge source {edge.source}")
        if edge.target not in state_ids:
            raise _error(graph, edge.line, edge.column, f"unknown edge target {edge.target}")
        edge_id = str(values.get(
            "id", _generated_edge_id(edge.source, edge.target, used_edge_ids)
        ))
        used_edge_ids.add(edge_id)
        compiled: dict[str, Any] = {
            "id": edge_id,
            "from": source,
            "to": edge.target,
            "evocations": _evocations(
                source_type, values, linear_convention=linear_convention
            ),
            "meaning": str(values.get("meaning", "")),
            "action": str(values.get("action", "transition")),
        }
        for key in (
            "condition", "confirmation", "feedback_kind", "on", "required_role",
            "requires_evidence", "requires_feedback", "requires_outcome", "weight",
        ):
            if key in values:
                compiled[key] = values[key]
        compiled_edges.append(compiled)
    if len(outgoing_from_start) != 1:
        raise WorkflowError("start node requires exactly one outgoing edge")
    entry = str(graph.attributes.get("entry", outgoing_from_start[0]))
    if entry not in state_ids:
        raise WorkflowError(f"entry state {entry} does not exist")
    terminals = sorted(
        item["id"] for item in states if item["node_type"] == "terminal"
    )
    if not terminals:
        raise WorkflowError("workflow requires at least one terminal node")
    ids = [item["id"] for item in compiled_edges]
    if len(ids) != len(set(ids)):
        raise WorkflowError("workflow edge IDs must be unique")
    _validate_reverse_linear_routes(states, compiled_edges)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in compiled_edges:
        if edge["from"] in state_ids:
            adjacency[str(edge["from"])].add(str(edge["to"]))
        elif edge["from"] == "@any_nonterminal":
            for state_id in state_ids - set(terminals):
                adjacency[state_id].add(str(edge["to"]))
    reached = {entry}
    queue = deque([entry])
    while queue:
        for target in adjacency[queue.popleft()] - reached:
            reached.add(target)
            queue.append(target)
    unreachable = sorted(state_ids - reached)
    if unreachable:
        raise WorkflowError("unreachable workflow node: " + unreachable[0])
    reverse: dict[str, set[str]] = defaultdict(set)
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].add(source)
    reaches_terminal = set(terminals)
    queue = deque(terminals)
    while queue:
        for source in reverse[queue.popleft()] - reaches_terminal:
            reaches_terminal.add(source)
            queue.append(source)
    stranded = sorted(state_ids - reaches_terminal)
    if stranded:
        raise WorkflowError("node has no path to a terminal: " + stranded[0])
    scope = {
        "entry_state": entry,
        "main_checkpoints": _list(graph.attributes.get("main_checkpoints", "")),
        "external_linear_states": _list(
            graph.attributes.get("external_linear_states", "")
        ),
        "terminal_states": terminals,
    }
    transitions = tuple(
        item for item in compiled_edges if item["from"] != "@any_nonterminal"
    )
    global_transitions = tuple(
        item for item in compiled_edges if item["from"] == "@any_nonterminal"
    )
    for state in states:
        state["state_definition"] = _state_definition_v1(state, compiled_edges)
    name = str(graph.attributes.get("name", graph.graph_id))
    normalized = {
        "schema_version": schema_version,
        "state_definition_version": 1,
        "name": name,
        "scope": scope,
        "states": states,
        "transitions": list(transitions),
        "global_transitions": list(global_transitions),
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return WorkflowDefinition(
        name=name,
        schema_version=schema_version,
        states=tuple(states),
        transitions=transitions,
        global_transitions=global_transitions,
        scope=scope,
        source_format="dot",
        source_text=graph.source_text,
        normalized=normalized,
        digest=digest,
    )


def _legacy_json(path: Path) -> WorkflowDefinition:
    values = json.loads(path.read_text(encoding="utf-8"))
    transitions = [dict(item) for item in values["transitions"]]
    global_transitions = [dict(item) for item in values["global_transitions"]]
    feedback = values.get("semantics", {}).get("review_feedback", {})
    required_state = feedback.get("required_when_leaving")
    decision_kinds = feedback.get("decision_kinds", {})
    for edge in transitions:
        if edge["from"] == required_state:
            edge["requires_feedback"] = True
            edge["feedback_kind"] = decision_kinds.get(edge["to"])
        edge.setdefault("action", "transition")
    for edge in global_transitions:
        edge.setdefault("action", "transition")
    normalized = {
        "schema_version": values["schema_version"],
        "name": values["name"],
        "scope": values["scope"],
        "states": values["states"],
        "transitions": transitions,
        "global_transitions": global_transitions,
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return WorkflowDefinition(
        name=str(values["name"]),
        schema_version=int(values["schema_version"]),
        states=tuple(dict(item) for item in values["states"]),
        transitions=tuple(transitions),
        global_transitions=tuple(global_transitions),
        scope=dict(values["scope"]),
        source_format="json",
        source_text=path.read_text(encoding="utf-8"),
        normalized=normalized,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def load_workflow(
    path: str | Path, *, profile_paths: Iterable[str | Path] = (),
    factory_defaults: dict[str, Any] | None = None,
) -> WorkflowDefinition:
    resolved = Path(path)
    if resolved.suffix == ".json":
        return _legacy_json(resolved)
    if resolved.suffix != ".dot":
        raise WorkflowError(f"{resolved}: workflow must be .dot or legacy .json")
    try:
        graph = load_dot(resolved)
    except DotSyntaxError as error:
        raise WorkflowError(str(error)) from error
    return compile_dot(
        graph, profile_paths=profile_paths, factory_defaults=factory_defaults
    )
