# ADR-0033: Place stages on owned workers and accept Git handoffs centrally

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-07 |
| Deciders | Maintainers |
| Supersedes | — |

## Decision

Keep one coordinator and authoritative SQLite ledger. Select a user-owned worker
before preparation, snapshot placement policy and native runner settings per
execution, and transfer exact Git commits between stage-specific workspaces.
Accept native results, required checks, and source import before graph completion.

Expose optional `execution` configuration alongside the existing schema-6
contract. Its additive `execution_policies` and `worker_handoffs` tables are owned
by the execution module; canonical event/outbox writes share their transaction.
Worker manifests and process logs are recovery artifacts, not workflow authority.

Use native Claude Code/Codex authentication on each worker. Billing is explicit;
all fallback candidates for a stage use the same billing method. Do not proxy
subscription credentials or migrate personal sessions through the coordinator.

## Why

A stage's toolchain requirements can differ from the issue's repository type.
A local path or native conversation ID cannot identify source on another host.

## Consequences

- Existing local-only configurations retain their behavior.
- Cloud precheck fails closed on unknown requirements, unavailable auth, and
  coordinator-owned resource handles. Physical/session providers remain outside this worker contract.
- Source preparation and import have durable intent, binding, and replay checks.
- A worker enforces its own lease/deadline; stale results cannot commit locally.
- Started attempts are never relaunched automatically; ambiguous work is retained.
- Host change does not invent a workflow status or bypass human review/merge.
- Prior conversation and review-feedback transfer is outside this contract.

## Alternatives

- **Linear owns dispatch/completion** — rejected; external outages cannot rewrite
  accepted attempts or source evidence.
- **One shared workspace on a VM** — rejected; concurrent native sessions can
  overwrite each other's work.
- **Copy full harness homes** — rejected; they contain credentials and personal
  sessions beyond the portable skill boundary in ADR-0003.

## Revisit when

- Multiple writers are required, bundle limits exclude measured workloads, or
  physical device/session requirements need a distributed reservation authority.

## Links

- Setup: [Cloud execution](../../factory/CLOUD_EXECUTION.md)
- Evidence: `factory/tests/test_execution.py`
- Changelog: [Owned-worker execution](../changelogs/2026-09-16-owned-workers.md)
