# ADR-0013: Schedule from durable attempt facts

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-29 |
| Deciders | Project maintainers |
| Source | Public baseline |
| Supersedes | — |

## Decision

Admit work from active attempts and their stored workflow bindings. Claim one
attempt atomically in SQLite before preparation. Apply host, project, and
resolved-runner limits in the same transaction.

Persist `claimed`, `preparing`, `prepared`, `dispatching`, `result_ready`,
`waiting`, `attention`, `completed`, and `superseded` phases. Only `claimed`
and `prepared` may expire and be reclaimed automatically. Preparation,
dispatch, and result phases are non-stealable; the same stable scheduler owner
reconciles them after restart. An operator-approved retry may transfer work to
a new owner.

Dispatch only `PreparedLaunch`. Record launch intent before calling a runner,
record its redacted result before cleanup, and complete the workflow through an
idempotent kernel command. Ambiguous runner intent requires attention and is
never rerun automatically.

Keep workflow loading, preparation, runner adapters, and external projection
outside the scheduler. Observation is fail-soft and cannot roll back canonical
state.

## Why

The ledger must answer what may run after a crash or concurrent poll without
reinterpreting current DOT or trusting process memory. Explicit irreversible
boundaries prevent duplicate external work.

## Consequences

- Good: concurrent pollers cannot claim the same attempt or exceed configured
  admission limits.
- Good: restart uses the execution's immutable workflow digest and resolved
  node binding.
- Good: resource waiting does not consume graph retries.
- Cost: scheduler owner identity must remain stable across ordinary restarts.
- Cost: ambiguous preparation or runner intent stops for an inspectable remedy.
- Not included: polling transports and live runner adapters.
- Not included: simulator, browser, and computer-use providers.

## Alternatives

- **Rebuild claims from process memory** — rejected because restart would lose
  ownership and capacity facts.
- **Reload current DOT on every poll** — rejected because running executions
  are bound to immutable snapshots.
- **Expire every phase** — rejected because a second runner could repeat an
  external side effect whose result was not recorded.
- **Let the scheduler create resources or interpret state names** — rejected
  because those policies already have graph and preparation owners.

## Revisit when

- A runner provides a durable external idempotency key that makes ambiguous
  dispatch automatically reconcilable.
- Multi-host scheduling requires a formal operator takeover lease rather than a
  stable scheduler owner.
