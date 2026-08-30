# ADR-0012: Prepare owned work before runner launch

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-29 |
| Deciders | Project maintainers |
| Source | Public baseline |
| Supersedes | — |

## Decision

Prepare repository workspaces and declared resources after the durable kernel
creates a fenced attempt and before a live runner starts. A live runner accepts
only an immutable `PreparedLaunch` containing the PR #13 `RunnerRequest`, its
execution workspace, redacted launch view, allocations, and preparation digest.

Keep workspaces execution-scoped so review and rework retain the same code.
Keep scarce processes, routes, devices, and sessions attempt-scoped so human
checkpoints do not hold them.

Default each workspace pool to the project's ignored `.worktrees/` directory.
Verify the ignore rule before mutation. Allow explicit external project roots.

Record provider mutation intent before external side effects. Commit a complete
preparation group before launch. When preparation fails, compensate exact owned
mutations in reverse order. Cross-system behavior is all-ready-or-compensated;
SQLite does not make host operations transactionally atomic.

DOT contains logical capability names only. Factory activation validates that
names and providers exist. Attempt preparation validates live availability.
Resource waiting does not complete the attempt or consume runner retries.

Cleanup requires matching durable ownership, stable machine identity, and
attempt fence. Dirty, unknown, stale, or differently owned state is quarantined
and surfaced through a durable attention request.

Use Portless for local services. Reuse its worktree-aware naming, never force a
route takeover, and never silently fall back to a raw port.

## Why

Runners need a complete environment, but workflow definitions must remain
portable and machine-independent. A durable preparation boundary makes resource
ownership, crash recovery, escalation, and cleanup observable without coupling
the graph or runner adapters to host-specific mechanisms.

## Consequences

- Good: runner launch has one fenced, testable prerequisite.
- Good: running executions retain their original graph and resolved resources.
- Good: review checkpoints retain code without hoarding scarce host resources.
- Good: cleanup acts only on state the factory can prove it owns.
- Cost: external side effects require a mutation journal and compensation.
- Cost: factory configuration and the ledger gain new schema versions.
- Not included: live simulator, browser, authenticated-session, physical-device,
  or foreground-computer providers; those remain follow-up capabilities.

## Alternatives

- **Let each runner prepare its environment** — rejected because ownership,
  cleanup, and recovery would vary by harness.
- **Put concrete resources in DOT** — rejected because workflow snapshots would
  become host-specific and could retain sensitive identifiers.
- **Count preparation failure as runner retry** — rejected because the runner
  has not started and `max_retries` governs work outcomes.
- **Claim host operations are atomic with SQLite** — rejected because Git,
  processes, routes, and devices require explicit compensation.
- **Delete uncertain state during cleanup** — rejected because user work and
  personal sessions outrank automated tidiness.

## Revisit when

- A provider cannot implement idempotent reconcile or compensation with a stable
  machine identity.
- Execution-scoped workspaces measurably prevent required parallel verification.
- Portless cannot provide collision-safe worktree-local routing without takeover.
