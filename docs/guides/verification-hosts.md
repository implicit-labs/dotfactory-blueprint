# Configure verification hosts

Stage `readiness` and `checks` execute on the selected worker. The pinned Python
delivery verifier executes on the coordinator. Declare their requirements
separately; a worker pass cannot establish coordinator suitability.

```json
{
  "projects": {
    "example": {
      "execution": {
        "stages": {
          "Verifying": {
            "coordinator": {
              "requires": ["os:darwin"],
              "python_min_version": "3.12",
              "readiness": [
                {
                  "name": "python-library",
                  "command": ["{python}", "-I", "-c", "import sqlite3"],
                  "timeout_seconds": 5
                }
              ]
            }
          }
        }
      }
    }
  }
}
```

Merge this fragment into a configured project. `coordinator` is one replaceable
stage field: omission inherits; an explicit object replaces the whole contract;
`{}` clears configurable coordinator requirements. This never changes pinned
verification, isolation or workflow approval requirements. Requirements are frozen
at admission and are checked before every worker attempt, before allocation or
model work. Delivery checks its own stage contract again before running the verifier.

`{python}` expands to the coordinator's actual interpreter. Probes and delivery
share the minimal system PATH, temporary HOME and absence of ambient credentials.
Probes run from `/`, before the delivery checkout exists: use absolute paths for
host fixtures. These checks establish host capabilities, not repository test
results. No credentials or environment overrides are supported for this lane.

## Inspect

```bash
python3 -m dotfactory config-preview --config instance.json --project example \
  --execution-config run.json
python3 -m dotfactory doctor --config instance.json --project example \
  --execution-config run.json --json
python3 -m dotfactory worker-check --config instance.json --project example \
  --stage Verifying --worker mac --execution-config run.json
```

Preview only reads configuration/workflow files; it never opens a ledger or
executes probes. Doctor checks local host facts and labels worker/probe checks
`not_checked`. With worker execution configured, doctor does not require native
runner executables or authentication on the coordinator. Worker-check explicitly executes trusted operator probes and native
login/version checks using the same resolver and report validation as dispatch.
Omit `--execution-config` to use project defaults. Preview describes new admission,
not an existing run: inspect that run's frozen `execution_settings` for its policy.

The shared resolver currently covers execution fields. Runner/tool profiles,
workflow selection, workspace policy, budgets, projections and evidence retention
remain separate follow-up phases. Planning may propose changes to configurable
implementation and verification fields. They apply only after exact plan and
requirements approval; credentials, host registrations, billing identity, safety
ceilings, pinned checks and the original admission record remain unchanged.
Readiness does not reserve devices.
