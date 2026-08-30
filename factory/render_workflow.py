#!/usr/bin/env python3
"""Render an inspectable Markdown view of any typed DOT workflow."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_WORKFLOW = ROOT / "workflows" / "default.dot"
DEFAULT_OUTPUT = ROOT / "WORKFLOW.md"

sys.path.insert(0, str(ROOT / "src"))

from dotfactory.workflow import WorkflowDefinition, WorkflowError, load_workflow  # noqa: E402


def table_cell(value: Any) -> str:
    if value in (None, "", [], {}):
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    return str(value).replace("|", "\\|").replace("\n", " ")


def actor_label(edge: dict[str, Any]) -> str:
    actors = []
    for evocation in edge["evocations"]:
        actor = evocation["actor"]
        if actor not in actors:
            actors.append(actor)
    return " + ".join(actors)


def signal_label(edge: dict[str, Any]) -> str:
    signals = []
    for evocation in edge["evocations"]:
        signal = evocation["signal"].replace("_", " ")
        if signal not in signals:
            signals.append(signal)
    return " / ".join(signals)


def display_name(state_id: str, states: dict[str, dict[str, Any]]) -> str:
    if state_id == "@outside":
        return "start"
    if state_id == "@any_nonterminal":
        return "any nonterminal"
    return str(states[state_id].get("label", state_id))


def execution_label(state: dict[str, Any]) -> str:
    execution = state.get("execution", {})
    sources = state.get("config_sources", {})
    if not execution:
        return "—"
    preferred = (
        "profile", "runner", "model", "reasoning_effort", "prompt", "skills",
        "max_retries", "timeout", "exit_contract",
    )
    parts = []
    for key in preferred:
        if key in execution:
            value = execution[key]
            if isinstance(value, list):
                value = ",".join(str(item) for item in value)
            source = f" [{sources[key]}]" if key in sources else ""
            parts.append(f"{key}={value}{source}")
    parts.extend(
        f"{key}={value}{f' [{sources[key]}]' if key in sources else ''}"
        for key, value in sorted(execution.items())
        if key not in preferred
    )
    return "; ".join(parts)


def policy_label(edge: dict[str, Any]) -> str:
    fields = []
    for key in (
        "action", "on", "condition", "required_role", "confirmation",
        "requires_feedback", "feedback_kind", "requires_outcome",
        "requires_evidence", "weight",
    ):
        if key in edge and edge[key] not in ("", False, None, "transition"):
            fields.append(f"{key}={table_cell(edge[key])}")
    return "; ".join(fields) or "—"


def mermaid(definition: WorkflowDefinition) -> list[str]:
    node_ids = {
        state["id"]: f"N{index}" for index, state in enumerate(definition.states)
    }
    lines = ["```mermaid", "flowchart LR", '    START{{"start"}}']
    for state in definition.states:
        node = node_ids[state["id"]]
        label = str(state.get("label", state["id"])).replace('"', "&quot;")
        projection = state.get("linear_status")
        if projection and projection != state["id"]:
            label += f"<br/><small>Linear: {projection}</small>"
        node_type = state["node_type"]
        if node_type == "terminal":
            lines.append(f'    {node}(["{label}"])')
        elif state["kind"] == "work":
            lines.append(f'    {node}["{label}"]')
        else:
            lines.append(f'    {node}(["{label}"])')
    if definition.global_transitions:
        lines.append('    ANY["any nonterminal"]')

    for edge in definition.transitions + definition.global_transitions:
        source = (
            "START" if edge["from"] == "@outside"
            else "ANY" if edge["from"] == "@any_nonterminal"
            else node_ids[edge["from"]]
        )
        target = node_ids[edge["to"]]
        label = str(
            edge.get("on")
            or (edge.get("action") if edge.get("action") != "transition" else "")
        ).replace('"', "&quot;")
        if label:
            lines.append(f'    {source} -->|"{label}"| {target}')
        else:
            lines.append(f"    {source} --> {target}")

    classes: dict[str, list[str]] = {
        "agent": [], "human": [], "tool": [], "checkpoint": [], "terminal": []
    }
    for state in definition.states:
        node_type = state["node_type"]
        style = node_type if node_type in classes else (
            "agent" if state["kind"] == "work" else "checkpoint"
        )
        classes[style].append(node_ids[state["id"]])
    lines.extend([
        "    classDef agent fill:#ffedd5,stroke:#ea580c,color:#111827;",
        "    classDef human fill:#ede9fe,stroke:#7c3aed,color:#111827;",
        "    classDef tool fill:#cffafe,stroke:#0891b2,color:#111827;",
        "    classDef checkpoint fill:#dbeafe,stroke:#2563eb,color:#111827;",
        "    classDef terminal fill:#e5e7eb,stroke:#4b5563,color:#111827;",
        "    classDef external fill:#f3f4f6,stroke:#9ca3af,color:#374151,stroke-dasharray: 4 3;",
    ])
    for style, nodes in classes.items():
        if nodes:
            lines.append(f"    class {','.join(nodes)} {style};")
    lines.append("    class START external;")
    if definition.global_transitions:
        lines.append("    class ANY external;")
    lines.append("```")
    return lines


def render(definition: WorkflowDefinition, source_label: str) -> str:
    states = {state["id"]: state for state in definition.states}
    edges = definition.transitions + definition.global_transitions
    out = [
        f"<!-- Generated by render_workflow.py from {source_label}. Do not edit. -->",
        f"# {definition.name}",
        "",
        f"Workflow digest: `{definition.digest}`",
        "",
        "## State machine",
        "",
    ]
    out.extend(mermaid(definition))
    out.extend([
        "",
        "## States",
        "",
        "| ID | Label | Type | Lifecycle | Linear projection | Execution configuration |",
        "|---|---|---|---|---|---|",
    ])
    for state in definition.states:
        out.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                table_cell(state["id"]), table_cell(state["label"]),
                table_cell(state["node_type"]), table_cell(state["kind"]),
                table_cell(state.get("linear_status")),
                table_cell(execution_label(state)),
            )
        )
    out.extend(["", "## Edges", ""])
    include_meaning = any(edge.get("meaning") for edge in edges)
    if include_meaning:
        out.extend([
            "| ID | From | To | Evoked by | Signal | Policy | Meaning |",
            "|---|---|---|---|---|---|---|",
        ])
    else:
        out.extend([
            "| ID | From | To | Evoked by | Signal | Policy |",
            "|---|---|---|---|---|---|",
        ])
    for edge in edges:
        cells = [
            edge["id"], display_name(edge["from"], states),
            display_name(edge["to"], states), actor_label(edge),
            signal_label(edge), policy_label(edge),
        ]
        if include_meaning:
            cells.append(edge.get("meaning"))
        out.append("| " + " | ".join(table_cell(item) for item in cells) + " |")
    out.extend([
        "",
        "## Runtime contract",
        "",
        "- Checkpoint nodes may wait without an active attempt.",
        "- Work nodes require an owned attempt and evidence before completion.",
        "- Node IDs are stable internal identity; `label` and `linear_status` are projections.",
        "- The resolved workflow and node configuration are snapshotted by digest per execution.",
        "- Only edges in this graph may be accepted by the kernel.",
        "",
    ])
    return "\n".join(out)


def relative_label(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
    parser.add_argument("--profiles", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    try:
        definition = load_workflow(args.workflow, profile_paths=args.profiles)
    except (OSError, WorkflowError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    content = render(definition, relative_label(args.workflow))
    if args.check:
        actual = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if actual != content:
            print(f"{args.output} is stale; run factory/render_workflow.py", file=sys.stderr)
            return 1
        return 0
    args.output.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
