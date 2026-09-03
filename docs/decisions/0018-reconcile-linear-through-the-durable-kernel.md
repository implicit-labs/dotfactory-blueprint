# ADR-0018: Reconcile Linear through the durable kernel

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-30 |
| Deciders | Project maintainers |
| Source | Public release |
| Supersedes | Part of 0008 and 0011 |

## Decision

Compile each typed DOT node into a versioned state definition. Reject any graph
where one observed Linear status could select multiple human transitions from
the same state.

Bind a workflow digest to exact Linear team status IDs before observations can
drive the kernel. Persist each observation before reconciliation. Record whether
a transition originated from an agent, human, system, or recovery separately
from the actor and edge authority.

Commit each desired Linear status mutation in the same SQLite transaction as
its source event. Deliver mutations outside the transaction using a stable
semantic key and request hash. Treat an interrupted or timed-out send as
ambiguous until the remote issue is read; do not retry it as an ordinary pending
mutation. Confirm only the exact desired status.

Keep Linear field and label policy explicit. Native status, project, priority,
cycle, assignee, team, and delegation fields cannot be duplicated as label
dimensions. Runner overrides must come from project-allowed labels and name a
configured runner.

## Why

Linear is a human control surface, not execution authority. A webhook may be
duplicated, delayed, or superseded, while a mutation may succeed remotely even
when its response is lost. Exact status bindings, durable observations, and an
ambiguity-aware outbox keep those conditions from inventing transitions or
replaying side effects.

## Consequences

- Good: graph ambiguity fails during compilation, before a run starts.
- Good: every accepted or rejected human status edit has a durable source fact.
- Good: crash recovery cannot convert an unknown remote result into a blind retry.
- Good: Linear status names may change without changing internal node identity.
- Cost: projects must refresh status-ID bindings when their Linear workflow changes.
- Cost: the delivery edge must read Linear after an ambiguous send.
- Not included: live GraphQL transport, webhook HTTP verification, Agent Session
  summaries, or nightly trace repair.

## Alternatives

- **Let Linear own run state** — rejected because remote availability and edits
  cannot define durable completion.
- **Resolve status by display name** — rejected because names are mutable and
  may collide.
- **Retry every timeout** — rejected because the first mutation may already have
  succeeded.
- **Infer source from actor** — rejected because recovery and system actions may
  exercise an agent- or human-authorized edge without sharing that origin.

## Revisit when

- Linear offers a versioned, transactional compare-and-set mutation with a
  durable idempotency key and read-after-write receipt.
- One SQLite writer no longer owns transition order.

## Evidence

- `factory/tests/test_linear_reconciliation.py`
