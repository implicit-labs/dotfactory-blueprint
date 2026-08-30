# ADR-0017: Keep execution traces authoritative

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-30 |
| Deciders | Project maintainers |
| Source | Public release |
| Supersedes | — |

## Decision

Persist versioned trace records, normalized error facts, and projection receipts
in SQLite. Write each trace record in the transaction that commits its source
fact. Use one ledger-assigned trace sequence for trusted order; provider time is
annotation only.

Keep lifecycle events, runner streams, trace records, and error facts distinct.
Build trace hierarchy from durable entity IDs and workflow snapshots. Represent
duration boundaries as spans, point observations as events, and capture loss or
migration uncertainty as completeness facts. Schema-9 migration marks ordering
as reconstructed instead of inventing chronology.

Make external delivery destination-neutral and fixed-range. Record immutable
attempts, item receipts, redacted rejections, and monotonic watermarks before a
destination advances. External viewers, including Logfire, are rebuildable and
cannot change execution completion.

Keep contracts dependency-free and canonically serialized. Pydantic, Logfire,
and OpenTelemetry SDKs remain optional projection-edge tools. A Logfire write
token may only ingest telemetry; a separately supplied project API key may only
call explicitly enabled dataset APIs. Persist neither secret.

## Why

Separate lifecycle and runner sequences cannot produce one trustworthy
waterfall after a crash. External retention, partial acceptance, and provider
clock skew cannot be allowed to erase, reorder, or upgrade local facts.

## Consequences

- Good: local traces, errors, and projection progress survive restart and
  external outages.
- Good: bounded readers and fixed ranges make replay and crash recovery
  deterministic.
- Good: destination adapters can change without migrating canonical execution
  history.
- Cost: one source mutation writes an additional normalized record and index
  entries.
- Cost: adapters must append per-item outcomes and advance watermarks explicitly.
- Not included: waterfall HTML, HTTP trace routes, OpenTelemetry export, Logfire
  setup, and datasets; later changes own those views.

## Alternatives

- **Make Logfire authoritative** — rejected because availability and retention
  of a viewer cannot define factory completion or history.
- **Merge runner payloads into lifecycle events** — rejected because raw
  provider volume, trust, and retention differ from kernel facts.
- **Order by provider timestamp** — rejected because skew could create false
  chronology and negative durations.
- **Require Pydantic models in the runtime** — rejected because the ledger
  contract must work in the stdlib-only factory boundary.

## Revisit when

- One SQLite writer can no longer assign the authoritative trace order.
- A stable cross-provider protocol supplies stronger ordering and trust than
  local observation.

## Evidence

- `factory/src/dotfactory/observability.py`
- `factory/tests/test_observability.py`
