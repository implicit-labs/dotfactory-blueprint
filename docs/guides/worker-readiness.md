# Check a worker before launching an agent

Define project defaults and explicit run overrides using the
[project/run configuration guide](project-run-configuration.md). Add `readiness`
to each applicable stage rule. Existing `requires`
checks OS, architecture and tool presence; readiness probes assert versions,
installed runtimes, fixtures or device availability on the selected host.

```json
{
  "workers": ["mac"],
  "scope": "native",
  "requires": ["os:darwin", "tool:xcrun"],
  "readiness": [
    {
      "name": "python-version",
      "command": ["/usr/bin/python3", "-c", "import sys; raise SystemExit(sys.version_info < (3, 9))"],
      "timeout_seconds": 5
    },
    {
      "name": "available-ios-simulator",
      "command": ["/usr/bin/python3", "-c", "import json,subprocess; d=json.loads(subprocess.check_output(['/usr/bin/xcrun','simctl','list','devices','available','--json'])); assert any(v.get('isAvailable') for k,vs in d['devices'].items() if '.iOS-' in k for v in vs)"],
      "timeout_seconds": 10
    }
  ],
  "checks": []
}
```

Use a project-specific assertion for the required simulator version, device,
voice fixture or permission. Listing devices successfully does not prove that
a suitable device exists. An available simulator does not prove app behavior,
microphone access or physical-device verification.

- Commands are trusted operator configuration, frozen with placement policy for
  the execution. They are not generated from issue text or selected by a model.
- Commands run before workspace allocation, from `/`, without shell expansion,
  under the worker's configured environment and billing credential isolation.
  Use absolute fixture paths; never embed credentials in command arguments.
- Exit zero means ready. Missing commands, nonzero exits, timeouts or missing
  worker reports reject that candidate. If all candidates fail, work requests
  attention without launching a coding agent.
- At most eight uniquely named probes; each allows 1–30 seconds, default 10;
  combined budget at most 60 seconds. Their process groups are killed on timeout
  and after parent exit. Probes must be read-only and must not daemonize.
- Reports retain names, outcomes and exit codes, not command output. Successful
  reports live in the selected worker handoff manifest; failures name the probe
  in the attention request. Update remote `worker.py` before using readiness.
- Probes observe capability at dispatch time; they do not reserve devices or
  prevent capability changes. `checks` still verify delivery after work.
  Model selection and atomic resource bundle allocation are separate work.

Linear shows human workflow nodes as awaiting input. Use the configured workflow
approval controls; responding to the projected activity does not approve a plan
or merge. The exact known Codex skill-budget notice remains a warning in the
local trace; genuine error and failed-turn frames remain errors.
