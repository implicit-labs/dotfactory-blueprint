# ADR-0019: Compose one recoverable lifecycle

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-30 |
| Deciders | Project maintainers |
| Source | Public release |
| Supersedes | — |

## Decision

Compose config loading, project registration, typed-DOT kernels, preparation,
runner dispatch, Linear convergence, tracing, restart recovery, and terminal
cleanup behind `python3 -m dotfactory run`.

Run one process and one SQLite writer per ledger. Hold a nonblocking instance
lock for the process lifetime. Start an explicit issue or discover the oldest
eligible issue in a configured pickup status. Stop a one-shot run at the next
human, attention, or terminal boundary; `--watch` continues polling.
Record attention remedies through a separate audited CLI command. Resolution
does not execute work; a later run reconciles the recorded durable safe phase.
Offer scheduler retry only for `claimed`, `preparing`, `prepared`, and
`result_ready`. A `dispatching` ambiguity retains evidence and escalates without
an automated retry or cancel. Repeated recovery failures open fresh attention.
Bind each composed control service to one project before journaling a command.

Keep SQLite authoritative. Linear mutations, worktree changes, runner results,
and cleanup remain fenced and ownership-checked. Emit a deterministic lifecycle
receipt from the same fixed trace facts used by the local waterfall.
Treat factory projections and runner-owned integrations as separate authority
surfaces. Disable configured named MCP connectors when the kernel owns a system.
Treat alternate-channel prompt rules as cooperative unless separate sandbox or
network policy enforces them.

## Why

The existing components were independently durable but had no honest startup
surface. Starting them by hand could omit reconciliation, reuse the wrong
worktree, lose restart recovery, or leave cleanup unowned.

SQLite 3.51.0 through 3.51.2 has a tracked concurrent-WAL safety issue. One
process avoids claiming concurrency before a patched runtime and multi-writer
recovery test are available.

## Consequences

- Good: one command exercises the complete durable path and produces one trace.
- Good: restart reuses active executions and scheduler recovery facts.
- Good: a stored runner result replays without a live runner or connector check.
- Good: project-bound controls cannot mutate another project's execution.
- Good: terminal cleanup touches only clean, provenance-matched owned worktrees.
- Good: a configured named MCP connector cannot bypass kernel-owned Linear
  synchronization.
- Cost: alternate browser, shell, and network channels remain prompt-governed
  unless a separate execution policy blocks them.
- Cost: one slow runner blocks other dispatches in this first composition.
- Cost: automatic discovery depends on live Linear credentials and schema.
- Not included: multi-process writers, a hosted control server, webhook hosting,
  simulator/browser delegation, or nightly repair.

## Alternatives

- **Start each worker separately** — rejected because ordering and shutdown would
  remain operator convention instead of runtime policy.
- **Enable concurrent writers immediately** — rejected until a patched SQLite
  requirement is satisfied and verified.
- **Make Linear the lifecycle authority** — rejected because remote delay or
  outage cannot define local completion.

## Revisit when

- A patched SQLite runtime and a multi-writer recovery test are proven.
- One synchronous runner prevents the required throughput.
- The listener and control host need independent failure domains.

## Evidence

- `factory/src/dotfactory/lifecycle.py`
- `factory/tests/test_lifecycle.py`
