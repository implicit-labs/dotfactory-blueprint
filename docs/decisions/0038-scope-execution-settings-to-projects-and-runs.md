# ADR-0038: Scope execution settings to projects and runs

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-18 |
| Deciders | Maintainers |
| Supersedes | — |

## Decision

Resolve execution stage settings from instance defaults, project fields and
explicit run fields, in that order. Omission inherits; a supplied list replaces,
including a valid empty list. Users can increase or reduce project requirements.

Record the resolved policy, route snapshot, run input and per-field origin
atomically with run creation. Retrying or restarting uses that snapshot. Reject
changed overrides for an existing run. Keep worker registration, credentials and
workflow authority outside the override schema.

## Why

One instance can serve an iOS app and a landing page. A shared stage policy cannot
express their requirements, and first-dispatch snapshots can drift while queued.

## Consequences

- Stage overrides allow workers, scope, requires, readiness, checks and deadline.
- Run input is an explicit CLI/Python API parameter, not interpreted issue prose.
- Project configuration edits affect new runs only. Existing legacy snapshots
  stay unchanged; legacy runs without one retain first-placement migration.
- Empty requirements do not remove intrinsic Git/auth checks, requirements
  implied by retained checks, or approved delivery contracts.
- Broader configuration domains remain under their existing resolution rules;
  the audit identifies follow-up work instead of introducing a universal merge.

## Alternatives

- Instance-only policies: force separate instances for unrelated projects.
- Additive-only run requirements: cannot express intentional lighter runs.
- Implicit list union: makes removal impossible and hides inherited constraints.
- Mutable live config: changes queued/restarted runs without an admission record.

## Revisit when

An approved verification plan must change placement after admission. Add an
explicit revision/reapproval protocol rather than modifying an existing snapshot.

## Links

- [User configuration](../guides/project-run-configuration.md)
- [System audit and work packages](../audits/project-run-configuration.md)
