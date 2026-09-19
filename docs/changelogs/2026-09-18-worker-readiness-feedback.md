# Check worker readiness and clarify human review feedback

- Run bounded, operator-defined readiness probes before allocation. Freeze them
  with placement policy; missing worker reports reject the candidate.
- Derive human input requests from each execution's frozen workflow without
  granting approval authority to activity replies.
- Retain the exact known Codex skill-budget notice as a warning only in an
  `item.completed` error item. Other error and failed-turn frames remain errors.
- Preserve bounded, redacted nested error diagnostics as the prerequisite for
  distinguishing warnings from failures.
- Add regression coverage for rejected prerequisites, old workers, timeouts,
  frozen policy, durable warnings and replay-safe human input requests.

[Decision](../decisions/0037-check-worker-readiness-before-allocation.md) ·
[Configuration](../guides/worker-readiness.md)
