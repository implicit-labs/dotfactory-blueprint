# Architecture decisions

These records govern dotfactory itself. Decisions for products or repositories
configured by dotfactory belong to their owning repository.

## Index

| ADR | Decision | Status | Supersedes |
|---|---|---|---|
| [0001](0001-repository-ownership-boundaries.md) | Partition the repository by owner | Accepted | — |
| [0002](0002-factory-is-a-new-runtime.md) | Build factory as a new runtime informed by autosymph | Accepted | — |
| [0003](0003-portable-harness-and-credential-boundary.md) | Version portable behavior; isolate credentials and machine state | Accepted | — |
| [0006](0006-separate-checkpoints-work-and-edge-authority.md) | Define the Linear workflow, status types, and edge authority | Superseded | — |
| [0008](0008-local-ledger-external-projections.md) | Make the local ledger authoritative | Accepted | — |
| [0009](0009-mobile-control-through-kernel.md) | Route mobile control through the durable kernel | Accepted | — |
| [0011](0011-author-workflows-as-typed-dot-graphs.md) | Author workflows as typed DOT graphs | Accepted | 0006; part of 0008 |
| [0012](0012-prepare-work-before-runner-launch.md) | Prepare owned work before runner launch | Accepted | — |
| [0013](0013-schedule-from-durable-attempt-facts.md) | Schedule from durable attempt facts | Accepted | — |
| [0014](0014-expand-readable-dot-before-execution.md) | Expand readable DOT before execution | Accepted | — |
| [0015](0015-run-live-agents-through-versioned-protocols.md) | Run live agents through versioned protocols | Accepted | — |
| [0017](0017-keep-execution-traces-authoritative.md) | Keep execution traces authoritative | Accepted | — |
| [0018](0018-reconcile-linear-through-the-durable-kernel.md) | Reconcile Linear through the durable kernel | Accepted | 0008 and 0011 (partial) |
| [0019](0019-compose-one-recoverable-lifecycle.md) | Compose one recoverable lifecycle | Accepted | — |
| [0020](0020-project-readable-run-evidence-into-linear.md) | Project readable run evidence into Linear | Accepted | — |

## Lifecycle

| Status | Meaning |
|---|---|
| Proposed | Under consideration; implementation must not assume approval. |
| Accepted | Governs current dotfactory behavior. |
| Rejected | Considered and not adopted. |
| Deprecated | Still present but no longer recommended. |
| Superseded | Replaced by the ADR named in the record. |

Accepted records are history. Correct broken links or factual metadata in
place; replace the decision with a new ADR using `Supersedes`.

Backfills use the original decision date. Git history records later
documentation changes.

## Template

```markdown
# ADR-NNNN: <decision>

| Field | Value |
|---|---|
| Status | Proposed |
| Date | YYYY-MM-DD |
| Deciders | <maintainers> |
| Source | <public issue or release> |
| Supersedes | — |

## Decision

<What dotfactory will do.>

## Why

<The constraint or tradeoff that requires this choice.>

## Consequences

- Good:
- Cost:
- Not included:

## Alternatives

- **<option>** — rejected because <reason>.

## Revisit when

- <Observable condition that would invalidate the decision.>

## Evidence

- Test or document:
```
