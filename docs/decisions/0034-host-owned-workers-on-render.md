# ADR-0034: Host owned workers on Render

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Deciders | Maintainers |
| Supersedes | — |

## Decision

Use a persistent Render background worker for portable coding stages, reached
through the existing SSH transport, or colocate one coordinator and local coding
stages in that service. Persist the coordinator ledger, repositories and workspaces
on its `/data` disk. Host Linear's receipt-only listener as a separate Render web
service with its own disk; receipt ingestion grants no dispatch authority.

Keep native login state on the worker's private disk, provisioned by direct
native sign-in. Pin coding CLI versions in the worker image. Deploy manually
after draining work; provider restart or SSH loss requires reconciliation.
Operate the worker on demand with explicit resume/readiness and drain/suspend
steps. Automatic idle suspension is not implemented; idle running time is billed.

## Why

One selected provider can host the listener and coding workers. The existing
worker protocol already uses SSH and independent Git attempts; it does not need
a provider-specific scheduler, credential proxy or second workflow authority.

## Consequences

- Runtime ownership and acceptance rules remain those of ADR0033.
- Render compute/storage and model billing remain separate, explicit costs.
- Mac/Xcode stages retain Mac placement. A Linux worker does not prove iOS output.
- The persistent worker is for trusted operator-owned work, not hostile multi-tenant execution.
- Live native auth, coding, cancellation, restart and returned-Git checks remain
  release gates; a container build or SSH connection alone cannot satisfy them.
- The listener never receives coding credentials. A worker-only service never
  receives Linear credentials. A colocated coordinator explicitly owns them;
  local worker processes filter them from the inherited environment. This is
  trusted single-operator execution, not isolation from hostile agent code.

## Alternatives

- Railway remains an optional existing transport; it is no longer the deployment default.
- Ephemeral Render shells lose instance continuity and are unsuitable for this persistent-worker contract.
- A separate always-on coordinator adds idle cost. Colocation is sufficient for
  portable work; Mac reachability remains a separate gate for native stages.

## Revisit when

Per-task isolation, autoscaling, workload memory or service cost makes the
single persistent service unsuitable, or continuous discovery is required while
coding compute is suspended.

## Links

- [Worker authority](0033-place-stages-on-owned-workers.md)
- [Cloud setup](../../factory/CLOUD_EXECUTION.md)
- [Changelog](../changelogs/2026-09-16-owned-workers.md)
