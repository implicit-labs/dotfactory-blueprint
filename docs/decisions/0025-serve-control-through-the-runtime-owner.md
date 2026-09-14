# ADR-0025: Serve HTTP control through the runtime owner

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-08 |
| Deciders | Project maintainers |
| Supersedes | — |

## Decision

Host the existing control adapter in a separate authenticated loopback gateway.
Forward active requests to the ledger owner; acquire the exclusive control-only
lock for stopped instances. Never replay a mutation after ambiguous delivery.

## Why

Status must remain available after a worker stops without creating another
scheduler or concurrent ledger writer.

## Consequences

- Gateway startup fixes a bearer token's subject and role; viewer is the default.
- Reads have factory-wide scope. Commands retain audited, idempotent kernel
  authorization and select the execution's owning project.
- The existing same-UID socket is the trust boundary for forwarded principals.
- No init API, public listener, webhook registration, or background admission.
- Bounded socket responses and serialized requests limit this local gateway.

## Alternatives

- **Worker-only HTTP server** — unavailable when work stops.
- **Independent long-lived HTTP ledger writer** — breaks exclusive ownership.
- **Automatic retry after timeout** — may duplicate an accepted mutation.

## Revisit when

A remote client needs authenticated ingress, project-scoped reads, concurrent
requests, or evidence responses larger than the local socket limit.

## Links

- Contract and startup: [Control API](../../factory/CONTROL_API.md)
- Evidence: [HTTP gateway tests](../../factory/tests/test_control_server.py)
