Read the repository instructions and work only on the current workflow state.

- `Autoplanning` or `Planning`: inspect the issue and repository, then produce a concrete plan. Do not implement.
- `Implementing` or `Reworking`: implement the requested change and verify the relevant checks.
- `Verifying`: verify the implementation and report evidence; do not broaden the change.
- `Investigating`: diagnose the recorded failure and make only the recovery change the issue requires.

Use the immutable execution context below as the task identity. Preserve unrelated work and return evidence URIs for claims you verified.
