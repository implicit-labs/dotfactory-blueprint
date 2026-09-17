# ADR-0031: Freeze a bounded verification host policy

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-13 |
| Deciders | Project maintainers |
| Supersedes | — |

## Decision

Schema-version-1 verification plans may select only 60 or 120 seconds. Omission
retains 60 seconds. The selection must be nested under `verification_policy`;
unknown top-level or policy fields fail closed. One policy resolver supplies
planning guidance, exact schema shape, validation, execution, and receipt evidence.

Host verification runs `sys.executable -I` against a raw committed-source export
with `PATH=os.defpath`, a temporary `HOME`, no stdin, and no ambient credentials.

## Why

PATH-based interpreter discovery and an implicit 60-second deadline let local
planning disagree with the actual host.

## Consequences

- Good: approved checks bind the effective deadline before implementation.
- Good: planning and execution cannot drift through separate policy text.
- Cost: required suites longer than 120 seconds need a reviewed policy change or
  separate operator evidence.
- Not included: an OS security sandbox, in-place repair of frozen executions, or
  retrospective changes to saved plans and receipts.

## Alternatives

- Arbitrary positive deadlines: rejected because they silently weaken the host
  bound.
- PATH discovery: rejected because the host intentionally restricts `PATH`.
- Rewrite incompatible frozen plans: rejected because it destroys approval and
  failure provenance.

## Revisit when

- A required suite cannot be focused below 120 seconds without losing coverage.
- The host adds a stronger isolation boundary with a different executable contract.

## Evidence

- [Host policy tests](../../factory/tests/test_verification_host_policy.py)
- [Delivery contract](../VERIFIED-DELIVERY.md)
