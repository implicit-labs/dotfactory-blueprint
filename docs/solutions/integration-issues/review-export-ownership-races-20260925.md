---
module: evidence_bundle
symptom: "Validated export directories could be replaced before cleanup or publication"
root_cause: "Path checks and rename did not bind later effects to the checked directory"
solved_date: 2026-09-25
tags: [evidence, cleanup, ownership, redaction]
---

# Bind export effects to directory ownership

## Problem

Independent review found two races: cleanup could delete an identical-content
replacement directory after initial validation; rename could replace a concurrent
empty destination. Generic secret assignment matching also removed `Bearer` while
leaving the opaque authorization credential behind.

## Solution

- Journal directory device/inode identity and verify it again across cleanup boundaries.
- Open directories with `O_NOFOLLOW`; perform nested mutations relative to owned handles.
- Reserve final output with atomic `mkdir`, then use exclusive file creation rather than rename-overwrite.
- Refuse incomplete publication; recover only a complete, verified bundle.
- Scrub entire authorization/cookie header values and known secret JSON fields.

## Prevention

Keep `test_cleanup_refuses_replacement_root_after_validation`,
`test_publication_never_replaces_concurrent_empty_output`, and
`test_bearer_cookie_and_secret_fields_are_completely_scrubbed` in the evidence suite.
Hosted adapters need equivalent destination/receipt binding, not just string sanitization.

[ADR-0047](../../decisions/0047-freeze-local-review-evidence-policy.md)
