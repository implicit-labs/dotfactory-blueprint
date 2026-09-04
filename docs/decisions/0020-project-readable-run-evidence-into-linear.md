# ADR-0020: Project readable run evidence into Linear

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-02 |
| Deciders | Project maintainers |
| Source | Public release |
| Supersedes | — |

## Decision

Project each execution into one Dotfactory-owned Linear comment. Render that
comment from authoritative SQLite facts and update it only when its digest
changes.

Persist a caller-generated UUIDv4 before `commentCreate`. After any unknown
write outcome, read that exact comment before deciding whether to create or
update. Never scan by prose, append a second run log, or let projection failure
change execution state.

Keep the Linear comment summary-first. Show the ordered workflow nodes traversed
with durable elapsed time and provider-reported token usage for each node. Sum
run-scoped usage when the provider supplies it; otherwise sum per-call usage.
Label missing or partial coverage instead of reporting zero. Put trace IDs,
stable error codes, and links in a collapsed technical section. Keep raw trace
records and verification receipts outside Linear.

Keep verification media under `evidence-to-linear`. That capability may post a
separate artifact report; it does not update the runtime-owned comment. Workflow
skill injection and automatic link handoff remain separate scope.

## Why

Operators inspect the issue first, while the ledger remains the only safe
execution authority. Append-only logs were unreadable and disappeared with an
external trace backend. One rebuildable comment gives Linear durable evidence
without making remote presentation state authoritative.

## Consequences

- Good: an issue shows current outcome and actionable failure without comment spam.
- Good: restart and timeout recovery cannot blindly duplicate the summary.
- Good: the presentation can be rebuilt from the ledger.
- Good: operators can see where wall time and model usage accumulated.
- Cost: comment delivery needs its own durable state and reconciliation.
- Cost: executions recorded before normalized usage capture say `usage unavailable`.
- Cost: a deleted owned comment requires attention instead of silent replacement.
- Not included: full waterfall UI, Logfire retention, destination-wide health,
  or Linear Agent Sessions.

## Alternatives

- **Append a comment per event** — rejected because the issue becomes a log stream.
- **Store only a Logfire link** — rejected because external retention failure erases the readable result.
- **Put the summary in the issue description** — rejected because Dotfactory would overwrite product reasoning.
- **Retry timed-out creates** — rejected because the first create may have succeeded.

## Revisit when

- Linear provides transactional idempotency and compare-and-set comment updates,
  or Agent Sessions become the configured control surface.

## Links

- Evidence: `factory/tests/test_linear_evidence.py`
