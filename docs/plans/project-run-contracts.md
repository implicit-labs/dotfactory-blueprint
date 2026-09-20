# Project/run configuration contracts

## Phases

1. Separate coordinator verification requirements from worker readiness; use the actual verifier interpreter and isolated environment. Gate dispatch before workspace allocation and model launch.
2. Share a typed, side-effect-free execution resolver across admission, preview, doctor and worker-check. Explain inheritance and lanes; reject unsupported fields.
3. Add registered runner/tool/workflow profile selection with frozen provenance.
4. Freeze workspace/resource/lifecycle overrides without rebinding allocated handles.
5. Extend project/run budgets and priority within shared instance ceilings.
6. Scope approved projection/evidence profiles and retention.

## First implementation boundary

Execution policy gains an optional coordinator contract. Instance/project/run overlays replace that contract explicitly; omitted values inherit. The coordinator lane uses the delivery verifier's interpreter, minimal PATH and temporary HOME. Credentials are unavailable by design. Worker checks retain worker environment semantics. Host checks run before any worker allocation, including before planning, and again before later attempts; restart uses frozen requirements.

Preview loads configuration and workflow files only. It must not open the ledger, allocate workspaces, execute probes, contact workers or resolve credentials. Explicit worker-check is the side-effecting probe surface. Doctor stays read-only and labels probes not checked.

## Risks and verification

- False host equivalence: share the verifier environment constructor; test PATH, HOME and secret isolation.
- Configuration drift: reuse one resolver for admission and preview; persist the selected contract in existing atomic snapshots; test restart.
- False readiness: fail closed on omitted worker probe results; label read-only inspection separately from executed checks.
- Scope leakage: two project fixtures with stricter and reduced overrides; invalid fields fail before state mutation.
- Existing runs: absent coordinator contracts retain legacy behavior. No retroactive requirements or approval changes.

No service resumes. Live browser/simulator/voice proof remains a distinct gate; fixtures cannot satisfy it. Later phases remain open until implemented and independently verified.
