# ADR-0027: Continue validated Autoplanning

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-13 |
| Deciders | Project maintainers |
| Supersedes | 0024 (planning approval policy only) |

## Decision

Autoplanning continues through Ready after host validation without human plan
approval. Manual Planning retains PlanReview and exact-commit human approval.

## Why

Choosing automatic planning delegates the planning decision, not just drafting.

## Consequences

- The accepted agent transition must reference the validated planning attempt.
- Both paths pin the exact source commit and declared check hashes across restart.
- Invalid plans, modified checks, and failed verification still block delivery.
- Final Review and merge authority remain human-owned.
- Existing executions retain their saved graphs; no retrospective authorization
  or implicit migration of paused runs. New runs use the updated graph.
- Structural validation does not establish test adequacy or execute proposed tests.

## Alternatives

- Synthetic human approval: misrepresents decision provenance.
- Remove all approval guards: weakens manual planning and frozen-check enforcement.

## Revisit when

Automatic planning needs an independent quality gate beyond structural validation.

## Evidence

- [Regression tests](../../factory/tests/test_verified_delivery.py)
