# ADR-0028: Check local readiness without starting work

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-13 |
| Deciders | Project maintainers |
| Supersedes | — |

## Decision

Expose `dotfactory doctor --config PATH [--json]` as offline, read-only local
prerequisite inspection. It must not construct a runtime or authenticate providers.

## Why

Operators need actionable setup diagnostics before admitting work or acquiring resources.

## Consequences

- Reuse configuration validation; inspect local Git roots, origin/main, executable
  availability, and only credentials required by the enabled runtime path.
- JSON schema version 1 distinguishes pass, fail, skipped, and not_checked.
- Exit 0 means required local prerequisites passed, not authenticated or live readiness.
- Never fetch, repair, launch agents, read credential stores, allocate, or create a ledger.
- Do not expose credential values, remote URLs, or raw configuration exceptions.
- Linear polling requires a token; webhook readiness remains a separate unchecked boundary.

## Alternatives

- Run full runtime preflight: creates state and may contact providers.
- Require every named environment variable: conflates optional capabilities with prerequisites.

## Revisit when

A new runtime capability changes the required local inputs or a diagnostic needs
network access; keep that operation explicit and separately authorized.

## Evidence

- [Tests](../../factory/tests/test_doctor.py)
