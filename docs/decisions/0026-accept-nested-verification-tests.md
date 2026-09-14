# ADR-0026: Accept nested verification test directories

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-13 |
| Deciders | Project maintainers |

## Decision

Planning definitions may declare canonical repository-relative Python files
inside a `tests/` directory at any depth, or the root `.factory/` directory.
This extends ADR-0024's planning file policy without changing approval semantics.

## Why

The root-only rule rejected this repository's `factory/tests/` layout during
a local planning run despite preserving the intended test ownership boundary.

## Consequences

- Exact path components distinguish test directories from arbitrary source files.
- Traversal, aliases, URI encodings, Git internals, symlinks, and untracked files
  remain rejected. Existing AST checks and planning changed-file checks remain.
- Human approval still freezes exact file hashes; tests are not run at planning.
- The path is an ownership convention, not proof of test quality or an OS sandbox.

## Alternatives

- Hard-code `factory/tests/`: rejects the next monorepo layout.
- Allow arbitrary Python files: weakens the planning-only production boundary.
- Move all checks to root `tests/`: conflicts with repository ownership conventions.

## Revisit when

A supported project requires checks outside `tests/` or `.factory/`; introduce an
explicitly reviewed policy rather than accepting arbitrary source paths.

## Links

- [Regression tests](../../factory/tests/test_verified_delivery.py)
