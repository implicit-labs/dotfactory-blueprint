# Trace delivery and signed agent receipts

- Freeze unique observation spans and structural ownership anchors before OTLP
  delivery. Persist bounded batches, exact retry bytes and ancestor-aware source
  receipts; block partial/permanent rejection and malformed source without
  treating them as successful exports.
- Add a signed, freshness-checked, app-bound Linear webhook inbox on its own
  durable SQLite store. It records metadata only and never dispatches prompts.
- Keep canonical ledger migrations separate from projection-owned state. Record
  the boundaries in ADRs 0029/0030 and provide non-root deployment instructions.
- Preserve configurable Logfire project/region identities. Version destination
  receipts and verify that different projects never share accepted coverage.

Failed-run fixtures verify complete parent ownership, unique observation IDs,
safe failure facts, exact-byte restart delivery, and partial-upload handling.
Successful HTTP ingestion alone is not proof of the hosted hierarchy.

Hosted webhook deployment, container/volume/HTTPS verification, native Linear
session proof and inbound dispatch remain separate operator verification gates.
