# ADR-0009: Route mobile control through the durable kernel

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-28 |
| Deciders | Project maintainers |
| Source | Public baseline |
| Supersedes | — |

## Decision

Expose a versioned, provider-neutral observation and control API over the local
ledger and durable kernel. Mobile clients read bounded, redacted projections
and submit commands; they do not own workflow state or transition policy.

Require the host transport to authenticate every request and supply a verified
`viewer`, `operator`, or `approver` principal. Ignore actor claims in request
bodies. Do not return attempt or resource fence tokens.

Use one `Idempotency-Key` per command. Persist its normalized request,
principal, authorization decision, result or error, and reconciliation receipt
in the ledger. Repeating the same command returns its prior receipt. Reusing the
key with different inputs fails.

Add `control_command` as a signal only on edges that already grant human
authority. Preserve edge conditions. Require confirmation for cancel, retry,
and terminal generic transitions. Require an `approver` principal and durable
review feedback for approval.

Keep the core transport-neutral. Provide a WSGI adapter, but no hosted server or
authentication provider.

## Why

Phone supervision is useful only if it observes and mutates the same authority
as local runners and Linear reconciliation. A second mobile state machine would
reintroduce split-brain completion and unaudited actions.

## Consequences

- Good: every mobile command is restart-safe, attributable, and reconcilable.
- Good: read-only clients can ship without enabling write roles.
- Good: clients cannot bypass workflow edge authority or acquire fence tokens.
- Cost: the host must provide authentication and map identities to roles.
- Cost: every new command requires an explicit kernel semantic and audit test.
- Not included: scheduler, listener, hosted daemon, Photon, or rich dashboard.

## Alternatives

- **Store mobile state separately** — rejected because two authorities can disagree.
- **Let clients write Linear directly** — rejected because accepted local state may differ.
- **Trust an actor field in JSON** — rejected because request bodies are not identity proof.
- **Expose raw SQLite queries** — rejected because schema details and fence tokens are unsafe contracts.

## Revisit when

- More than one local writer must accept transitions concurrently.
- A deployed transport requires stronger role or delegated-approval semantics.
