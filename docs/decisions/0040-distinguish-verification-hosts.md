# ADR-0040: Distinguish worker and coordinator verification requirements

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-19 |
| Deciders | Maintainers |
| Supersedes | — |

## Decision

Declare coordinator requirements separately from worker requirements in the frozen
execution policy. Share resolution across admission and inspection. Share the
coordinator environment constructor with delivery verification. Reject unsuitable
coordinators before worker allocation and recheck before delivery verification.

## Why

A suitable worker cannot establish that coordinator-side verification can run.
Instance-only diagnostic checks conceal project/run overrides.

## Consequences

- Optional contracts preserve legacy snapshots and execution behavior.
- Explicit contract replacement can reduce configurable defaults; pinned verifier,
  workflow authority and credential isolation remain unchanged.
- Read-only inspection does not claim that probes or remote checks passed.
- Host probes are trusted operator commands, not plan-authored policy or leases.
- Remaining configuration domains and planning-to-placement binding require later
  phases; this record does not grant arbitrary recursive configuration overrides.

## Alternatives

- Run delivery under the worker environment: rejected; it changes the independent
  coordinator verification authority and credential boundary.
- Infer readiness from installed worker tools: rejected; host identities differ.

## Revisit when

A separately reviewed verification lane supports other hosts or credentials.

## Links

- [Guide](../guides/verification-hosts.md)
- [Implementation plan](../plans/project-run-contracts.md)
