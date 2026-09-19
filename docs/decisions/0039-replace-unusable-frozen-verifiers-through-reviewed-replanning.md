# ADR-0039: Replace unusable frozen verifiers through reviewed replanning

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-19 |
| Deciders | Project maintainers |
| Supersedes | [ADR-0031](0031-freeze-bounded-verification-host-policy.md) (replanning policy only) |

## Decision

Keep an approved verification plan immutable during implementation,
verification, and investigation. After a frozen verifier fails and investigation
reaches Blocked, permit only an approver-confirmed `replan` command with a durable
reason and the exact failed source revision.

Replacement planning may change only plan and declared verification files. It
creates a new plan receipt and stops at ReplanReview. Exact-SHA human approval
then enters Verifying against the existing implementation. The old plan,
failure, diagnosis, and approval remain in the ledger.

## Why

Syntax validation does not prove that a verifier accepts the host invocation.
Canceling and recreating the execution preserves safety but loses a direct
in-execution recovery path. Letting an agent edit frozen checks would erase the
authority boundary the plan established.

## Consequences

- Good: an unusable harness can be replaced without rerunning implementation.
- Good: approval, source, reason, replacement lineage, and prior failure remain auditable.
- Cost: an approver must diagnose the harness and review a second exact commit.
- Cost: saved executions with an older workflow snapshot still require cancel and restart.
- Not included: silent repair, automatic replanning, or executing proposed
  acceptance tests during planning.

## Alternatives

- **Let investigation edit the verifier** — rejected because it mutates approved policy.
- **Always create a new execution** — safe, but needlessly discards direct recovery lineage.
- **Run proposed tests during planning** — rejected because expected
  pre-implementation assertion failures do not distinguish a broken harness.
- **Treat `ast.parse` as runnable proof** — rejected because syntax is not invocation behavior.

## Revisit when

- A deterministic, side-effect-free verifier handshake can prove harness
  compatibility without executing acceptance assertions.

## Evidence

- [Verified-delivery tests](../../factory/tests/test_verified_delivery.py)
- [Delivery contract](../VERIFIED-DELIVERY.md)
