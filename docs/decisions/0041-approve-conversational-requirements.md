# ADR-0041: Approve conversational requirements as plan amendments

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-19 |
| Deciders | Maintainers |

## Decision

Treat chat as untrusted planning input. Bind a typed requirements artifact to the
captured conversation and effective configuration. Human approval binds its
exact digest and Git commit; record the settings amendment atomically with the
PlanReview-to-Ready transition. Preserve the original admission snapshot.

Planning itself runs under admitted requirements. Amend only implementation and
verification stages. Freeze each attempt's chat context; newer messages and
unanswered questions require revision. Autoplanning proposals require PlanReview.

## Why

Natural-language input cannot silently grant execution authority. Approval must
survive restart and replay without changing another project or an active attempt.

## Consequences

- This explicit approved amendment extends admission freezing in ADR-0040.
- Readiness commands proposed in a plan become trusted only through exact human
  approval. They execute later under existing worker/coordinator probe boundaries.
- Preview exposes removals as well as additions; project defaults remain intact.
- Automated pinned checks still execute on the coordinator. Host declarations
  cannot pretend a worker executes a verifier it does not support.
- Unsupported legacy workflow paths fail closed rather than bypassing review.
- Live chat adapters and additional verification executors remain separate work.

## Alternatives

Immediate chat-driven mutation would bypass review. Requiring a new run for
every planning clarification would discard useful planning history.

## Revisit when

A reviewed executor supports additional verification hosts or mid-run revision.

## Links

- [User journey and commands](../guides/planning-conversation.md)
- [Verification host boundary](0040-distinguish-verification-hosts.md)
- [Implementation PR](https://github.com/implicit-labs/dotfactory-blueprint/pull/22)
- [Changelog](../changelogs/2026-09-19-planning-conversation.md)
