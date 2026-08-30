# ADR-0001: Partition the repository by owner

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-25 |
| Deciders | Project maintainers |
| Source | Public baseline |
| Supersedes | — |

## Decision

Partition dotfactory by owner: shell-loaded environment in `dotfiles/`, state
machine runtime in `factory/`, portable capabilities in `skills/<goal>/`, and
decisions and history in `docs/`.

Prompts stay with their owner. Do not create root `prompts/`, `scripts/`,
`shared/`, or another catch-all.

## Why

Portability, orchestration, reusable capabilities, and repository history have
different install and change lifecycles. A path should identify which contract
owns a file without requiring knowledge of its current implementation.

## Consequences

- Good: each component can install, test, and evolve against its own contract.
- Cost: cross-cutting work may touch several owner directories.
- Not included: splitting these owners into separate repositories.

## Alternatives

- **Group by file type** — rejected because prompts and scripts would lose their
  owning context.
- **Keep the upstream tree** — rejected because upstream structure does not
  express dotfactory's portability boundary.
- **Separate repositories now** — rejected because coordinated changes still
  benefit from one review and one history.

## Revisit when

- An owner needs independent access control, versioning, or releases that the
  monorepo cannot provide.
