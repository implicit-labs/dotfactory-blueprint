# Planning interface verification

Date: 2026-09-20. Scope: conversational planning with verification-host contracts.

## What was exercised

| Surface | Evidence | Result |
|---|---|---|
| Local CLI | Separate `python -m dotfactory operator` processes talking to the live owner's Unix socket | Message, status, revision and approval exercised |
| Clarification loop | Autoplanning → PlanReview with a question → answer → explicit revision → Planning → PlanReview | Approval blocked until the new proposal had no questions |
| Exact approval | Wrong plan SHA, wrong requirements digest, correct pair, identical replay | Wrong identifiers rejected; correct approval applied once |
| Restart / export | Stop owner; separate `delivery` CLI exports the review packet; reopen runtime | Requirements file exported; conversation/proposal unchanged after restart |
| Delivery | Fixture agent writes real Git commits; real host evaluates pinned Python checks | Approved run reached Review; late planning message rejected |
| HTTP | Real authenticated loopback HTTP → live owner's socket → control service | Bad token rejected; chat replay stable; operator unable to approve despite spoofed role header |
| Native planner prompt | Actual `LiveRunner._prompt` with a real admitted attempt | Captured chat included as untrusted input; later message did not alter the attempt's prompt |
| Invalid input | Blank/non-string/oversize messages; forged context; wrong stages/shape; missing/duplicate checks; invalid questions | Rejected without changing the conversation or granting approval |
| Existing safety cases | Prior conversation/configuration/verified-delivery suites | Project isolation, original admission, reductions, missing capabilities, crash rollback, tamper guards and later revisions covered |

The planner and coder in the transport scenario are deterministic fixtures.
Git, SQLite, socket/HTTP transport, CLI parsing, prompt construction, approval and
host verification are real local paths. No live model or hosted worker is used.

## Observed user journey

1. Todo status advertises `planning_message`; message history is empty.
2. `planning-chat` returns a completed command receipt; an identical retry adds no message.
3. PlanReview status displays “Which wording should the manual review cover?” and coordinator/manual checks. Approval is rejected.
4. Replying “Review the greeting and punctuation.” sets `needs_revision: true`.
5. A revision command with complete `changes_requested` feedback returns to Planning. A `note` alone is insufficient.
6. New PlanReview status has no questions and `needs_revision: false`; both exact identifiers are available.
7. Export/restart preserves that proposal. Exact approval enters Ready; the run reaches Review after implementation and verification.
8. Another planning message at Review is denied.

No chat window or assistant-message stream appeared: status JSON and the committed
plan are the implemented response surfaces. See the [command-by-command user
guide](../guides/planning-conversation.md).

## Reproduce offline

From the repository root with a working Python 3.9+ / SQLite installation:

```sh
PYTHONPATH=factory/src:factory/tests python3 -m unittest \
  test_planning_interface test_planning_conversation \
  test_configuration_contracts test_verified_delivery

/bin/bash factory/test.sh
```

To capture the actual CLI requests and responses locally:

```sh
DOTFACTORY_PLANNING_TEST_TRANSCRIPT=/tmp/planning-ui-transcript.json \
  PYTHONPATH=factory/src:factory/tests python3 -m unittest \
  test_planning_interface.PlanningInterfaceTests.test_cli_clarification_revision_restart_approval_and_export
```

The transcript contains fixture paths, run IDs and local operator identity. Treat
it as local evidence; the guide contains only a reduced example.

## Unverified boundaries

- A live model's interpretation and adequacy of proposed checks.
- Live Linear comments/agent-session chat: no adapter is implemented.
- A graphical chat or proposal-review UI: no frontend is implemented.
- Simulator, voice, physical device or cloud execution: these tests do not prove them.
- Hosted deployments remain untouched. Passing these tests does not publish or deploy these changes.
