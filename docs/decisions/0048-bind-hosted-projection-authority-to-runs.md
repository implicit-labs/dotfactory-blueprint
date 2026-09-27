# ADR-0048: Bind hosted projection authority to runs

- Status: Proposed
- Date: 2026-09-26

## Decision

Freeze each run's hosted channel choices and instance registration digests at
admission. Recheck destination identity, delivery age and redaction at the send
boundary, including retries. This complements local-copy policy in ADR-0047.

## Why

A queued payload must not bypass opt-out, follow a changed endpoint or become
less private because mutable defaults changed after admission.

## Consequences

- Existing adapter registrations own endpoints and credential references; no per-run raw credentials or arbitrary tenants.
- Instance constraints can tighten, never grant previously absent send authority.
- Frozen pending bytes that no longer comply are blocked, not rewritten.
- Intentional skips are durable processing dispositions, not successful provider receipts.
- Metadata allowlists replace field-sensitive rendered output conservatively.
- Delivery expiry is not provider retention; remote deletion remains unsupported.
- Explicit legacy-v0 migration records current registrations, not unprovable historical authority.
- Independent tables preserve the canonical ledger schema; unmanaged library compatibility is separate from managed runtime guarantees.

## Alternatives

- Gate only when queueing: rejected; retries outlive configuration changes.
- Treat skipped data as accepted: rejected; reports would falsely imply remote evidence.
- Add arbitrary endpoint routing: deferred; current provider adapters do not support that authority boundary.

## Revisit when

Provider-specific deletion receipts and independent tenant credentials can be
verified without silently changing prior-run authority.

## Links

- [Operator guide](../guides/hosted-projection-policy.md)
- [Changelog](../changelogs/2026-09-25-local-review-evidence.md)
- Evidence: `factory/tests/test_projection_policy.py` (offline, fake providers).
