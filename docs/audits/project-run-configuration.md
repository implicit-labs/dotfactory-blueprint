# Project and run configuration audit


## Decision

Default to project settings. Allow explicit run overrides to increase, reduce or
clear configurable requirements. Freeze effective settings and origin metadata at
admission; use an explicit reconfiguration contract for later changes. Keep host
identity, credentials and shared safety limits owned by the instance.

No arbitrary recursive merge of the whole factory config. Resolve each domain's
allowed fields, list semantics, authority and freeze timing explicitly.

## Implemented here

- `projects.<key>.execution.stages` overrides instance stage defaults.
- `run --execution-config` supplies explicit stage overrides; omitted fields
  inherit and supplied lists replace, including valid empty lists.
- Worker policy, route snapshot and field provenance are recorded atomically at
  run admission. Existing run overrides cannot be silently replaced.
- Operator/API run details expose effective stage settings and provenance.
- Two projects can use distinct requirements in one runtime. Tested reductions,
  additions, clearing, isolation, restart, rollback and untrusted issue prose.

## Scope matrix

| Domain | Current source and behavior | Required scope / identified gap |
|---|---|---|
| Prerequisites and placement | `execution.py`: shared worker registry; instance/project/run stage resolver added here | Implemented for workers, scope, requires, readiness, checks and check deadline. Reusable named capability profiles remain absent. |
| Runner/model/reasoning | `instance.py:resolve_runners`, `workflow.py:PROFILE_ATTRIBUTES`, `live_runner.py`: route defaults plus workflow-node model/reasoning overrides | Project defaults and run-stage selection of named runner profiles. Preserve explicit billing; validate availability before admission. Native config files can also affect behavior and are not content-frozen by the route snapshot. |
| Workflow and stage behavior | `instance.py:resolve_workflow`: project chooses a registered workflow; defaults/profile paths belong to that workflow; `kernel.py` freezes normalized graph | Preserve project selection. Add run selection of a registered workflow before admission, plus typed project/run defaults for runner, timeout, retry and skill fields. Do not treat graph edges or authority as ordinary overrides. |
| Verification contracts and actual host | `delivery.py:_verify` runs the frozen Python verifier on the coordinator using `sys.executable`, isolated HOME/PATH and no ambient credentials; `worker.py:checks` runs separate configured checks on the selected worker | P0: represent coordinator verification and worker verification separately; declare exact interpreter, OS, fixtures and credentials by verification lane. A worker readiness pass does not establish coordinator suitability. The first implementation phase adds an optional frozen coordinator contract, pre-allocation refusal and pre-delivery recheck using the actual isolated verifier environment. Planning-to-placement binding and additional verification lanes remain open. Current Python policy supports only 60/120-second plan-selected deadlines. |
| Planning → environment requirements | `verified-plan.md`, `delivery.py` freeze criteria and tests; execution policy is independent | P0: connect approved verification steps to capability requirements and a placement digest. Use explicit replanning/reconfiguration receipts; never mutate frozen approvals incidentally. A supplied run can reduce defaults today, but planning cannot yet propose accepted placement changes. |
| Skills, capabilities and MCP | `workflow.py` has skills/resources/capabilities per node/profile; `skills.py` resolves installed content; runner config owns directories, environment names and disabled MCP servers | Add project/run selection of trusted named tool/skill profiles, plus explicit list replacement. Verify actual activation in the chosen host. Keep installation roots, credential values and exposure permissions in the host registry. |
| Workspace and source revision | `instance.py:resolve_preparation` already merges project workspace root/remote/base_ref/retention; `resources.py` journals allocation | Add run-level base revision and retention before first allocation. Freeze workspace policy at admission; existing allocated handles remain authoritative. Repository identity stays project-owned; run cannot silently switch repositories. |
| Resource allocation/providers | Shared `preparation.providers` and capability catalog; workflow-node resource requests; worker placement currently rejects local resource handles | Project capability configuration and run resource requests need a consistent resolver. Keep host/provider registrations global. Add explicit live device/browser delegation; do not equate readiness probes with exclusive leases. |
| Deadlines, retries and recovery | Workflow node timeout/max_retries; instance preparation retry policy; route silence/grace/frame/event limits; worker lease expiry | Project/run task deadlines and retry policy should be typed and frozen. Keep protocol memory bounds, host lease limits and kill behavior as instance ceilings. An override cannot silently extend an active attempt's lease. |
| Scheduling, queue and budgets | `scheduler.py`, `work_queue.py`, `budgets.py`: explicit admission label, fresh eligibility snapshot, tracker-priority ordering, concurrency limits and recorded-token project/execution caps; cap values are instance-wide | Add project-specific budget defaults and run overrides within instance ceilings, plus explicit run priority. Preserve queue admission receipts and fail-closed handling of unknown usage; do not duplicate the existing admission design. |
| Linear and external projections | Tracker identity is per project; `resolve_linear_projection` and `resolve_logfire_projection` set enablement, endpoints, session mode and dataset destination globally | Project destination/profile selection and run opt-out/redaction mode. Keep auth/endpoint registrations at instance level; do not let issue text choose new egress destinations. Reconcile against frozen destination identity for each run. |
| Evidence/telemetry retention | `telemetry_delivery.py`, `datasets.py`, `linear_evidence.py`, `raw_stream.py`: receipts, projections and bounded raw-stream handling have separate policies | Project evidence/retention defaults and run overrides within deployment storage/privacy limits. Record where evidence is retained and exported; never expose credentials in resolved config views. |
| Credentials, permissions and host infrastructure | Environment references, worker billing/transport/root/SSH identity, instance ledger lock, control API principal and listener configuration | Keep instance/host-owned. Project/run may select approved profiles, not redefine credentials, API identity, control roles or listener deployment. Explicit reductions in user requirements do not override these enforcement boundaries. |
| Schema and explainability | Top-level/project unknown fields now fail. config-preview, doctor and worker-check share execution resolution with admission; domain validators beyond execution still vary | Extend strict validation and the side-effect-free execution preview to remaining domains. Preserve explicit not-checked status for unexecuted readiness probes; never present inspection as live readiness. |

