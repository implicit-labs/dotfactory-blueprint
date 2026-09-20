# Conversational planning requirements

- Accept scoped, idempotent planning messages through the local operator CLI and control API.
- Freeze planner context; commit a typed proposal with host mappings, questions, and visible requirement additions/removals.
- Require exact plan SHA and requirements digest approval; preserve original admission settings and atomically record approved amendments.
- Reject stale conversations, unanswered questions, unsupported check hosts and post-implementation edits. Conversational Autoplanning stops at PlanReview.
- Cover real Git/ledger delivery, restart/replay, project/role boundaries, transaction rollback and subsequent revisions with offline fixtures.
- Requires the verification-host contracts. Linear chat, graphical chat, live model/host proof and remaining configuration audit domains stay outside this change.

[ADR-0041](../decisions/0041-approve-conversational-requirements.md) · [Guide](../guides/planning-conversation.md)

## Interface validation

- Exercise actual CLI subprocesses over the owner socket, authenticated loopback HTTP, native prompt capture, clarification/revision, stopped-owner export and restart.
- Document the current terminal/API experience, complete revision feedback, exact approval commands, errors and unsupported UI/Linear surfaces.
- Add invalid-message/proposal cases; keep fixture-agent proof distinct from live model and device proof.
