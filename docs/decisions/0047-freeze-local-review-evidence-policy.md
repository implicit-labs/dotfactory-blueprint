# ADR-0047: Freeze local review evidence policy before export

- Status: Proposed
- Date: 2026-09-25

## Decision

Freeze local review destination identity, export mode, field redaction and copy
retention in the run admission transaction. Apply instance constraints before
filesystem effects; record export ownership and content hashes before publication.

This first lane is `local_review`. It grants no authority over Linear, Logfire,
datasets, public projection, source merge, or canonical evidence deletion.

## Why

Mutable export defaults can redirect an old run or expose more evidence than its
operator selected. Cleanup cannot safely infer ownership from directory names.

## Consequences

- Registered roots belong to the instance; snapshots contain references/digests, no credentials.
- Runs can select only allowed destinations and cannot weaken mandatory constraints.
- Full bundles retain patch identity; suspicious source bytes refuse export instead of being rewritten.
- Summary mode excludes source and free text rather than pretending pattern matching guarantees privacy.
- Expiry cleanup deletes only manifest-matching exported copies, after durable intent; unknown data needs attention.
- Version-1 policy snapshots and explicit legacy-v0 migration preserve restart/replay behavior.
- Independent domain tables do not change the canonical ledger schema number.
- Hosted routing/retention and the real web acceptance proof remain separate unfinished work.

## Alternatives

- Read current defaults at export time: rejected; prior runs would change behavior silently.
- Recursively erase named output directories: rejected; names are not ownership evidence.
- One policy for all external destinations immediately: deferred; each adapter has independent retry and authority contracts.

## Revisit when

A hosted adapter can bind destination identity, policy, retries and receipts without
changing prior runs or granting new egress authority. Extend the versioned contract
and acceptance tests per adapter, not by treating local proof as hosted proof.

## Evidence

- [Changelog](../changelogs/2026-09-25-local-review-evidence.md)
- [Operator guide](../guides/local-evidence-policy.md)
- `factory/tests/test_evidence_policy.py`