## Resolution contract for follow-up domains

1. Registered instance defaults and ceilings.
2. Selected project defaults, optionally by workflow stage.
3. Explicit run settings, optionally by stage.
4. Named workflow/node defaults need a documented position per domain. Preserve
   current explicit node overrides during migration; reject ambiguous conflicts.
5. Validate the result against selected host profiles and workflow contract.
6. Persist values, source references/digests and field origins atomically at
   admission. Resolve secret values only at the host; never snapshot them.

The current execution resolver covers steps 1–3 for placement fields only. It does
not claim a unified schema across all domains.

## Work packages

| Priority | Deliverable | Acceptance |
|---|---|---|
| P0 | Verification host/capability contract | iOS, web and Python lanes each identify every check's execution host; unsuitable coordinator or worker is rejected before model work. Include a real simulator/browser pass and missing-prerequisite refusal. |
| P0 | Typed effective-config resolver and inspection | One project/run preview explains every supported field and origin; unknown keys fail; two projects do not inherit each other's settings. |
| P1 | Runner/tool profiles and workflow selection | Projects and runs select different model/tool profiles without duplicating workflows; credentials remain isolated; restart retains the selection. |
| P1 | Workspace/resource and lifecycle scope | Run source ref, retention, requested capabilities, timeouts and retries are explicit and frozen; allocated resources are not rebound on restart. |
| P1 | Scheduling/queue limits | Extend the existing queue; project-specific defaults and run budget/priority overrides honor instance ceilings and preserve deterministic admission receipts. |
| P1 | Projection and evidence profiles | Two projects route to different approved destinations; run opt-out prevents sends; credentials and private settings never appear in public projections. |

An advisory model may later suggest requirements. It is not needed to define or enforce these
contracts. No iOS, browser, voice or hosted-agent verification is claimed by this
audit.
