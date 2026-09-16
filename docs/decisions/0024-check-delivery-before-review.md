# ADR-0024: Check delivery before Review

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-07 |
| Deciders | Project maintainers |
| Supersedes | — |
| Superseded by | [0027](0027-continue-validated-autoplanning.md) (planning approval policy only) |

## Decision

Enforce named completion contracts in the host and require their durable receipt
at the kernel transition. Start with an opt-in, small trusted Python delivery
preset. Export review artifacts through the host; merge stays a human action.

## Why

A runner's success label or evidence URI does not prove a usable change exists.
Planning turns issue requirements into proposed checks and manual procedures.
Human approval of the exact planning commit freezes them before implementation;
the host then checks committed source independently of the agent claim.

## Consequences

- Receipts bind source base/head, patch and evidence hashes, checks, and limits.
- Recovery can consume a saved receipt; changed source requires a new attempt.
- Unknown contracts fail startup; existing named contracts now require receipts.
- Approval is a durable human transition bound to the reviewed planning SHA.
- Every declared test/helper, the plan, and the verifier stay frozen during work.
- Planning validates structure without executing unapproved code; a human judges
  adequacy and test dependency coverage. Manual criteria remain manual.
- The approved verification script is a trusted policy input. Project code still has
  host OS permissions; this export is not a security sandbox.
- This lane excludes large repositories, dependencies, UI verification, direct
  agent tracker uploads, and automatic publication or merge.

## Alternatives

- **Provider-only success** — cannot independently prove deliverables or checks.
- **One universal verifier** — hides product-specific acceptance requirements.
- **A second execution engine** — duplicates existing workspace and kernel authority.

## Revisit when

- A supported project needs dependencies, more than 2000 files/4 MiB, or an OS
  sandbox. Add a versioned contract with explicit bounds and verification proof.

## Links


- [Operator instructions](../VERIFIED-DELIVERY.md)
- [Regression tests](../../factory/tests/test_verified_delivery.py)
