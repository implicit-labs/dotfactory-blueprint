---
module: SQLiteLedger
symptom: "Terminal cancellation leaves runner, dispatch, and attention records live"
root_cause: "The workflow transition completed the attempt without terminalizing its owned operation records"
solved_date: 2026-09-02
tags: [cancellation, scheduler, runner, attention, sqlite]
---

# Terminal cancel leaves live ledger records

## Problem

An execution reached `Canceled`, but its attempt-owned runner remained `running`,
its scheduler dispatch remained `attention`, and the linked attention remained
`open`. The terminal summary still presented the historical scheduler attention
as current.

## Solution

`SQLiteLedger.accept_transition` now closes live records owned by the canceled
attempt in the same transaction:

- runner: `canceled`, without a result or receipt;
- dispatch: `superseded`, preserving its prior ambiguity error;
- open attempt attention: `canceled`, with resolution evidence.

Completed summaries retain historical errors but omit them from the headline.

## Why It Works

The attempt ID is the ownership boundary shared by runner, dispatch, attention,
lease, and allocation records. Transactional terminalization prevents a restart
from treating pre-cancel work as live while preserving the original ambiguous
side-effect evidence.

## Prevention

For terminal transitions from work states, assert every attempt-owned live span
has a terminal record. Also assert unrelated execution and attempt resources are
unchanged and replay the control command after reopening SQLite.

## Related

- [ADR-0013](../../decisions/0013-schedule-from-durable-attempt-facts.md)
- [ADR-0019](../../decisions/0019-compose-one-recoverable-lifecycle.md)
