# ADR-0045: Freeze registered workflow and runner selections at admission

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-09-21 |
| Deciders | Maintainers |
| Supersedes | — |

## Decision

Instance configuration registers workflows, runners and named execution
profiles. Projects set defaults; explicit run input may select registered
workflows and profiles globally or per work stage. Resolution order is
workflow/DOT → project → run. A supplied list replaces; `[]` clears.

Resolve and validate before run creation. Freeze the selected graph, effective
stage settings, runner routes, placement policy and provenance atomically in
the ledger. Restart and replay use that snapshot; changed explicit selections
cannot alter an existing run.

## Why

Projects need different models and tools without duplicating workflow graphs.
Unrestricted per-issue commands or paths would bypass host and credential
boundaries; mutable defaults would change queued runs after admission.

## Consequences

- Only registered names are selectable; unknown stages, runners, capabilities
  and resources fail before admission.
- Workflow edges, authority, credentials, worker billing and host registration
  remain outside project/run selection.
- Read-only preview and admission use one resolver. Selected profile values do
  not prove installed tools or reserve shared resources.
- Existing work without a registered runner can still use an injected fixture.

## Alternatives

- Copy a workflow per model: duplicates graph authority and drifts.
- Accept arbitrary run JSON for commands and paths: bypasses the instance ceiling.
- Resolve on each dispatch: changes behavior after approval or restart.

## Revisit when

Runner adapters can attest model availability and native configuration content
before admission, or a reviewed reconfiguration contract can change a frozen
selection safely.

## Evidence

- `factory/tests/test_selection.py` exercises precedence, freezing, restart,
  real fixture dispatch, and selected-graph Linear bindings.
