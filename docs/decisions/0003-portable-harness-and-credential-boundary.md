# ADR-0003: Version portable behavior; isolate credentials and machine state

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-25 |
| Deciders | Project maintainers |
| Source | Public baseline |
| Supersedes | — |

## Decision

Version portable harness behavior in `dotfiles/harness-config/`, expressed in
each harness's native format. Keep credentials, OAuth state, MCP tokens,
session history, project trust, and machine-specific hooks outside the
repository.

Shell identity commands may unset or inject credentials only in their child
process. They must not mutate the parent shell or globally export provider
keys.

## Why

Dotfactory must reproduce behavior across machines without copying secrets or
pretending Claude Code, Codex, and OMP share a configuration schema or
credential store.

## Consequences

- Good: portable behavior is reviewable and credentials remain machine-owned.
- Cost: equivalent capabilities must be expressed separately for each harness.
- Not included: headless installation of state a harness exposes only through
  its own UI.

## Alternatives

- **Commit complete harness homes** — rejected because they contain secrets,
  sessions, trust, and machine-specific state.
- **Invent one shared meta-format** — rejected because translation would hide
  native semantics and unsupported settings.
- **Configure everything in shell aliases** — rejected because aliases cannot
  own or validate full harness behavior.

## Revisit when

- The harnesses expose a common, lossless portable schema and a separate secure
  credential interface.
