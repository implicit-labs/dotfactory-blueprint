# ADR-0043: Plan through Linear agent conversations

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-20 |
| Deciders | Maintainers |
| Supersedes | — |

Builds on [ADR-0041](0041-approve-conversational-requirements.md) and
[ADR-0042](0042-approve-verification-methods-and-evidence.md).

## Decision

Use native Linear Agent Sessions as an opt-in planning UI. Keep the local ledger,
control service and exact-plan gate authoritative.

- A project explicitly enables Planning admission and supplies participant roles,
  app/workspace identity and reviewer profile URLs.
- The `linear-planning.dot` workflow adopts Planning into a pickup checkpoint;
  all plans stop at PlanReview. Existing workflows keep their admission rules.
- Read the bound session's activities through the authenticated Linear API.
  Webhook receipt arrival is neither dispatch authority nor approval. Complete
  paginated reads recover prompts after missed deliveries and restarts without
  sharing the receiver database or putting a second writer in the listener.
  One background planning read runs at a time; completed reads yield with a
  five-second cooldown. Results older than ten seconds are re-fetched before use.
- Verify workspace, app, issue, project, team, session and participant. Freeze
  admission roles; current configuration may revoke, but not grant, run authority.
- Save the readable plan from the validated delivery receipt. A displayed revision
  binds its plan SHA, requirements digest and conversation digest. Accept only an
  authorized, explicit `approve plan vN` sent after confirmed publication.
- Send contextual question batches and review elicitations. Keep stage/tool progress
  in the run trace. Reuse the durable activity outbox for retries and deduplication.
- Pause gates the next dispatch; stop uses cancellation. Neither proves that an
  already-running remote process has exited.

## Consequences

An awake owner polls Linear and runs eligible work. An offline owner cannot send
new status or notifications; Linear retains replies until it returns. Initial
mentions creating unrelated sessions are not implicitly attached. Users reply in
the run's bound native session; this avoids routing private replies to another run.

The receiver remains receipt-only (0030). No Render activation is required for
this polling implementation. Notification delivery still depends on Linear and
user preferences; offline adapter tests are not Inbox or push delivery proof.

## Alternatives

- Dispatch directly from webhook bodies: rejected; duplicates, replay and a
  separate receiver process must not bypass control authorization.
- Interpret natural-language agreement as approval: rejected; ordinary answers
  must remain safe planning input.
- Create a second comment-based conversation: rejected; competing threads make
  review and reply ownership ambiguous.

## Revisit when

Measured polling latency or API volume requires webhook wakeups, or native-session
mention routing can prove an unambiguous binding without introducing a second run.

## Evidence

- `factory/tests/test_linear_planning.py`: real Git/ledger/control round trips with
  a fake Linear transport; scope, revision, duplicate, pagination and crash cases.
- [User journey and activation](../LINEAR-PLANNING.md).
- [Changelog](../changelogs/2026-09-20-linear-planning-and-verification-methods.md).
