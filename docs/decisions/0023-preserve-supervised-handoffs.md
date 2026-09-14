# ADR-0023: Preserve supervised handoffs through one writer

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-07 |
| Deciders | Project maintainers |
| Supersedes | — |

## Decision

- Adopt new tracker work at its observed eligible pickup checkpoint.
- Save requirements once per execution and bounded context once per attempt.
- Consume pending human transitions before automatic pickup.
- Persist retention decisions until an explicit provenance-checked release.
- Serve local control on the runtime's existing ledger thread through an
  owner-only Unix socket; the owning OS account holds approver authority.
- Stop at a named workflow state before dispatching its next attempt.

## Why

The ledger contracts already represented human intent, but the composed runtime
omitted them. Opening a second writer to repair that gap would violate ADR-0019.

## Consequences

- Existing execution and command identities remain authoritative across restart.
- Context is bounded and discloses truncation; artifact references remain local.
- The socket is local control, not protection from agents sharing the OS account.
- Unknown processes and outstanding resource cleanup retain their inspection gate.
- Queue admission, enforced delivery evidence, and network authentication remain
  separate work.

## Alternatives

- **Independent control writer** — violates the current SQLite concurrency gate.
- **Tick-count stopping** — varies with recovery and scheduler phases.
- **Transient retention receipt** — cannot constrain cleanup after restart.

## Revisit when

- Multi-writer correctness is proven, or operators need distinct OS identities.
- Bounded handoff omissions measurably prevent successful review or rework.

## Links


- Evidence: `factory/tests/test_supervised_delivery.py`, `factory/tests/test_operator_boundary.py`.
- Recovery: [supervised handoffs](../solutions/integration-issues/supervised-handoffs-20260907.md).
