# ADR-0037: Check worker readiness before allocation

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-18 |
| Deciders | Maintainers |
| Supersedes | — |

## Decision

Extend the frozen stage placement policy with bounded, operator-authored readiness
commands. Run them on each candidate worker before workspace allocation or agent
launch. Require explicit successful reports; old workers cannot silently bypass
requested probes. Keep external activity status derived from the frozen workflow.

## Why

Tool presence does not establish the required version, simulator runtime, device
or fixture. Discovering missing prerequisites after launch wastes work and can
produce verification on the wrong host.

## Consequences

- Reuse existing transport, credential isolation and candidate preference order.
- Persist successful probe outcomes in the handoff; surface failures as attention.
- Discard command output and bound runtime; trusted probes remain capable of host
  side effects, so operators must use read-only checks.
- Readiness is an observation, not a lease or acceptance proof. Capability
  reservation and verification remain distinct coordinator responsibilities.
- Project human gates as input requests without changing workflow state or
  granting authority to Linear activity replies.

## Alternatives

- Tool-presence checks alone: cannot establish versions or available devices.
- Model-inferred placement: lacks deterministic prerequisite proof and remains
  a separate advisory experiment.
- Agent-authored probes: would let the work being checked define its eligibility.

## Revisit when

Concurrent workers need exclusive capability bundles or probes need capabilities
that cannot be safely observed without preparation.

## Links

- [Operator guide](../guides/worker-readiness.md)
- [Changelog](../changelogs/2026-09-18-worker-readiness-feedback.md)
