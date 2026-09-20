# Verification methods during planning

Project configuration defines reusable methods. A run selects them during
Planning; PlanReview approves the exact selection and commit. Configuration is
JSON in the existing instance file, under `projects.<key>.verification`.

## User journey

1. Connect the project: register commands, eligible hosts, prerequisites,
   scenarios and evidence. Commands are trusted project configuration. Never
   embed credentials; this executor does not inject provider keys.
2. Plan a change: the planner lists affected paths and change categories. The
   runtime adds project defaults and matching rules. Omitted run settings inherit
   the project methods. Explicit additions/removals appear in the review.
3. Review: read `planning.verification_summary` in operator status/control API,
   and the same explanation in `.factory/plan.md`. For example:
   “Checkout UI: browser interaction check, desktop/mobile screenshots before
   and after, and a checkout recording; run on the browser host.”
4. Chat: “Also check mobile Safari” or “This is comment-only; omit visual checks.”
   Use `operator planning-chat` or reply in the bound
   [Linear planning conversation](../LINEAR-PLANNING.md) on an enabled project.
   A removal needs a reason; a new message invalidates the earlier proposal.
5. Approve: approve the exact plan commit and requirements digest. In Linear,
   the explicit versioned approval binds to that same frozen proposal.
6. Run: the runtime checks all selected hosts before coding and captures baseline
   artifacts at the approved source revision. After implementation it runs the
   methods on the result revision. Missing prerequisites, command failure, missing
   artifacts or an uncovered source change block completion.
7. Review evidence: `verification_baseline` and `verification_methods` in the
   delivery receipt contain source SHA, selected host/location, command result,
   scenario and artifact hashes/paths. Review export carries these receipts.

## Project configuration example

This fragment belongs under a project. The repository must supply the referenced
verification harness; configuring this example alone does not install Playwright.

```json
{
  "verification": {
    "defaults": ["regression"],
    "rules": [
      {"paths": ["web/*"], "categories": ["frontend"], "methods": ["browser"]},
      {"paths": [], "categories": ["interaction"], "methods": ["browser"]}
    ],
    "methods": {
      "regression": {
        "description": "Run backend regression checks",
        "hosts": ["coordinator"],
        "requires": ["tool:python3"],
        "readiness": [],
        "commands": {"after": ["{python}", "-m", "unittest", "discover", "-s", "tests"]},
        "artifacts": [],
        "files": ["tests"],
        "timeout_seconds": 120
      },
      "browser": {
        "description": "Verify checkout behavior at a 390x844 viewport",
        "hosts": ["browser-worker"],
        "requires": ["tool:node"],
        "readiness": [{"name": "browser-fixture", "command": ["node", "/opt/check-browser-fixture.js"], "timeout_seconds": 10}],
        "commands": {
          "before": ["node", "checks/checkout.mjs", "--output", "{artifacts}"],
          "after": ["node", "checks/checkout.mjs", "--output", "{artifacts}"]
        },
        "artifacts": [
          {"phase": "before", "path": "checkout.png", "kind": "screenshot", "scenario": "checkout, 390x844"},
          {"phase": "after", "path": "checkout.png", "kind": "screenshot", "scenario": "checkout, 390x844"},
          {"phase": "after", "path": "checkout.webm", "kind": "recording", "scenario": "checkout, 390x844"}
        ],
        "files": ["checks/checkout.mjs"],
        "timeout_seconds": 300
      }
    }
  }
}
```

For iOS, register an equivalent method with `hosts: ["mac-worker"]`,
`requires: ["os:darwin", "tool:xcodebuild", "tool:xcrun"]`, a readiness command
that checks the exact simulator/runtime/fixtures, and an existing harness that
builds, exercises the app and writes its screenshots/recording. Merely checking
that `xcrun` exists is insufficient. Project owners choose the device and harness;
the planner may select registered methods but cannot invent them.

## Rules and overrides

- Defaults always apply; matching rules add methods (path or category match).
- `.swift`, storyboard/XIB and Xcode project paths infer `ios`; common UI
  extensions infer `frontend`; Python/Go/Rust/Java infer `backend`.
- iOS/frontend selections must cover before/after screenshots; interaction
  selections must cover an after recording. Missing registered methods block
  the plan. An explicit, reasoned omission is a visible waiver for review.
- These are conservative hints, not semantic classification. Project path rules
  cover custom layouts; the planner explicitly declares interaction changes.
- Run `add` lists registered methods. Run `omit` maps selected methods to reasons.
  Both require exact plan approval; at least one method must remain.
- The registry freezes at admission. Editing instance configuration affects new
  runs only. Existing runs do not acquire changed commands or requirements.
- Actual changed paths are checked again. An uncovered required method needs a
  revised plan/new run, not a silent downgrade.

## Execution and proof boundaries

Declare repository harness inputs (including imported helpers) in `files`;
file and directory paths are supported. Planning freezes their tracked manifests
and hashes. An implementation cannot replace, remove or add declared harness
files to manufacture a pass. Relative script arguments must be declared. Use
`files: []` only for inline commands or trusted executors installed on the host.
Host-installed executors and undeclared transitive dependencies remain a host
trust boundary; the runtime cannot infer arbitrary imports.

Commands use argv, not a shell. `{source}`, `{artifacts}` and `{python}` resolve
on the selected host. The source is an isolated checkout of the exact SHA;
artifact output starts empty. Before/after declarations must match scenario and
kind. Readiness must be self-contained; it runs outside the source checkout.

Hosts are `coordinator` or registered execution workers, tried in configured
order. Workers require the new `verification-probe` and `verify-method` protocol
operations. An old/offline worker fails eligibility; no service is provisioned.
Verification does not require a coding-model login. It shares the host's tool
PATH, HOME and simulator services, but not ambient provider-key environment
variables. This is trusted project code, not an OS security sandbox.

Method results are separate from the existing pinned Python regression lane;
both must pass. Artifact type headers, hashes, source identity and matching
scenarios prove provenance, not that a screen looks correct. The configured
harness must assert behavior; humans still review visual output.

Limits: 32 methods; 16 artifacts per method; 8 MiB of artifacts per phase;
1–1800 second method deadline; existing Git bundle transfer limits apply.
No credential injection, fixture provisioning, browser/Xcode installation,
semantic screenshot judging, or shared-device reservation is added. A host
should dedicate its simulator/browser fixture to avoid competing runs.
Cancellation prevents accepting a result, but a running method may continue
until its configured deadline; immediate remote command cancellation is not
implemented. Keep deadlines appropriate for commands with side effects.
An interrupted method start is uncertain and is not replayed automatically.
Investigate before retrying. Verifier-only Replanning cannot change this contract;
start a new reviewed Planning run when its methods need replacement.

Opted-in projects require a workflow with reviewed `plan-result-v2`,
`implementation-result-v2`, and `python-verification-v2` stages. Admission rejects
other workflows rather than silently ignoring their methods.

Projects without `verification` preserve their existing behavior. Adoption is
explicit so existing Python-only runs do not silently claim UI verification.
