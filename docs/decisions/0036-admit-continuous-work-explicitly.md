# ADR-0036: Admit continuous work explicitly and gate dispatch on recorded usage

- Status: Proposed
- Date: 2026-09-18

## Decision

Continuous discovery requires explicit configuration plus an admission label.
Service durable work before new admission; keep one writer and one active child.
Apply optional project/execution provider-token limits before resource preparation.
Persist queue and budget receipts in ledger schema 14.

## Why

Pickup status alone is not permission to consume every issue. Discovering first
can grow the queue while recovery is blocked. Missing usage cannot safely count
as zero, and token telemetry cannot establish a dollar or in-flight spending cap.

## Consequences

- Existing `work` deployments must opt in; explicit `run --issue` stays available.
- Test/demo exclusion and unfinished blockers win over the admission label.
- Priority is Linear priority then age; fresh eligibility is checked at adoption.
- Previously executed issues require explicit restart/rework, never automatic readmission.
- Human checkpoints permit another admission; active/uncertain work does not.
- Project accounting includes all historical executions in the same ledger.
- Configured limits fail closed on missing final usage; stored results can recover.
- Budget changes require an operator configuration edit and coordinator restart.
- No daily reset, price estimation, concurrent writer, resource delegation or
  automatic service activation is introduced.

## Alternatives

- **All pickup statuses imply opt-in** — rejected: ordinary work and canaries become spend authority.
- **Discover before recovery** — rejected: new work obscures unresolved ownership.
- **Hard spend cap from final usage** — rejected: usage arrives after the paid call.
- **Count missing usage as zero** — rejected: repeated failures bypass the limit.

## Revisit when

Provider-enforced reservation/cancellation supports a hard cap, or separate
concurrent-writer and resource-ownership proofs permit parallel execution.

## Links

- [Changelog](../changelogs/2026-09-18-continuous-admitted-queue.md)
- [Operator contract](../../factory/CONTINUOUS_WORK.md)
- Tests: `factory/tests/test_work_queue.py`
