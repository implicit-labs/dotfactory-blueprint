# ADR-0008: Make the local ledger authoritative

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-27 |
| Deciders | Project maintainers |
| Source | Public baseline |
| Supersedes | — |
| Amended by | [ADR-0011](0011-author-workflows-as-typed-dot-graphs.md), state identity and Linear projection only |

## Decision

Use SQLite WAL as the completion boundary. Commit normalized events, attempt
outcomes, transition decisions, desired Linear state, and projection outbox
work in one local transaction. Treat Linear and Logfire as asynchronous,
rebuildable projections.

Retain every canonical SQLite row indefinitely for now. Do not automatically
delete, prune, or vacuum the ledger. Treat Logfire Personal's 30-day retention,
as checked on 2026-08-28, as a property of the external view, never as the
factory's retention boundary.
Rebuild external views through durable replay sessions that record destination,
initiator, command ID, fixed event range, item attempts, failures, and
completion. Repeating a command resumes its unfinished items. Require sinks to
deduplicate at-least-once delivery using canonical `event_id`. Keep the
envelope for an `event_id` immutable; current state is derived by folding its
ordered transitions. Include entered and completed attempt facts so evidence,
outcome, and feedback remain attributable to the work that produced them.

Keep raw provider events in separate redacted NDJSON streams. Use the SQLite
event sequence as canonical replay order and UUIDv7 identifiers for correlation.
Before persistence, recursively replace values under keys containing
`authorization`, `cookie`, `password`, `secret`, `token`, or `api_key`. This is
a minimum field-name policy, not content inspection; producers must use honest
field names and must not put credentials in arbitrary strings.

Run one factory with a registry of project lanes. Scope work items by stable
project key and number repeated executions as `<issue>`, `<issue>-2`, and onward;
idempotent retries keep the original execution. Snapshot intent separately for
each execution. State IDs exactly match their Linear status names. Bind each
ledger to one `factory_id` so a second
configuration cannot silently take over the same database. Keep each registered
project's tracker identity immutable and unique.

Record every observed human Linear status change. Commit authorized changes and
record rejected or no-op observations without changing accepted local state.
Require a durable human review record when leaving `Review`; target requested
changes at a pending handoff, then move them to the next rework attempt when its
agent claims it. Cancel that pending handoff if Linear returns to `Review` first.
Expire abandoned resource leases and fence their former owners from renewing or
releasing a replacement lease.

## Why

Neither a runner exit nor a successful remote write proves that the workflow
transition was accepted. Local acceptance must survive crashes and remote
outages without rerouting completed work.

## Consequences

- Good: restart, replay, and audit do not depend on Linear or Logfire.
- Good: late completions are rejected by attempt fencing.
- Good: temporary or premature Linear movement remains auditable without becoming truth.
- Good: one factory can select among several registered projects.
- Good: a human can request rework before a coding agent has been assigned.
- Good: short external retention cannot erase the long-term execution record.
- Good: projection loss can be repaired without erasing prior delivery history.
- Cost: projection workers must retry and report lag.
- Cost: local storage grows until a later measured retention policy replaces
  indefinite retention.
- Not included: live Linear, Logfire SDK, runner, or resource-provider adapters.

## Alternatives

- **Logfire as the ledger** — rejected because observability availability cannot gate completion.
- **Files only** — rejected because multi-record completion cannot be atomic.
- **Linear as source of truth** — rejected because internal states and attempts are not Linear entities.

## Revisit when

- One local writer can no longer own all workflow transitions.
- A storage report shows that indefinite local retention has unacceptable cost.
