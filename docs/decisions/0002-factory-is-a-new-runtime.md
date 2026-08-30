# ADR-0002: Build factory as a new runtime informed by autosymph

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-25 |
| Deciders | Project maintainers |
| Source | Public baseline |
| Supersedes | — |

## Decision

Build a new orchestration runtime named `factory` under `factory/`. Use
autosymph as a reference implementation, adopting ideas only after evaluating
them against factory's own contracts.

## Why

Dotfactory combines a portable environment with an orchestration runtime whose
ownership and goals differ from autosymph. Treating factory as a fork would
make upstream compatibility an accidental constraint and obscure which system
owns a path or behavior.

## Consequences

- Good: factory can choose its architecture while retaining useful prior art.
- Cost: autosymph changes require evaluation and an explicit port.
- Not included: compatibility, rebasing, or automatic synchronization with
  autosymph.

## Alternatives

- **Rename an autosymph fork** — rejected because factory is a new runtime, not
  an upstream maintenance branch.
- **Match autosymph's tree and behavior by default** — rejected because it
  imports constraints before factory has chosen them.
- **Ignore autosymph** — rejected because its working patterns remain useful
  evidence and prior art.

## Revisit when

- Factory intentionally adopts autosymph as its codebase and commits to ongoing
  merge or rebase compatibility.
