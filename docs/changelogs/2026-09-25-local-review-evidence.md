# Local and hosted evidence policy

- Freeze local destination identity, export mode, redaction and exported-copy retention atomically at admission; preserve old runs with explicit legacy migration.
- Enforce instance constraints during export, expose secret-free settings through preview/doctor/run views, and refuse changed destinations or expired replay.
- Journal export manifests and filesystem identity before publication. Explicit cleanup deletes only expired, unchanged owned copies; canonical evidence remains.
- Add failure tests for two-project isolation, rollback/restart, symlinks, secret-bearing source, tampering, and crash recovery.
- Independent review hardened directory-handle cleanup, no-clobber publication, project-scoped destination allowlists, complete bearer/cookie redaction, and checked artifact hashes.
- Freeze hosted channel selection and existing registration identity; gate Linear status/comments/agent activities, Logfire batches and hosted datasets at send/retry boundaries.
- Separate intentional telemetry skips from accepted receipts. Tightened policy blocks queued bytes without silently rewriting ambiguous retries or redirecting destinations.
- Reopened managed dataset ledgers require their owning configuration before publication. First eligible live tracker read is immediate even when the monotonic clock starts near zero.
- Independent review closed missing-snapshot restart bypasses: durable managed identity survives process reopen, and legacy migration is journaled once per project rather than regranting missing authority.
- Expose metadata-only output and source delivery-age limits. Remote provider deletion/retention and the real web acceptance proof remain unfinished; fake-provider tests are not live delivery proof. No publication or merge authority changes.


[ADR-0048](../decisions/0048-bind-hosted-projection-authority-to-runs.md) · [Hosted guide](../guides/hosted-projection-policy.md)
