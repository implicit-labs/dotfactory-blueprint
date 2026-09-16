# ADR-0029: Freeze hierarchical telemetry delivery

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-07 |
| Deciders | Project maintainers |
| Extends | [0017](0017-keep-execution-traces-authoritative.md) |

## Decision

Freeze a versioned, payload-free upload plan before sending telemetry. Persist
structural parents, unique observation leaves, source coverage, batch bytes,
hashes and aggregate outcomes in projection-owned SQLite tables. Version these
tables independently of execution-ledger migrations; they are rebuildable view
state, not runner or workflow authority.

Use immutable zero-duration anchors for execution, state, attempt and resource
ownership. Label them as derived structure with unknown duration. Observations
retain their own timestamps and failure status; later facts never rewrite an
already-exported anchor or masquerade as a second lifecycle span with its ID.

Batch outgoing spans, not arbitrary slices of raw lifecycle records. Confirm a
source record only after its observation and required ancestors are accepted.
Commit batch acknowledgement and newly satisfied source receipts together.
Unknown delivery may replay the identical saved bytes and stable IDs; do not
promise exactly-once behavior from an HTTP acknowledgement.

Keep mapping versions in delivery identity. Old receipts remain evidence but
cannot suppress a new mapping. Retain partial acceptance as aggregate uncertain
coverage and stop automatic retries; do the same for permanent HTTP failures.
Warnings with zero rejected spans may confirm the batch once.

## Why

The failed-run canary exposed twelve records exported as seven distinct span
IDs and two missing parents. A successful minimal upload proved ingestion, not
hierarchy. Grouping only within an upload page would fail again across pages or
incremental ranges. OTLP partial acceptance does not identify rejected items.

## Consequences

- Good: live uploads preserve ownership and replay byte-identically after a crash.
- Good: partial delivery cannot masquerade as complete source coverage.
- Cost: a durable upload plan duplicates sanitized view facts locally.
- Cost: blocked partial/permanent delivery requires an explicit repair decision.
- Not included: replacing SQLite, terminal-only export, hosted datasets, or
  importing raw prompts and tool payloads into telemetry.

## Alternatives

- **One raw record per raw span ID** — rejected: lifecycle updates collide.
- **Flatten missing parents to the execution root** — rejected: loses ownership.
- **Wait for execution termination** — rejected: removes live observability.
- **Retry an entire partially accepted batch** — rejected by the OTLP contract.

## Revisit when

- The destination supports acknowledged, versioned updates to open spans.
- Projection-plan retention prevents bounded local storage operations.

## Evidence

- [Changelog](../changelogs/2026-09-07-trace-delivery-and-agent-receipts.md).
- [OTLP partial success](https://opentelemetry.io/docs/specs/otlp/#partial-success-1).
- `factory/src/dotfactory/telemetry.py` and `factory/tests/test_telemetry.py`.
