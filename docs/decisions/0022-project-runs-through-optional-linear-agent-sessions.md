# ADR-0022: Project runs through optional Linear Agent Sessions

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-07 |
| Deciders | Project maintainers |
| Source | Public release |
| Supersedes | —; extends [ADR-0020](0020-project-readable-run-evidence-into-linear.md) |

## Decision

Use one Linear Agent Session per execution when an agent OAuth actor and an
external run URL are configured. Emit only start, workflow-state, attention,
error, and terminal activities. Keep the local ledger authoritative.

Persist the unique run URL, remote session ID, caller-generated UUIDv4 activity
IDs, request hashes, attempts, errors, and confirmations. Reconcile an unknown
session create by the unique run URL. If it cannot be identified exactly, never
create again; use the ADR-0020 owned comment instead.

Replace Agent Session external URLs when trace, failure, evidence, or pull
request links appear. Reconcile an unknown update by reading the exact session.
Use the owned comment immediately when the preview API is unsupported or rejects
the configured actor.

## Why

Linear is the operator control panel, but presentation cannot become workflow
authority. Agent Sessions add native progress and terminal semantics while the
durable ledger and existing comment fallback protect recovery from preview API
changes.

## Consequences

- Good: one issue exposes native run progress and one-click execution evidence.
- Good: activities are immutable, sparse, and idempotent across restarts.
- Good: unsupported preview behavior degrades to the existing readable comment.
- Cost: Agent Sessions require an OAuth app actor; a personal API key is insufficient.
- Cost: an unreconciled create may leave an inactive remote session plus the fallback comment.
- Not included: webhook-driven prompts, plans, repository suggestions, or control authority.

## Alternatives

- **Require Agent Sessions** — rejected because the API is preview-only and actor setup is not universal.
- **Retry an unknown create** — rejected because the first write may have succeeded.
- **Mirror runner frames** — rejected because tool noise hides meaningful progress.
- **Make Linear session state authoritative** — rejected because local recovery must not depend on a remote presentation.

## Revisit when

- Linear accepts a caller-generated idempotency key for proactive session creation.
- Agent Sessions leave preview or replace their OAuth actor contract.

## Evidence

- Changelog: [Linear Agent Sessions](../changelogs/2026-09-17-linear-agent-sessions.md)
- Tests: `factory/tests/test_linear_agent.py`
