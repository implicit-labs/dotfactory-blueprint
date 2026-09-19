# Queue admission examples

Use `run --issue` to select one issue explicitly. Use `work` for repeated,
opt-in discovery. Queue discovery requires `work_queue.enabled = true`, an
admission label, and `scheduler.limits.host = 1`; keep the project and runner
limits at `1` too.

Explicit `run --issue` does not require the admission label. Configured budgets
still apply to explicit runs and queue dispatches.

## Admission cases

| Case | Inputs | Result | Reason |
|---|---|---|---|
| `opted-in-todo` | `Todo` plus the configured admission label | Accepted. | The issue is in a pickup status and explicitly opted in. |
| `missing-admission-label` | `Todo` without the admission label | Not admitted. | Pickup status alone does not opt an issue into discovery. |
| `test-plus-opt-in` | `TEST` plus the admission label | Excluded by the default policy. | Excluded labels win over admission. |
| `unfinished-blocker` | Opted in with an unfinished incoming `blocks` relation | Not admitted while the blocker is unfinished. | Completed or canceled blockers do not block admission. |
| `priority-oldest` | Multiple eligible issues | Higher Linear priority is selected first; the oldest issue breaks a priority tie. | Remaining ties use identifier, then project key. |
| `previous-execution` | Eligible issue with any previous execution | Not automatically readmitted. | Use explicit `run --issue` for an intentional new execution or operator feedback for rework. |

## Review and test boundaries

Human approval remains required at the workflow's human gates. Autoplanning
continues after a valid plan contract; manual Planning stops at `PlanReview`.
Delivery stops at `Review` for human review.

A `TEST` issue may be admitted only in a dedicated local test instance whose
policy explicitly permits `TEST` and uses a unique admission label. This is an
isolated exception; the default policy excludes `TEST` work.
