# ADR-0032: Preserve agent activity order and identity

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Deciders | Project maintainers |
| Source | Public release |
| Supersedes | —; extends 0022 |

## Decision

Native session publication uses a dedicated app-actor credential. Missing app
credentials retain classic comment fallback. Activities retain staging order
across equal/backward clocks, retries and restart; later activities wait for the
first unconfirmed activity. Unknown session writes reconcile before new desired
links replace the frozen request.

## Why

An ordinary API key is not an app actor. Out-of-order activities can overwrite a
terminal session status, and changing an ambiguous create request can duplicate
sessions.

## Consequences

- Logical activity staging timestamps increase monotonically within an execution;
  they describe ordering, not measured event timing.
- A delayed activity delays subsequent activities, including the terminal response.
- The first terminal response is frozen per terminal state run; later evidence may
  update session links without reposting or invalidating that response.
- Canary success requires remote session/activity readback and restart replay;
  hosted webhook receipt and image validation remain separate evidence.

## Alternatives

- Shared token — obscures the actor boundary and cannot retain independent fallback.
- UUID tie-breaking or skipping retry-delayed activities — can reorder user-visible state.
- Restaging ambiguous requests — loses the identity needed to reconcile safely.

## Revisit when

The provider supplies ordered idempotent batches or removes the app-actor requirement.

## Evidence

- [Changelog](../changelogs/2026-09-17-linear-agent-sessions.md)
- [Canary procedure](../../factory/LINEAR_AGENT_SESSIONS.md)
- Tests: `factory/tests/test_linear_agent.py`
