# Hosted projection policy

Controls runtime-owned Linear status, evidence comments and agent activities,
Logfire traces, and hosted execution datasets. Configure local review copies
separately with [evidence policy](local-evidence-policy.md).

## Configure

Register adapters and credential environment references in `projections` first.
This policy selects existing registrations; it cannot add endpoints or tenants.

```json
{
  "projection_policy": {
    "defaults": {"destinations": ["linear", "logfire"], "redaction": "standard"},
    "constraints": {"max_delivery_age_seconds": 604800}
  }
}
```

Project defaults: `projects.<key>.projection_policy`. Run override:
`projection_policy` in the `--execution-config` JSON file. For a local-only run:

```json
{"projection_policy": {"destinations": []}}
```

| Field | Contract |
|---|---|
| `destinations` | Replacement list: `linear`, `logfire`, `dataset`; `[]` opts out |
| `redaction` | `standard`: existing structured adapter output plus secret-pattern scrubbing; `metadata`: omit free-form evidence |
| `redact_fields` | Extra field restrictions conservatively select metadata-only hosted output, because rendered prose/OTLP cannot preserve original field boundaries |
| `max_delivery_age_seconds` | Maximum source age at send, 1–315360000; `null` means unlimited. Not remote retention or deletion |

Omission inherits instance → project → run. Instance constraints can require
destinations, require metadata redaction, add redacted fields, and cap delivery age.
Explicit opt-out cannot override a required destination. Preview the resolved
policy, provenance and conflicts with `config-preview` or `doctor` before admission.

Defaults preserve currently enabled adapters. Run status/API includes the frozen
policy and destination hashes, never credential values or resolved endpoint details.
Credential rotation under the same configured reference remains allowed.
Linear binds its project/team, endpoint and token references; Logfire binds its
existing project/region/endpoint/references. Dataset name is also bound.
The existing Logfire project/region restriction is unchanged.

## Send and retry

- Freeze policy atomically with run admission, including direct kernel admissions.
- Current registration disablement, identity changes, or tighter instance constraints block incompatible sends.
- Current required destinations never enable a channel absent from a frozen run.
- Check queued data again before external calls. Never rewrite an ambiguous retry into different bytes.
- Linear metadata output is a local-run identifier and withheld-evidence message. Agent sessions fall back to that comment.
- Logfire metadata retains trace structure, opaque execution/record IDs, timestamps, status codes and completeness flags; names become generic.
- Dataset metadata omits inputs and free-form metadata; retains output status/state/digests, source time and execution/policy identity.
- Patterns are defense in depth, not a guarantee that arbitrary content is secret-free. Use opt-out for no egress.

Logfire skip receipts distinguish opted-out/expired new source from accepted
delivery. Skips advance the local processing watermark without claiming remote
success. A mixed pending batch is held if any included run loses permission;
restore the original approved registration or inspect locally. No automatic
retargeting or force-send command is provided.

`projection_health` exposes policy disposition and skipped/blocked counts separately
from confirmed receipts. Linear's queued rows remain available for inspection;
blocked rows do not prevent draining other runs in the same pass.

Native Linear planning requires standard Linear output without extra field
redaction. Incompatible admission fails early rather than hiding the approval
conversation. These egress switches do not disable tracker discovery or admission
reads; disable Linear/work-queue integration for an entirely offline instance.

## Migration and limits

Existing runs receive explicit `legacy-v0` snapshots of current enabled instance
registrations on first managed startup. New project/run defaults are not applied
retroactively. Old queued OTLP bytes must pass current redaction and destination
checks before retry. Inspect configuration before migrating an old ledger.
Migration is journaled once per project; a later missing snapshot fails closed
rather than silently becoming a new legacy approval on restart.

Hosted deletion/retention is unsupported and rejected as configuration. Delivery
age never erases provider data, canonical ledger facts, source, or local exports.
Listener-owned acknowledgements, independent SDK calls, public publication and
merge authority are outside this runtime policy. Library-only unmanaged ledgers
retain their prior contract; a managed ledger without its owning configuration
fails closed. Tests use fake providers: live provider delivery and the real web
acceptance run still require separate proof.
