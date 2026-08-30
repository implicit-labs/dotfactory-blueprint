# ADR-0014: Expand readable DOT before execution

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-29 |
| Deciders | Project maintainers |
| Source | Public baseline |
| Amends | [ADR-0011](0011-author-workflows-as-typed-dot-graphs.md) |

## Decision

Add opt-in schema-v2 authoring conventions that expand into explicit typed
policy before validation, hashing, snapshotting, rendering, or execution.
Schema v1 behavior remains unchanged.

`linear_statuses=node_ids` uses each node ID as its default Linear projection.
`conventions=linear` derives standard evocations and confirmation policy from
an edge's `on` and `authority`. Missing edge IDs are generated from endpoints.
`source -> @resume [on=retry]` expands to conditional edges for every state
with an incoming `on=failed` route to `source`.

The normalized graph remains the audit and execution contract. The authored
DOT remains its concise source.

## Why

The first default DOT repeated tracker mechanics and prose on nearly every
line. The graph was correct but obscured its topology. The normalization layer
already exists to make readable syntax and explicit runtime policy compatible.

## Consequences

- Good: the default graph exposes states, routes, decisions, and authority at a glance.
- Good: the generated view still shows every effective signal and resume edge.
- Good: existing schema-v1 workflows and snapshotted executions do not change.
- Cost: schema v2 owns deterministic expansion rules and diagnostics.
- Cost: generated edge IDs, not authored IDs, are the default for new graphs.
- Not included: new states, transitions, runner settings, or graphical editing.

## Alternatives

- **Keep the verbose graph canonical** — rejected because the authoring artifact
  fails the readability goal that motivated DOT.
- **Hide detail only in the renderer** — rejected because users still edit the
  verbose source.
- **Infer from state names** — rejected because custom graphs must not inherit
  dotfactory-specific names.

## Revisit when

- Expansion cannot be explained entirely from visible graph attributes.
- A concise graph normalizes differently across hosts or parser versions.
- Generated IDs cannot provide stable audit references for a proven workflow.
