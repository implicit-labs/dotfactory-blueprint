# Audited frozen-verifier replanning

- Add an approver-only, confirmed `replan` action from Blocked that records the
  exact failed source, replaced plan attempt, owner, and reason.
- Restrict replacement planning to plan and declared verification files, require
  exact-SHA review, and verify the existing implementation without rerunning it.
- Carry a safe frozen-verifier failure category and recovery guidance through the
  observation API while preserving the prior plan, failure, and investigation.
- Keep older workflow snapshots on the existing cancel-and-restart recovery path.

See [ADR-0039](../decisions/0039-replace-unusable-frozen-verifiers-through-reviewed-replanning.md)
and the [verified-delivery guide](../VERIFIED-DELIVERY.md).
