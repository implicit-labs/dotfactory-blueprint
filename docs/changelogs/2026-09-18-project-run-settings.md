# Scope execution requirements to projects and runs

- Resolve instance defaults, project fields and explicit run fields in order.
  Omission inherits; supplied lists replace, including valid empty lists.
- Add project execution stages and `run --execution-config`; worker registration,
  credentials, billing and workflow authority stay outside overrides.
- Freeze effective policy, routes and provenance atomically with admission.
  Preserve restart settings and reject changed overrides for existing runs.
- Reject direct dispatch if worker configuration is removed while frozen worker
  runs remain unfinished; preserve control-only inspection without dispatch.
- Expose stage settings and origins in operator/API run details.
- Audit remaining configuration domains and distinguish worker readiness from
  coordinator verification requirements.
- Include the worker-readiness candidate as an explicit dependency of this
  cumulative patch; its behavior and diagnostics have separate release notes.
- Add offline admission, replacement, restart and isolation regressions.
- Exercise real CLI and local worker subprocesses with independent project probes,
  stricter and reduced run requirements, custom workers/checks, failed-check
  routing and frozen replay. Verify local activity icons and human-review prompts.

[Decision](../decisions/0038-scope-execution-settings-to-projects-and-runs.md) ·
[Guide](../guides/project-run-configuration.md) ·
[Audit](../audits/project-run-configuration.md)
