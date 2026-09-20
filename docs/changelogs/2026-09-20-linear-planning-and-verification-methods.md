# Linear planning and verification methods

- Admit Planning issues into opt-in native agent conversations. Batch contextual
  questions, revise one plan, and bind explicit `approve plan vN` replies to its
  exact plan, requirements and conversation digests.
- Recover authenticated replies through paginated reads, validate session and
  participant scope, persist durable receipts, deduplicate attention activities,
  and route pause, resume and stop through existing control boundaries.
- Keep planning reads bounded and fair. Stale results are re-fetched, and plan
  approval cannot approve later code-review checkpoints.
- Show a compact review with expandable plan detail while keeping questions,
  verification, overrides and approval instructions visible.
- Freeze project-owned verification methods at admission. Review run additions
  and omissions, host eligibility, commands, scenarios and required before/after
  artifacts before coding.
- Pin declared harness inputs to the approved plan. Reject workflows that cannot
  enforce the contract and reject missing, changed or tampered evidence.
- Add offline adapter, ledger, Git, delivery and compatibility regressions. Live
  app notifications, model question quality and browser/simulator behavior remain
  activation checks; this change does not provision environments or deploy a service.
- Name the default app credential `LINEAR_DOTFACTORY_AGENT_TOKEN`; explicit
  `agent_token_env` overrides remain supported.

[Planning guide](../LINEAR-PLANNING.md) ·
[Verification guide](../guides/verification-methods.md) ·
[ADR-0042](../decisions/0042-approve-verification-methods-and-evidence.md) ·
[ADR-0043](../decisions/0043-plan-through-linear-agent-conversations.md)
