# Local review evidence policy

Controls `dotfactory delivery` review bundles only. It does **not** change Linear,
Logfire, dataset exports, canonical artifact retention, public publication, or merge authority.

Use [hosted projection policy](hosted-projection-policy.md) for runtime-owned external sends.

## Configure

Instance configuration:

```json
{
  "evidence_policy": {
    "destinations": {
      "local-reviews": {"kind": "local_review", "enabled": true, "root": "/absolute/reviews"}
    },
    "defaults": {"destination": "local-reviews", "mode": "summary", "retention_seconds": 604800},
    "constraints": {
      "allowed_destinations": ["local-reviews"],
      "allow_full": false,
      "max_retention_seconds": 604800,
      "redact_fields": ["intent", "context"]
    }
  }
}
```

Project defaults: `projects.<key>.evidence_policy`. Run overrides: an
`evidence_policy` object in the existing `--execution-config` JSON file.
Destinations are registered only at the instance; credentials are not accepted.
An optional destination `projects` list restricts its use to named projects;
omission allows all configured projects. Use it for separate project-owned roots.

| Field | Meaning |
|---|---|
| `destination` | Registered local root; built-in `operator-local` permits an explicit operator-selected local path |
| `mode` | `disabled`: no export; `summary`: IDs/hashes only; `full`: checked patch, review JSON, checks, logs and artifacts |
| `retention_seconds` | Exported copy lifetime from publication; `null` means no automatic expiry |
| `redact_fields` | JSON field names recursively replaced in `review.json`; does not rewrite source files or binary artifacts |

Omission inherits instance → project → run. Explicit `null` clears retention
unless capped. A supplied redaction list replaces defaults; mandatory instance
redactions are always added back. `allow_full: false` caps full exports to summary.
`require_export: true` rejects opt-out; it does not automatically create an export.

Use `config-preview --config CONFIG --project PROJECT` or `doctor` before admission.
Run status/API exposes the frozen policy, provenance, conflicts and destination
digest—not the destination root or credentials. No destination probe is implied.

## Export and inspect

```sh
python3 -m dotfactory delivery --config CONFIG --project PROJECT --execution RUN --output /absolute/reviews/review-1
python3 -m dotfactory evidence-cleanup --config CONFIG --project PROJECT
python3 -m dotfactory evidence-cleanup --config CONFIG --project PROJECT --apply
```

The runtime must be stopped before these owning CLI commands open its ledger.
Export requires checked planning at PlanReview/ReplanReview or checked delivery
at Review. An export is not plan approval, publication permission, or merge permission.

- `evidence-manifest.json` hashes every exported file and records the policy digest and expiry.
- Repeating the same output verifies its ownership/content and returns the original receipt.
- Destination removal, disablement, root changes, or tighter current constraints fail closed.
- Summary bundles contain no intent, titles, context, free text, patches, logs, checks or images.
- Full bundles remain private. Known secret patterns in source/checks/logs/artifacts refuse export;
  matching review JSON strings are replaced in full, not partially scrubbed. This
  heuristic cannot certify arbitrary or encoded content as secret-free.
- A summary digest identifies private source; do not treat it as an approved public projection.

## Expiry and cleanup

Expiry blocks replay of an old export. Cleanup is explicit; dry-run is the default.
`--apply` permanently deletes only expired copies whose file hashes and complete
inventory still match their journaled manifest. There is no trash recovery.
Canonical ledger facts, source/workspaces, and canonical verification artifacts remain intact.
Fresh exports may be generated from retained canonical evidence.

Unknown, modified, extra, or symlinked content produces `needs_attention` and a
nonzero CLI exit. Inspect that exact directory manually; cleanup never guesses ownership.
Deletion intent precedes mutation. A crash during deletion resumes only matching remaining
files. A crash after publication recovers the receipt by verifying the final bundle.
A crash before publication leaves a visible incomplete receipt; choose a new output.
Final-directory reservation and file creation never overwrite existing paths.
A partially copied bundle is incomplete evidence, not a successful export; inspect
it manually instead of adopting its contents as proof. Cleanup also checks recorded
directory identity, not just matching bytes.

## Restart and migration

Policies freeze atomically at admission, including internal kernel admissions.
Changing defaults cannot rewrite an existing run; changing an explicit override
requires a new run. Current instance constraints may tighten new exports but
cannot weaken frozen restrictions or silently redirect a destination.

Old runs migrate as `legacy-v0`: full, operator-local, unlimited retention, no
additional field redaction. They do not inherit new project defaults. Current
instance constraints still apply. Existing unjournaled export directories are
never adopted or deleted. Hosted projections are unchanged by this migration.
Migration is journaled once per project. Later missing snapshots fail closed;
restart cannot reinterpret missing authority as an older permissive policy.
Opening a ledger directly without its owning configuration cannot export copies.
