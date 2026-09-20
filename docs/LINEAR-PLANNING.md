# Planning in Linear

## User journey

1. Move an issue to **Planning** in an enabled project.
2. Open Dotfactory's native agent discussion on the issue. The first message says
   planning is queued; an eligible, awake worker investigates the repository.
3. Read one contextual question batch. Each question explains the decision,
   recommends an option and names the tradeoff. Reply naturally, for example:
   “Compact dock only. Expand the selected page; hover should only show a tooltip.”
4. Dotfactory saves your answer, batches nearby replies and revises the same plan.
5. Read **Plan v2**: intended behavior, scope, verification environment, checks,
   required screenshots/recordings and changes from project defaults.
6. Reply **approve plan v2** to authorize that revision's implementation. “Yes,”
   numbered answers and status changes do not approve a plan. An older revision,
   unresolved question or later answer requires another review.

The default conversation concerns product behavior and verification. Ask for
implementation detail when it helps a particular run or step. A browser or iOS
simulator requirement is separate from unit tests and from the evidence captured.

| Reply | Effect |
|---|---|
| An answer or requested change | Saved in the planning conversation; revised before approval |
| `approve plan v2` | Configured approver authorizes the displayed revision |
| `pause` | Prevents the next stage from dispatching; an active stage may finish |
| `resume` | Resumes the paused run; incorporates saved answers before approval |
| `stop` / native stop signal | Cancels the run; does not assert remote process exit |

Reply in the **existing agent discussion**. A mention that creates another session
is not automatically attached. Editing an old message is not a new planning turn;
send a new reply. Messages sent during a planning attempt are recovered after that
attempt finishes; its frozen context is never changed underneath it.

## Project setup

The instance configuration is JSON. Register `factory/workflows/linear-planning.dot`
as a workflow and select it for the project. Keep the existing trusted worker and
Agent Session projection configuration. Register verification methods when the
project requires browser, simulator or other behavior evidence.

Add this project setting, replacing example IDs and profile URL:

```json
{
  "linear_planning": {
    "enabled": true,
    "organization_id": "workspace-uuid",
    "app_user_id": "dotfactory-app-user-uuid",
    "participants": {
      "reviewer-user-uuid": "approver",
      "collaborator-user-uuid": "operator"
    },
    "reviewer_urls": ["<Linear reviewer profile URL>"],
    "quiet_seconds": 5
  }
}
```

- Replace every angle-bracket placeholder before loading the JSON. Reviewer URLs
  use Linear's workspace profile URL shown by the configured account.
- Enable `projections.linear.enabled` and `agent_sessions_enabled`; configure the
  private app OAuth token through the existing `agent_token_env`, never inline.
- Give the app access to the selected team/project and the existing Agent Session
  scopes. The owner reads session activities and publishes through the same app.
- Run the configured owner continuously (`work`, with `work_queue.enabled=true`). Planning
  admission is opt-in per project and does not require the normal admission label.
  Excluded TEST/demo labels and blocking dependencies still prevent admission.
- The workflow must expose a Planning pickup, `Planning` with `plan-result-v2`,
  and `PlanReview`. All projects without this setting retain existing behavior.
- The owner must remain awake. No receiver deployment or service resume is done by
  this code. If the owner is offline, messages remain in Linear; response latency
  includes that offline time. Discovery sees current Planning state, not historical
  transitions that happened and were reversed while offline.
- Remove a participant to revoke access immediately. Adding a new participant does
  not expand the admitted run's authority; configure them before a new run.

## Notifications and recovery

Questions, plan review and rejected actions use native elicitations. Reviewer
profile links become mentions. Routine tool output is not sent to the discussion.
The existing outbox reuses activity IDs after retries and unknown outcomes.

Actual Inbox/push/email delivery depends on Linear preferences. The queued message
does not claim that a worker is already running. An offline worker cannot publish
fresh offline status; inspect the run link or restart its owner.

## Validation boundary

Automated tests exercise a real local Git repository, durable ledger, workflow,
revision approval and delivery gate with fake Linear responses. They also test
pagination failure, identity mismatch, duplicate input/output, pause/cancel,
crash recovery, fair background reads and approval isolation from code review. They do not prove model question quality or live notifications.

Before enabling for product work, use a private test issue to verify: Planning
pickup, a real planner question batch, an actual human reply, exactly one attention
notification, the revised plan, stale approval rejection, and explicit current
revision approval. Keep source merge and deployment approval separate.

## Summary and expandable detail

Plan reviews show a short prose preview and a native **Full plan and scope**
collapsible section. Verification/evidence, project overrides, questions, and
approval instructions remain outside the collapsed section. The frozen plan is
preserved verbatim; plans containing toggle delimiters or unclosed code fences are shown as expanded
literal Markdown so they cannot swallow the following approval instructions.
Previously published revisions keep their original immutable presentation.

Before activation, verify the collapsed detail, native Agent Activity rendering
and notification delivery with the configured app identity. Comment rendering or
a user API token does not prove the app-actor path. This format applies only to
planning reviews; other update types retain their current presentation.
