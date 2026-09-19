# ADR-0035: Abandon uncertain runs without assuming process exit

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-18 |
| Deciders | Project maintainers |
| Supersedes | —; refines ADR-0012 and ADR-0019 |

## Decision

An in-flight or ambiguous dispatch permits inspection and confirmed cancellation,
not a fresh control transition. Cancellation abandons durable work; it does not
prove child-process exit or erase side-effect uncertainty. Automatic workspace
cleanup quarantines canceled uncertain runs. An approver must inspect surviving
processes before explicit release, or choose durable retention/quarantine.

## Why

The coordinator can die while its child survives. A clean Git status and a
canceled ledger row do not prove nobody can still write into that workspace.

## Consequences

- Preserve original error/trace/runner identities; never invent a successful result.
- Keep command fencing, idempotency, and cleanup guards on the existing writer.
- Conservatively require inspection even when a canceled child may already have
  exited; no PID-only kill or automatic continuation is authorized.
- Terminal workflow state and safe physical cleanup remain separate facts.

## Alternatives

- **Retry from scratch** — can repeat external side effects.
- **Remove a clean workspace immediately** — an unowned child may still use it.
- **Kill the recorded PID on recovery** — does not prove current process ownership.

## Revisit when

An adapter proves continuation and descendant-process ownership/exit across host
restarts, with a durable receipt and fault-injection coverage.

## Links

- [Changelog](../changelogs/2026-09-18-operator-recovery-health.md)
- `factory/tests/test_process_lifecycle.py`
