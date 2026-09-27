# Project defaults and run overrides

Local review export has a separate [evidence policy](local-evidence-policy.md).
Its defaults, run overrides, redaction, retention and destination constraints do
not change worker placement or hosted Linear/Logfire projections.

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

Placement overrides cannot redefine worker hosts, billing, credentials, or
workflow authority. Registered selection profiles can choose a runner/model;
see below. Git, native runner authentication/minimum version, requirements
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

## Select a workflow and runner profile

Register named profiles at the instance ceiling. Project and run inputs may
select them, but cannot supply an arbitrary runner command, model, credential,
skill directory or workflow path.

```json
{
  "selection_profiles": {
    "sol-medium": {"runner": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium"},
    "browser-check": {"runner": "codex", "skills": ["browser-qa"], "capabilities": ["browser"]},
    "no-extra-skills": {"skills": []}
  },
  "projects": {
    "landing": {
      "workflow": "default",
      "profile": "sol-medium",
      "stage_profiles": {"Verifying": "browser-check"}
    }
  }
}
```

The example is a fragment: keep the required project, runner and workflow
fields from the base configuration. The browser capability must be declared on
the registered runner, and any named resource must be registered for the
project. Profile fields are `runner`, `model`, `reasoning_effort`, `skills`,
`capabilities`, `resources`, `timeout`, and `max_retries`.

Resolution order for each work stage: workflow/DOT fields → project profile →
project stage profile → run profile → run stage profile. Omitted fields inherit;
each supplied list replaces the entire list, and `[]` clears it. An explicit
`null` for a run `profile` or stage selection clears inherited named profiles
for that scope and returns to workflow/runner defaults. `null` is not valid
inside a profile's fields. The registered runner's default model and reasoning
effort fill any remaining gaps; Codex defaults to Sol/medium.

To select a registered alternate workflow and a stage profile for one run:

```json
{
  "workflow": "alternate",
  "profile": "sol-medium",
  "stage_profiles": {"Verifying": "no-extra-skills"},
  "stages": {"Verifying": {"workers": ["mac"]}}
}
```

`workflow` must name an entry in `workflows`; it cannot change edges or human
authority. `stage_profiles` accepts work stages only. Save this as a JSON file
and pass it to `run --execution-config`; `config-preview` and `doctor` accept
the same file for read-only inspection. Preview reports the selected workflow,
per-stage effective values, provenance and overridden-field conflicts without
probing hosts. Admission
freezes the resolved graph, route/placement policy and selection in one ledger
transaction. Reusing a running issue with changed explicit selection fails;
restarts use the frozen snapshot, not new project/profile definitions.

Profiles describe declared requirements. They do not acquire simulators or
browsers, grant credentials, or replace `prepare_attempt()` and lease checks.
