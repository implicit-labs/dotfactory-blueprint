---
module: factory.telemetry
symptom: "Failed-run uploads reused span IDs and referenced missing parents"
root_cause: "Entity lifecycle records were exported independently as mutable entity spans"
solved_date: 2026-09-07
tags: [otlp, logfire, retries, durable-delivery]
---

# Preserve trace identity across delivery

## Failure

The retained failed fixture contained 12 records but only seven unique span IDs
and two missing parents. A three-span ingestion smoke did not exercise a runner
lifecycle. Successful HTTP ingestion was not hierarchy proof.

## Repair

- Derive a unique observation leaf from every source record; freeze separate,
  explicitly structural ownership anchors.
- Persist the complete versioned plan before transport. Bound batches by actual
  span count, including anchors, and retain exact bytes across restarts.
- Acknowledge a source only after its leaf and every ancestor are accepted.
  Commit batch acknowledgment and source receipts atomically.
- Serialize senders with a crash-released lock. Partial success has unidentified
  accepted spans: retain the aggregate result and never automatically resend it.
- Keep the export namespace separate from v1 receipts and the canonical ledger
  schema. Do not mirror delivery receipts back into their own input trace.

Flattening the hierarchy would hide the defect by dropping ownership. Merely
fixing the mapper would leave batching, crash recovery, and receipt claims wrong.

## Regression contract

`factory/tests/test_telemetry_delivery.py` runs a real failed ScenarioRunner,
checks unique identities, acyclic parent closure and failure facts, and tests
single-span batches, incremental ownership, restart, receipt-write crashes,
competing senders, partial rejection, permanent failures, and old receipts.

The synthetic full lifecycle produces 25 source observations and 14 structural
anchors. Structural spans do not claim measured duration or successful execution.
Hosted hierarchy inspection remains a separate live gate from these offline tests.

See [ADR-0029](../../decisions/0029-freeze-hierarchical-telemetry-delivery.md).
