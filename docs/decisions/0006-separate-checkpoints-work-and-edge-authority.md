# ADR-0006: Define the Linear workflow, status types, and edge authority

| Field | Value |
|---|---|
| Status | Superseded |
| Date | 2026-08-26 |
| Deciders | Project maintainers |
| Source | Public baseline |
| Supersedes | — |
| Superseded by | [ADR-0011](0011-author-workflows-as-typed-dot-graphs.md) |

## Decision

Use one Linear workflow for human and agent work. Classify each
factory-visible status by what it means operationally: `checkpoint` or `work`.
Classify each transition separately by who may evoke it: `human`, `agent`, or
either.

`factory/workflow.json` is the executable graph. This ADR governs its status
taxonomy, routes, and authority rules. `factory/WORKFLOW.md` is the generated
human-readable projection.

## Mental model

| Concept | Operational meaning |
|---|---|
| Checkpoint | A durable condition that can wait without an active attempt. |
| Work | An active operation with an owner, attempt ID, start time, and heartbeat. It must complete, recover, or block. |
| Leading edge | The authorized move into a status. |
| Ending edge | The authorized move out of a status. |
| Authority | The human or agent allowed to decide an edge. `Both` means either independently, not two approvals. |
| Signal | Evidence of the decision: status change, listener claim, agent handoff, recovery request, or structured comment. A signal is not an actor. |

Linear's built-in status categories control its UI and queues. They do not
replace the factory type. The factory type determines whether an attempt must
be actively owned.

## Status inventory

| Linear status | Factory type | Role | Operational rule |
|---|---|---|---|
| Todo | checkpoint | pickup | Committed work waits for human planning or an automatic planner claim. |
| Autoplanning | work | planning | The automatic planner owns an active attempt. |
| Planning | work | planning | A human-evoked planning session is active. |
| Ready | checkpoint | pickup | An accepted plan waits for implementation to be claimed. |
| Implementing | work | delivery | A human or agent owns product changes. |
| Verifying | work | verification | A human or agent owns completion evidence. |
| Review | checkpoint | human decision | Verified work waits for human approval or rework. |
| Reworking | work | delivery | A human or agent owns requested changes. |
| Investigating | work | recovery | A human or agent owns diagnosis of a failed attempt. |
| Blocked | checkpoint | exception | Work waits for a human decision, dependency, or resource. |
| Done | checkpoint | terminal | The accepted outcome is complete. |
| Canceled | checkpoint | terminal | The issue will not be completed. |
| Duplicate | checkpoint | terminal | Another issue owns the work. |

`Triage` and `Backlog` remain Linear statuses before factory entry.
`Merging` remains available in Linear but is outside v1 until landing warrants
its own observable attempt.

## Workflow

The routes share checkpoints instead of creating separate human and agent
workflows.

| Route | Path |
|---|---|
| Human | `Todo → Planning → Ready → Implementing → Verifying → Review → Done` |
| Automatic planning | `Todo → Autoplanning → Ready` |
| Review rework | `Review → Reworking → Verifying → Review` |
| Recovery | `active work → Investigating → originating work` |
| Blocked recovery | `Investigating → Blocked → Investigating` |
| Global exits | `any nonterminal → Canceled` or `Duplicate` |

## Edge authority

| From | To | Authority | Signal |
|---|---|---|---|
| outside factory | Todo | human | Linear status change |
| Todo | Autoplanning | agent | listener claim |
| Todo | Planning | human | Linear status change |
| Autoplanning | Ready | agent | agent handoff |
| Planning | Ready | human | Linear status change |
| Ready | Implementing | human or agent | Linear status change or listener claim |
| Implementing | Verifying | human or agent | Linear status change or agent handoff |
| Verifying | Review | human or agent | Linear status change or agent handoff |
| Review | Done | human | Linear status change |
| Review | Reworking | human | Linear status change or structured comment |
| Reworking | Verifying | human or agent | Linear status change or agent handoff |
| Autoplanning | Investigating | agent | recovery request |
| Planning | Investigating | human | Linear status change |
| Implementing, Verifying, or Reworking | Investigating | human or agent | Linear status change or recovery request |
| Investigating | Autoplanning | agent | agent handoff when it is the recorded resume state |
| Investigating | Planning | human | Linear status change when it is the recorded resume state |
| Investigating | Implementing, Verifying, or Reworking | human or agent | Linear status change or agent handoff when it is the recorded resume state |
| Investigating | Blocked | human or agent | Linear status change or agent handoff |
| Blocked | Investigating | human | Linear status change or structured comment |
| any nonterminal | Canceled | human | Linear status change |
| any nonterminal | Duplicate | human or agent | Linear status change or agent handoff |

Ordinary comments are context only. A structured comment may evoke only an
edge that already grants that actor authority.

## Why

A status says what condition the issue is in. It does not say who caused the
condition. Keeping authority on edges preserves the human-only route while
allowing listeners and agents to share the same checkpoints.

The distinction also prevents passive waiting from looking like active work.
`Todo`, `Ready`, `Review`, and `Blocked` may rest safely. Planning, delivery,
verification, rework, and investigation require a live owner and attempt.

## Consequences

- Good: the Linear board communicates whether work is waiting or active.
- Good: leading and ending edge authority is explicit and testable.
- Good: human and automatic planning converge on the same `Ready` contract.
- Good: `Review` cannot advance or request rework without human authority.
- Cost: Linear is a projection; attempt ownership and recovery metadata remain
  factory records rather than additional statuses.
- Cost: the graph, generated guide, ADR, and live Linear names must stay aligned.

## Linear migration

| Action | Previous | Current | Result |
|---|---|---|---|
| Rename | Autoplan | Autoplanning | Applied 2026-08-27 |
| Rename | In Review | Review | Applied 2026-08-27 |
| Rename | Rework | Reworking | Applied 2026-08-27 |
| Remove | In Progress | — | Applied 2026-08-27 |
| Keep outside v1 | Merging | Merging | No factory edge |

## Alternatives

- **Encode actor ownership in status names** — rejected because the same
  checkpoint can be entered or exited by different actors.
- **Keep `In Progress`** — rejected because it loses the active operation.
- **Model `Investigating` as blocked** — rejected because diagnosis is active
  work; `Blocked` is the safe human wait.
- **Treat ordinary comments as commands** — rejected because context must not
  accidentally mutate the graph.
- **Make `Merging` part of v1** — deferred until landing is long-running or
  failure-prone enough to require ownership and heartbeat evidence.

## Revisit when

- Landing needs its own observable active attempt.
- A work state can wait without an owner or heartbeat and remain useful.
- Linear cannot represent the required checkpoint and work projection.
