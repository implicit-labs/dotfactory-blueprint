# Planned and verified Python delivery

Use `verified-python` for one Codex worker on a small trusted stdlib Python
repository. Planning defines the checks; no pre-existing `verify.py` is required.

## Flow

1. **Planning:** commit `.factory/plan.md`, `.factory/verification.json`,
   `.factory/verify.py`, and declared acceptance/regression test files.
2. **Autoplanning:** host validation accepts the exact planning commit and
   continues automatically to Ready. **Manual Planning** instead stops at
   PlanReview for exact-commit human approval. Production code cannot change
   during either planning path; proposed tests are not executed at this stage.
3. **Ready:** the accepted handoff or human approval freezes the plan, definition,
   verifier, and every declared test/helper file before implementation.
4. **Implementation and verification:** implement the issue; run the approved
   verifier against an isolated export of the exact committed source. Changed
   approved files or failed checks prevent Review.
5. **Review:** export the patch, requirement/feedback context, check results,
   approved checks, and limits. Request code revisions or authorize merge yourself.

## Initialize a local instance

For HTTP status and control after the first run, see
[Run the local HTTP gateway](../factory/CONTROL_API.md#run-the-local-http-gateway).
`init` remains a CLI setup command; `serve` hosts the existing API independently
of whether a worker is running.

From the dotfactory checkout, with a supported Python/SQLite runtime:

```bash
export PYTHONPATH="$PWD/factory/src"
python3 -m dotfactory init \
  --repository /absolute/path/to/python-project \
  --output /absolute/path/to/new-factory-instance \
  --project my-project --linear-project LINEAR_PROJECT_UUID --logfire
```

The output directory must be new and its parent must exist. Initialization
checks that the repository has an `origin` remote and an existing `origin/main`
commit; the runtime fetches fresh main before preparing work. No existing
verifier is required: planning writes the proposed checks for host validation.
Initialization does not fetch, execute checks, start a runner, or edit the project. An instance
inside the project must be gitignored. Prefer an instance outside the checkout.

The generated config selects `verified-python`, one Codex Sol/medium worker,
explicit workspace retention, and the canonical workflow/prompt files from this
dotfactory installation. Keep that installation available. `--linear-project`
records ownership; local mode does not synchronize or automatically create
Linear issues. Register the task in Linear before submitting its identifier and
description. Issue discovery requires separately configured live tracker intake.

`--logfire` enables telemetry **independently** of Linear synchronization. Without
it the initialization receipt explicitly says disabled. Supply the existing
`OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` through the shell;
never paste credentials into factory.json or a task description. The endpoint is
`https://logfire-us.pydantic.dev` for the default US region; configure the EU
region and matching endpoint for EU projects. Replace the example
`projections.logfire.project` with your account/project identity before use.
Missing required telemetry environment values
fail activation; init does not claim authentication or delivery is verified.

```bash
export DOTFACTORY_CONFIG=/absolute/path/to/new-factory-instance/factory.json
python3 -m dotfactory run --project my-project --issue ISSUE-123 \
  --description-file /absolute/path/to/task.md --until-state Review
python3 -m dotfactory status --project my-project
python3 -m dotfactory status --project my-project --execution EXECUTION_ID
```

`status` uses the owner socket while running and an exclusive control-only
runtime while stopped. It does not launch runners or perform network preflight.
A missing ledger is reported without creating one. Stale/refused sockets may
fall back under the lock; timeouts and unsafe sockets do not. This command prints
the existing versioned observation JSON, not a new completion contract.

Initialization only checks structural prerequisites. Live runner authentication,
verification OS permissions, and successful Logfire delivery remain separate
runtime checks. A green initialization receipt is not an end-to-end run result.

## Verification definition

`.factory/verification.json` maps acceptance criteria to executable checks or
explicit human procedures:

```json
{
  "schema_version": 1,
  "verification_policy": {
    "timeout_seconds": 120
  },
  "criteria": [
    {
      "id": "blank-name",
      "requirement": "Reject empty and whitespace-only names",
      "kind": "automated",
      "files": [".factory/verify.py", "tests/test_greeting.py"]
    },
    {
      "id": "copy-review",
      "requirement": "Greeting wording is appropriate",
      "kind": "manual",
      "procedure": "Read sample greetings and approve the wording."
    }
  ]
}
```

`verification_policy` must be nested exactly as shown. Its `timeout_seconds` may
be 60 or 120. A top-level `timeout_seconds`, unknown top-level fields, unknown
policy keys, other types, and other values fail closed. Omitting the entire
`verification_policy` object keeps the historical 60-second deadline. Planning
receives this exact schema shape from the canonical resolver used for structural
validation and execution.

The verifier receives the exported source directory as `argv[1]`. It should run
these acceptance tests and relevant regression tests, print readable output, and
exit nonzero on failure. Declare every Python test/helper file it uses under a
`tests/` directory at any repository depth (`tests/`, `factory/tests/`, or
`packages/service/tests/`) or root `.factory/`. Paths must be canonical and
repository-relative; traversal, URI/percent encoding, symlinks, untracked files,
and arbitrary production-file paths are rejected. Tests may fail before implementation; review their assertions and
coverage before approving. Manual criteria remain human work even when code
checks pass. Structural validation does not prove requirements are fully covered.

## Manual planning review

Select the registered `verified-python` workflow and set host/project/runner
capacity to one. Use the existing Codex authentication/setup procedure. Configure
Linear team/project IDs for live intake; otherwise supply `--description-file`.
The fixture in `factory/examples/verified-python/` starts without verification.
For manual planning, use an audited control transition from Todo to Planning
before the scheduler claims the task (or from an unclaimed Ready checkpoint).
The following commands assume that manual path is already selected. Normal
Autoplanning does not enter PlanReview; run it until Review instead.

```bash
PYTHONPATH=factory/src python3 -m dotfactory run \
  --config factory/factory.json --project example-service \
  --issue ISSUE-123 --until-state PlanReview

PYTHONPATH=factory/src python3 -m dotfactory delivery \
  --config factory/factory.json --project example-service \
  --execution EXECUTION_ID --output /tmp/issue-123-plan
```

Inspect `verification-plan.json`, `checks/`, `change.patch`, and `review.json`.
Approve through the running operator socket using `operator command` and a
request file:

```json
{
  "action": "approve",
  "expected_state": "PlanReview",
  "parameters": {
    "plan_sha": "EXACT_REVIEWED_HEAD_FROM_PACKET",
    "note": "Reviewed acceptance coverage and manual checks"
  }
}
```

```bash
# Keep this running while issuing the command from another terminal.
PYTHONPATH=factory/src python3 -m dotfactory run \
  --config factory/factory.json --project example-service \
  --issue ISSUE-123 --watch --max-ticks 100

PYTHONPATH=factory/src python3 -m dotfactory operator command \
  --config factory/factory.json --project example-service \
  --execution EXECUTION_ID --command-id approve-plan-1 \
  --request-file /tmp/approve-plan.json
```

A human Linear transition from Planning to Ready must include approval feedback
naming the exact reviewed commit. PlanReview projects to Planning in Linear;
its distinct local state prevents agent pickup. A Ready issue adopted without a
local approved plan waits without launching implementation: explicitly transition it to
Planning first. PlanReview can also transition to Planning for revisions.

After implementation begins, agents cannot change approved checks. Code-only
rework uses Review → Reworking and the same approved checks. A verifier that is
itself unusable follows the separate audited path below.

## Recover an incompatible frozen plan

Do not edit an accepted plan during implementation or investigation. A failed
`python-verification-v2` receipt is classified as `frozen_verification_failed`.
Investigation must diagnose whether the product is wrong or the harness is
unusable. Product failures use ordinary recovery/rework. If investigation reaches
Blocked and the harness is unusable, an approver may request replacement planning:

```json
{
  "action": "replan",
  "expected_state": "Blocked",
  "confirmed": true,
  "parameters": {
    "owner": "planner-1",
    "reason": "The verifier passes the export path to unittest as a test name."
  }
}
```

The command fails unless the workspace still equals the exact failed verification
revision. It records that revision, the replaced plan attempt, the approver, and
the reason before entering Replanning. Replanning may change only the plan and
declared verification files. `ReplanReview` requires a new exact-SHA approval and
an owner for verification; approval proceeds directly to Verifying against the
implementation already present. The old plan, failure, investigation, and source
revision remain immutable ledger history.

This edge exists only in workflow snapshots created after its release. For an
older blocked execution, preserve it and use the cancel/new-execution procedure.
Structural validation still uses `ast.parse`; it proves syntax, not that a test
harness accepts the host invocation. Do not run proposed acceptance tests during
planning because pre-implementation assertion failures are expected.

## Export the delivered change

At Review, use `delivery` again with a fresh output directory. It writes
`README.md`, `review.json`, `change.patch`, `verification.txt`,
`verification-plan.json`, and `checks/`. The packet includes immutable issue
context, bounded feedback with truncation markers, and pending manual criteria.
Stop/drain an active watch before export; it uses the exclusive ledger lock.
`operator delivery` shows a bounded check summary while the runtime is active.

## Contracts and bounds

| Contract | Requirement |
|---|---|
| `plan-result-v2` | Committed plan, valid criteria, parseable verifier/tests, declared files only |
| `implementation-result-v2` | Automatically accepted or human-approved unchanged plan/checks; real implementation change and delivery report |
| `python-verification-v2` | Implementation requirements plus approved verifier passes on committed source |

V1 contracts remain supported for saved executions with base-pinned verification;
they do not gain a retrospective planning approval. Unknown names fail loading.
Receipts bind source, evidence, checks, and approval. All kernel entry paths
check them; saved results survive restart, and changed source invalidates them.
Failures carry concrete reasons into investigation; failed investigation stops
at Blocked. Cancellation remains available.

- Criteria: 1–32, at least one automated; definition at most 64 KiB.
- Frozen files: at most 64; evidence 1–32 committed local files, no symlinks/escape.
- Verification source: at most 2000 regular committed files/4 MiB.
- Run: the frozen 60- or 120-second policy, 4 MiB output; final 16 KiB retained
  with full output hash. Policy-free schema-version-1 plans retain 60 seconds.
- Receipt: actual `sys.executable` and Python version, isolated mode, restricted
  `PATH`, temporary `HOME`, raw committed-source export, secret exclusion, and
  requested/effective timeout.
- Raw Git export excludes ignored/untracked files and archive substitutions.
- Trusted project code has host OS permissions; this is not a security sandbox.
- Passing checks prove their assertions, not complete coverage, UI quality,
  deployment, live Linear delivery, or manual criteria. Human review remains.
- The host owns export; agents do not upload directly to Linear or merge.

The complete regression suite is separate evidence when it exceeds the host
budget. Set `DOTFACTORY_TEST_PYTHON` to an audited absolute Python 3.13 path and
run `/bin/bash factory/test.sh`; do not attribute that result to an older host's
60-second verifier.
