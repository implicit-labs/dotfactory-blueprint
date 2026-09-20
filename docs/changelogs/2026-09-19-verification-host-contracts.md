# Distinguish verification hosts and inspect scoped settings

- Add optional coordinator requirements to frozen execution stage settings.
  Enforce them before worker allocation/model launch and before delivery checks.
- Share the verifier's interpreter/environment boundary with host probes. Keep
  credentials unavailable; preserve old runs with absent host requirements.
- Add side-effect-free config-preview and project/run-aware doctor/worker-check.
  Share admission resolution, worker requirements and report/version validation.
- Reject unknown instance/project fields instead of ignoring misspelled settings.
- Cover isolation, refusal, override reduction, frozen restart and diagnostic parity
  with real local subprocess fixtures. No live browser/simulator/provider proof.
- Initial verification-host scope only: broader configuration domains and planning-to-placement
  binding remain open. No service deployment is included.

[ADR](../decisions/0040-distinguish-verification-hosts.md) ·
[Guide](../guides/verification-hosts.md)
