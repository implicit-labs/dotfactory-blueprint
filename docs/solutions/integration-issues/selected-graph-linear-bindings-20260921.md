---
module: LinearConvergenceWorker
symptom: "missing Linear status bindings: Implementing"
root_cause: "Profile selection changed the frozen workflow digest, but Linear bindings existed only for the base digest."
solved_date: 2026-09-21
tags: [linear, workflow, admission, profiles]
---

# Selected graph lacks Linear status bindings

## Problem

A profile-only run reached a Linear projection with a new workflow digest and
failed `require_linear_status_bindings()` even though its status names had not
changed: `missing Linear status bindings: Implementing`.

## Solution

Before admitting a selected run, bind its digest to the configured team's
status IDs. Reuse the base graph's already-verified bindings only when every
selected status name is covered by the same team. If the selected workflow adds
a status, perform Linear preflight for that graph. Never guess a status ID.

## Prevention

Any change that creates a graph variant must test both identical-status reuse
and a newly introduced status. A frozen graph digest and its Linear binding
digest are one dispatch contract, even when only runner fields changed.

## Related

- [Project/run configuration](../../guides/project-run-configuration.md)
- [ADR-0045](../../decisions/0045-freeze-registered-run-selections.md)
