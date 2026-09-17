---
module: factory doctor
symptom: "Polling-only Linear configuration fails doctor with a missing webhook secret"
root_cause: "A configured environment-variable name was treated as a required runtime credential"
solved_date: 2026-09-13
tags: [doctor, credentials, verification]
---

# Match diagnostic requirements to the runtime

## Problem

The factory-generated doctor passed its planned tests but rejected valid Linear
polling configuration without `LINEAR_WEBHOOK_SECRET`. The runtime resolver only
requires the token. Independent text/JSON probes exposed the mismatch.

## Solution

Only require the polling token. Mark webhook readiness not_checked and direct
the operator to validate webhook setup separately. The regression compares a
token-only environment with `FactoryConfig.resolve_linear_projection`, then
asserts doctor passes in both output modes and still rejects a missing token.

## Prevention

- A named configuration input is not necessarily required for the active capability.
- Derive negative cases from the runtime's requirements, not the diagnostic's implementation.
- Keep independent black-box checks after generated verification; passing self-authored
  acceptance tests does not establish complete requirements coverage.
- Preserve the original failed proposal and receipt when promoting generated source.

## Evidence

- [Doctor regression](../../../factory/tests/test_doctor.py)
- [Process lifecycle coverage](../../../factory/tests/test_process_lifecycle.py)
