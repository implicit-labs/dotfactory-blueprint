# Continuous work

Use `run` for one explicitly selected issue. Use `work` for repeated, opt-in
Linear discovery. Both use one ledger owner; never start a second coordinator
against the same ledger or a copied live database.

## Enable a queue

1. Initialize an instance with `init`; authenticate its native runner.
2. Enable its Linear projection and configure the owning Linear project/team.
3. Set `scheduler.limits.host` to `1`; project/runner limits must also be `1`.
4. Add this configuration, choosing limits appropriate for the project:

```json
{
  "work_queue": {
    "enabled": true,
    "admission_label": "factory-ready",
    "excluded_labels": ["TEST", "demo"]
  },
  "budgets": {"project_limit": 200000, "execution_limit": 60000}
}
```

`init` does not enable discovery. Existing configurations without `work_queue`
cannot start `work` until explicitly opted in. Explicit `run --issue` does not
require an admission label; configured budgets still apply to its dispatches.

## Admit work

- Add the configured admission label and put the issue in a workflow pickup
  status (`Todo` or `Ready` in the supplied workflows).
- Labels match case-insensitively. Excluded labels win over admission.
- Unfinished incoming `blocks` relations prevent admission. Completed/canceled
  blockers do not. Missing or truncated eligibility data fails closed.
- Priority order is Urgent, High, Normal, Low, then unspecified; ties use oldest
  creation time, issue identifier, then project key. Discovery covers all pages,
  with a fail-closed 100-page safety bound. Nested labels/relations over 100 are
  excluded until their full eligibility can be established.
- The selected issue is re-read immediately before adoption. The same snapshot
  supplies its intent and initial tracker observation.
- An issue with any previous execution is not automatically readmitted, even
  after completion/cancellation. Use explicit `run --issue` for an intentional
  new execution; use operator feedback for rework.

Existing work is serviced first. Human checkpoints free the queue to pick up
another issue; active attempts, resource waits and unresolved attention do not.
Canceled runners with unproven exit block the queue until their workspace is
explicitly released after inspection. Never infer process exit from cancellation.
Human approval remains required at the workflow's human gates. Autoplanning
continues automatically after its plan contract passes; Planning uses PlanReview.

See [admission examples](QUEUE_ADMISSION_EXAMPLES.md) for concrete decisions.

## Start, inspect, drain, restart

```bash
PYTHONPATH=/absolute/dotfactory/factory/src python3 -m dotfactory work \
  --config /absolute/instance/factory.json --project example

PYTHONPATH=/absolute/dotfactory/factory/src python3 -m dotfactory status \
  --config /absolute/instance/factory.json --project example

PYTHONPATH=/absolute/dotfactory/factory/src python3 -m dotfactory operator drain \
  --config /absolute/instance/factory.json --project example
```

- Empty queues use tracker polling, not model calls. `--max-ticks N` bounds a test invocation;
  it limits scheduler ticks, not wall time or model calls.
- Drain lets an active stage finish, prevents another dispatch/admission, then
  exits successfully. The operator socket remains serviced while idle.
  Discovery also services it between pages; an interrupted scan admits nothing.
- SIGINT/SIGTERM requests cancellation, not graceful drain. Inspect its receipt
  before restarting; ambiguous launches require attention, never blind replay.
- Restart the same command with the same ledger. Existing work is recovered
  first; admitted issues are not duplicated.
- `status` and `GET /v1/runs` expose the last 25 `operating_receipts` (queue and
  budget decisions). Inspect their timestamps; a stopped owner's receipt is
  historical, not proof of a live service. OS service status proves the process.

## Budget contract

Limits count provider-reported tokens, not dollars, subscription credits or
remaining account allowance. Project limits cover the entire project's history
in this ledger, including completed/canceled executions. Execution limits cover
all stages/retries of one execution. Neither resets on process restart or midnight.

| Provider | Counted source |
|---|---|
| Codex | Sum of final `turn.completed` usage |
| Claude Code | Last cumulative `result` usage, not per-message usage plus result |
| OMP | Sum of `message_end` usage |

Cached tokens follow the existing provider normalizer: explicit totals win;
otherwise input/output plus separately reported cache reads/writes are counted.
Require input and output fields and a durable runner result. Failed/incomplete
runs, missing usage, unknown adapters and dropped event streams block the next
dispatch when their scope has a configured limit. Unknown usage is never zero.

At or over a limit, no new preparation or model call starts. A call already
running may exceed the remaining allowance; this is **not a hard spending cap**.
Stored-result recovery/cleanup remains possible without another paid call.
Limits apply to explicit runs as well as queues. With no `budgets`, operation is
explicitly `unlimited`.

To resume after a limit: drain, inspect status, deliberately raise the relevant
limit, then restart. Missing accounting needs repair; raising the number alone
does not bypass it. Removing a limit is an explicit choice to run without that
protection. Do not delete the ledger to reset usage.

See [budget troubleshooting](BUDGET_TROUBLESHOOTING.md) for recovery commands.

## Linux user service

Use a dedicated, reviewed checkout and durable local-disk instance. Install
Python 3.9+ and the configured coding CLI, then authenticate that CLI as the
service user. Do not run from a temporary review worktree. Keep the checkout
fixed while a coordinator is running.

Create `~/.config/systemd/user/dotfactory.service`, replacing all absolute paths
and the project key:

```ini
[Unit]
Description=Dotfactory explicitly admitted queue

[Service]
Type=simple
WorkingDirectory=/absolute/dotfactory
Environment=PYTHONPATH=/absolute/dotfactory/factory/src
Environment=PATH=/absolute/cli/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=/absolute/instance/service.env
ExecStart=/usr/bin/python3 -m dotfactory work --config /absolute/instance/factory.json --project example
Restart=on-failure
RestartSec=30
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=default.target
```

Create the private `service.env` with only required environment references
(for example `LINEAR_API_KEY`); restrict it to mode `0600`. Never commit it or
include its contents in evidence. Native CLI authentication stays with its user.

```bash
systemctl --user daemon-reload
systemctl --user start dotfactory
systemctl --user status dotfactory
journalctl --user -u dotfactory -n 50
```

Use `operator drain` for a graceful stop; `Restart=on-failure` does not restart a
successful drain. Use `systemctl --user stop dotfactory` only when cancellation
is intended. `systemctl --user start dotfactory` resumes the queue. Optional
`enable` starts it at user-manager startup; it is not required for a trial and
does not itself guarantee running after logout. Host linger policy is operator-owned.

This service recipe is not a deployment receipt. Verify native auth, a real
issue-to-Review run, restart and drain on the selected host before calling it
unattended-ready. Render uses the same `work` admission policy; see
[cloud execution](CLOUD_EXECUTION.md). No service is installed automatically.
