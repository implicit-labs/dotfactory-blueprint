---
module: factory
symptom: "Ready issues reset; rework loses context; retained work is later deleted"
root_cause: "Lifecycle composition did not consume existing ledger contracts"
solved_date: 2026-09-07
tags: [lifecycle, linear, retention, control]
---

# Preserve supervised handoffs

| Failure | Repair | Regression |
|---|---|---|
| Ready adoption queues Todo | Snapshot the issue and adopt its eligible checkpoint before projection | Ready stays Ready; foreign and terminal issues rejected |
| Runner cannot read requirements or feedback | Capture description and a bounded immutable attempt handoff | Actual rework prompt contains requirement, feedback, and prior evidence |
| Human transition stays pending | Consume pending transitions before automatic pickup | Planning wins; Reworking claimed once |
| Retain only resolves attention | Persist cleanup policy; require explicit safe release | Clean retained files survive restart |
| Cleanup indexes another project's engine | Scope cleanup to selected projects | Foreign workspace untouched |
| Watch prevents local control | Pump owner-only socket on the sole writer thread | Active owned process canceled; command replay idempotent |
| Tick count overshoots a checkpoint | Stop at exact state before next dispatch | Ready stop and restart create no implementation attempt |
| Canceled is rendered as Done | Label terminal state and prioritize attention | Cancellation never claims recovered incidents |

Run `./factory/test.sh`. Tests use disposable Git repositories and local child
processes; they do not prove live provider output quality or Linear delivery.
See [setup](../../SETUP.md) for operator commands and
[ADR-0023](../../decisions/0023-preserve-supervised-handoffs.md) for the boundary.
