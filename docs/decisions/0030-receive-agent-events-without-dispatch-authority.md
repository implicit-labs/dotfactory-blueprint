# ADR-0030: Receive agent events without dispatch authority

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-07 |
| Deciders | Project maintainers |
| Extends | [0019](0019-compose-one-recoverable-lifecycle.md) |

## Decision

Host a separate receipt-only HTTPS receiver for Linear `AgentSessionEvent`
webhooks. Authenticate the raw body with the app signing secret, verify its
timestamp and configured organization/app identity, and commit a bounded
metadata receipt before acknowledging delivery.

Keep its SQLite inbox and credentials separate from the factory's execution
ledger. Retain identifiers, action, hashes and timestamps, not prompt bodies,
issue descriptions or authorization values. Repeated delivery is idempotent;
identity reuse with conflicting content is rejected.

This service has no runner, task-launch, status-mutation or resource authority.
An HTTP acknowledgement means received, not acted upon. Native session updates
belong to a separately authorized outbound publisher, not this receiver.
Prompt-driven dispatch is explicitly unsupported until a reviewed consumer exists.

Deploy with a persistent volume, runtime-only secret injection, bounded request
reads and health/readiness probes. Bind only the dedicated app's event category;
do not broaden OAuth scopes or subscribe to unrelated workspace events.

## Why

Linear rejects native session creation when Agent Session events are disabled.
Enabling the category requires an actual HTTPS receiver. Hosting a receiver
must not silently grant external events authority to execute work.

## Consequences

- Good: webhook receipt remains available independently of a sleeping runner host.
- Good: retries and restarts preserve auditable receipt identity without raw content.
- Cost: a hosted service, persistent volume and signing-secret lifecycle.
- Cost: operators must not advertise this app as a prompt-driven agent yet.
- Not included: inbound prompt dispatch, OAuth installation flow, or a public
  control API. Receipt retention/backup is an explicit hosting operation.

## Alternatives

- **Dummy webhook URL** — rejected: enables UI while losing real events.
- **Temporary tunnel as production** — rejected: availability depends on a local session.
- **Run agents in the webhook handler** — rejected: exceeds timeout and authority.

## Revisit when

- A reviewed inbox consumer can map signed events into authorized kernel commands.
- Volume retention or request throughput requires a different inbox store.

## Evidence

- [Changelog](../changelogs/2026-09-07-trace-delivery-and-agent-receipts.md).
- [Official Linear webhook schema](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql).
- `factory/src/dotfactory/agent_webhooks.py` and `factory/tests/test_agent_webhooks.py`.
