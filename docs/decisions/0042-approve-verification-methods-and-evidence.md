# ADR-0042: Approve verification methods and evidence

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-20 |
| Deciders | Maintainers |
| Supersedes | — |

## Decision

Freeze project-owned verification methods at admission. Planning selects methods
from defaults, path/category rules and explicit run amendments. Human approval
binds the selection to the exact plan commit and requirements digest.

A method declares eligible hosts, readiness, commands, scenarios and required
artifacts. Host execution captures baseline evidence before coding and result
evidence before completion. Record source SHA, host, command result and artifact
hashes durably. Freeze declared repository harness inputs at plan approval.
Recheck actual changed paths, harness manifests/hashes and saved artifact bytes.

## Why

Unit tests cannot substitute for simulator/browser behavior and visual evidence.
Coding and verification may require different machines. Model-authored success
claims are not host execution receipts.

## Consequences

- Reuse worker transport and existing exact-plan approval; no model login is
  needed for verification commands.
- Registered commands are trusted code. Source checkouts isolate revisions,
  not malicious processes or shared simulator state.
- Interrupted starts are uncertain; do not replay automatically.
- Existing projects remain unchanged until they register methods.
- No fixture provisioning, shared-device reservation or visual judging is
  implied. Verifier-only replanning cannot replace method contracts yet.

## Alternatives

- **Only add unit checks to planning** — misses host and evidence requirements.
- **Let chat execute arbitrary commands** — bypasses project ownership and review.
- **Accept model-provided screenshots as proof** — lacks execution provenance.

## Revisit when

- Concurrent runs need atomic simulator/device leases.
- Larger recordings need external object storage or resumable transfer.
- Audited replanning can bind replacement methods and preserve baseline evidence.

## Links

- [User journey and configuration](../guides/verification-methods.md)
- [Changelog](../changelogs/2026-09-20-linear-planning-and-verification-methods.md)
- Evidence: `factory/tests/test_verification_contract.py`
