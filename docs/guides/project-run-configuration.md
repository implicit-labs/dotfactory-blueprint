# Project defaults and run overrides

Set project requirements in `factory.json` under
`projects.<project>.execution.stages.<stage>`. The instance's `execution.workers`
remains the worker registry; `execution.stages` supplies compatibility defaults.

Resolution order: instance stage defaults → project stage fields → explicit run
stage fields. Each layer changes only fields it supplies.

| Input | Meaning |
|---|---|
| Omitted field or stage | Inherit |
| Scalar | Replace the inherited value |
| List | Replace the entire inherited list; do not append implicitly |
| `[]` | Explicitly clear that list, where valid |
| `null` | Invalid; never means inherit or clear |

Runs can have more or fewer requirements than the project default. To add one
requirement, list the existing requirements plus the new one. To remove one, list
only those still required. The resolved run view records the winning source of
each field as `instance`, `project`, or `run`.

## Define two projects

Add these `execution` objects to the existing project entries. Repository,
tracker and display settings remain required. Worker names must already exist
in the instance registry. Example commands must be replaced with project commands.

```json
{
  "projects": {
    "ios": {
      "execution": {
        "stages": {
          "Verifying": {
            "workers": ["mac"],
            "scope": "native",
            "requires": ["os:darwin", "tool:xcodebuild", "tool:xcrun"],
            "readiness": [],
            "checks": [["/opt/project/check-ios"]]
          }
        }
      }
    },
    "landing": {
      "execution": {
        "stages": {
          "Verifying": {
            "workers": ["render", "mac"],
            "scope": "portable",
            "requires": ["tool:node"],
            "readiness": [],
            "checks": [["/opt/project/check-web"]]
          }
        }
      }
    }
  }
}
```

The empty readiness lists above are placeholders, not complete iOS/web profiles.
Add assertions for the selected runtime, simulator, browser or fixture using the
[readiness guide](worker-readiness.md). Fields omitted in a project still inherit
instance defaults; explicitly replace inherited checks when changing workloads.

## Override a particular run

Save a UTF-8 JSON object, at most 65,536 characters, such as `run-execution.json`:

```json
{
  "stages": {
    "Verifying": {
      "workers": ["mac"],
      "requires": ["tool:node"],
      "readiness": []
    }
  }
}
```

```bash
PYTHONPATH=factory/src python3 -m dotfactory run \
  --config /absolute/path/factory.json --project landing --issue ISSUE-ID \
  --execution-config /absolute/path/run-execution.json --until-state Review
```

This run selects the Mac, replaces the project's declared requirements with Node,
and clears readiness probes. Its verification commands still inherit from the
project. Omit `--execution-config` to use all project defaults.

Allowed override fields: `workers`, `scope`, `requires`, `readiness`, `checks`,
`check_timeout_seconds`. Stage names must identify work nodes in the project's
workflow. Unknown fields, empty worker candidates, invalid probe limits and
unknown workers are rejected before admitting a run.

Overrides cannot redefine worker hosts, billing, credentials, workflow authority
or runner models. Git, native runner authentication/minimum version, requirements
implied by retained verification commands, and frozen delivery contracts remain
in force. Removing a prerequisite does not remove an approved acceptance check.

## Freeze and inspect

New runs store the resolved worker policy, runner routes, explicit overrides and
field provenance atomically with run creation. Neither later project edits nor a
restart changes those settings. Removing the instance worker configuration while an enabled project has unfinished
frozen worker runs blocks dispatch; control-only inspection stays available.
The operator/API run detail includes
`execution_settings` with effective stage rules, their origins, digest and freeze
point; it excludes worker connection records and runner credential configuration.

Repeating a run command without overrides resumes its stored settings. Resupplying
the identical override is idempotent; a changed override is rejected. An active
run cannot be reconfigured through this input. Finish or cancel it before starting
a new execution, or use a separate issue for an independent run.

Legacy runs with a recorded policy keep it. Legacy runs without one snapshot at
first placement and report `legacy-first-placement`; they cannot accept a new run
override. Queue discovery through `work` inherits project defaults. Issue prose,
comments and agent output cannot supply execution overrides.

Settings for worker placement are frozen at admission. Planning chat may propose
an explicit amendment for configurable implementation and verification fields.
Exact plan SHA and requirements-digest approval records that amendment atomically
while preserving the original admission snapshot. Unapproved prose, unanswered
questions and stale revisions never change placement policy. See the
[planning conversation guide](planning-conversation.md) and
[configuration audit](../audits/project-run-configuration.md).
