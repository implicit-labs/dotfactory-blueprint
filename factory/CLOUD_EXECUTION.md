# Cloud execution

Run one coordinator with its SQLite ledger. Place each stage on a user-owned
Render worker, an SSH-accessible Mac, or a local worker. Workers do not write Linear
or own workflow transitions.

## Configure

Add `execution` from `execution.example.json` to a schema-6 factory config.
Keep the existing workflows, project registry, runners, preparation, and Linear
settings. Configure every work state, including recovery and rework. Replace the example
`xcodebuild -list` smoke command with the actual build, simulator, and device
acceptance commands; listing a project is not verification of an iOS change.

| Setting | Contract |
|---|---|
| `workers` | Candidates in preference order; every candidate uses the same billing method |
| `scope: portable` | Explicit operator decision that this stage can run without Apple SDKs |
| `scope: native` | Tracked Xcode projects/workspaces require macOS and Xcode |
| `requires` | Checked `os:`, `arch:`, and `tool:` requirements; unknown kinds fail closed |
| `checks` | Argument arrays executed on the worker before accepting its output |
| `check_timeout_seconds` | Per-command deadline, 1–3600 seconds |
| `location` | Worker metadata: `local` (💻) or `cloud` (☁️), shown in Linear per selected attempt; omitted values display “Location unknown” |
| `billing` | `subscription` or `api`; missing authentication never changes billing |

Set `location` to where the worker physically runs, independently of transport:
Render workers are `cloud` even with `transport: local`; your Mac is `local` even
when reached over SSH. Placement labels are frozen with each execution policy;
config changes apply to new executions and do not relabel historical work.

`xcodebuild` and `xcrun` checks always require macOS, even with portable scope.
Declare requirements inside shell scripts explicitly; the precheck does not
interpret arbitrary shell code. A successful executable probe does not prove
physical-device access, signing identity, network reachability, or test data.
Represent those requirements with a verification command that fails when absent.

Missing requirements become durable scheduler attention before source allocation.
The workflow retains its existing owned work state with an attention reason;
this version does not introduce an `Awaiting environment` Linear status.
Resolve the cause and use the existing `attention --remedy retry` control.

## Set up Render

Use the manual-deploy Blueprint at `factory/deploy/owned-worker/render.yaml`.
It builds a non-root Docker background worker with pinned native coding CLIs,
a 10 GB disk at `/data`, and no HTTP ingress. The checked-in plan, region and
storage size are examples: review them against current provider pricing before
provisioning. Applying the Blueprint starts a paid service. Compute while running,
retained storage while suspended, the separate listener, and model usage are
separate costs. Resume before work and explicitly suspend after verified completion;
there is no automatic suspension or spending cap. Start with one worker.

1. Deploy the reviewed source revision from your reviewed repository. Verify the
   mount is writable by UID/GID 10001 and the service remains healthy.
2. Register the coordinator's public SSH key in Render; keep the private key on
   the coordinator. Verify Render's regional host fingerprint against its docs.
3. Copy the service's exact SSH target from **Connect → SSH** into the example
   config's `render.target`. Keep one instance; do not use an ephemeral shell.
4. Sign in directly on the worker with the selected native coding CLI. The image
   persists `CODEX_HOME=/data/codex` and `CLAUDE_CONFIG_DIR=/data/claude`, while
   `/opt/factory-home/.ssh` remains image-local for Render's managed SSH access.
5. Probe from the coordinator:

```bash
PYTHONPATH=factory/src python3 -m dotfactory worker-check \
  --config /absolute/path/factory.json --worker render --runner codex \
  --require os:linux --require tool:git
```

The image already includes `/opt/dotfactory/worker.py`; do not overwrite it with
`worker-install`. Rebuild the reviewed image to update the protocol. The existing
SSH transport carries bounded JSON and Git bundles. Render owns its SSH server;
the image must not start another one or listen on port 22.

Native login, an actual coding task, cancellation, Git return, and disk/restart
recovery are separate live gates. Render can disconnect SSH during maintenance
or deploys; the worker's lease bounds orphaned processes and the coordinator
retains ambiguous work for reconciliation. Drain work before a manual redeploy.
Do not assume reconnecting resumes an interrupted native session.

### On-demand operating procedure

The first version uses explicit Render dashboard resume/suspend actions. It does
not implement automatic waking or an idle-cost controller. A running worker is
billed even when no agent task is active.

1. Resume the dedicated worker in Render before dispatch; wait for its startup
   check, then run `worker-check`. Resuming alone does not prove readiness.
2. Run the selected task through the coordinator; keep lease renewal active.
3. After the result is imported and accepted, stop new dispatches. For failed or
   ambiguous work, cancel the owned attempt and reconcile it first. Do not suspend
   a worker with an active attempt, native-login session or an unknown owner.
4. Suspend the service in Render and verify its status is **Suspended**. Merely
   closing SSH or finishing the coding process does not stop compute billing.
5. Retain the disk and source evidence for the next run. On resume, verify native
   login and preserved attempt state before allocating more work.

The listener is a separate web service and receipt disk, configured under
`factory/deploy/linear-webhook/`. Its receipt does not launch a coding worker.
The same 2 GB service can host the coordinator and local coding stages, with
its ledger, repositories and workspaces on `/data`. This avoids a second paid
coordinator service. Keep the listener separate. The coding service is one trusted
operator boundary; process environment filtering is not a tenant security boundary.

### Host a complete instance

The image includes `python3 -m dotfactory`, canonical workflows and `init`.
It initially waits for authenticated setup; it does not discover or dispatch work.

1. Sign in to the native coding CLI, then clone the selected repository under
   `/data/repositories/`. Use a repository-scoped credential when required.
2. Initialize an instance on its persistent disk:

```bash
python3 -m dotfactory init --repository /data/repositories/example \
  --output /data/instances/example --project example --linear-project PROJECT_UUID
```

3. Configure the instance's Linear projection and stage placement. For colocated
   portable stages use a `transport: local` worker with `/data/work` as its root
   and explicit subscription billing. Keep Mac-only stages on a reachable Mac.
   Set native login environment references on the runner; never forward Linear
   credentials into the worker environment.
4. Run one selected issue with `python3 -m dotfactory run --config
   /data/instances/example/factory.json --project example --issue ISSUE-ID`.
   Stop at human gates, including exact-plan-SHA approval before implementation.
5. For continuous discovery, first configure the explicit admission label,
   exclusions, single-child limit and budgets in [continuous work](CONTINUOUS_WORK.md).
   Then set Render's `DOTFACTORY_CONFIG` to that config path
   and `DOTFACTORY_PROJECT` to `example`. Startup validates persistent ledger,
   repository and workspace paths, then executes the existing `work` command.
   Its instance lock prevents a second writer. Drain any manual run first.
6. Verify the selected issue, preserved state and native login after a restart.
   Suspend after work using the procedure above. A suspended coordinator cannot
   discover issues or react to incoming events; the receipt listener remains up.

Linear credentials belong to this coordinator role. A worker-only deployment
must omit them and leave both `DOTFACTORY_*` startup settings unset. Do not copy
a live Mac ledger to create a second active coordinator.

## Set up a Mac

Configure a reachable SSH target, absolute worker entrypoint, and private worker
root. The example `/opt` directories must be writable by the worker account;
replace them with account-owned absolute paths in both setup and config. Use native SSH key authentication and the account's native coding CLI login.
Install Xcode/toolchains under that account; no signing keys or personal browser
profiles are transferred by factory.

```bash
PYTHONPATH=factory/src python3 -m dotfactory worker-install \
  --target builder@mac-host --directory /opt/dotfactory-worker
```

A coordinator on a laptop may use `transport: local` for its own Mac. Both local
and remote worker modes allocate separate attempt directories and transfer Git
objects; a `local` worker is not a reference to the user's active checkout.

## Authentication and billing

| Mode | Precheck |
|---|---|
| Codex subscription | `codex login status` must report ChatGPT |
| Claude subscription | `claude auth status --json` must report native Claude account auth |
| Codex API | Worker key must exist and native login must report API-key auth |
| Claude API | Worker key must exist and native auth must report API-key auth |

Sign in directly on each worker. For Codex headless sign-in, use `codex login
--device-auth`. API mode requires native API-key login as documented by the
provider; setting a variable alone is not proof that a cached account changed.
Subscription processes exclude API credentials from the inherited environment.
Only explicit runner `environment_envs` references are resolved on the worker;
coordinator values are never forwarded. A revoked login or quota failure does
not trigger account rotation or API fallback. Render compute is billed separately.

## Run and host the coordinator

```bash
PYTHONPATH=factory/src python3 -m dotfactory run \
  --config /absolute/path/factory.json --project example --issue EXAMPLE-1
PYTHONPATH=factory/src python3 -m dotfactory work \
  --config /absolute/path/factory.json --project example
```

`work` discovers eligible issues, reuses durable executions, reconciles Linear,
and continues until signaled. Linear must be configured and reachable at startup;
later tracker errors retry on the next poll. Readable Linear evidence includes
worker, stage, handoff status, and accepted output commit.

`serve` retains the authenticated loopback control API. `work` enables the local
operator socket and uses the same runtime owner; do not start a second writer on
its ledger. Back up SQLite using its online backup API, not a live WAL file copy.

## Handoff and recovery

- Snapshot all nonignored task files into a Git commit without changing the index.
  Gitignored environment files, caches, and build products are excluded.
- Allocate a separate worker directory per execution/attempt with a bound source
  SHA. Existing allocations with different bindings are rejected.
- Resolve and copy only factory-declared skill packages. Skill symlinks fail closed.
- Parse native terminal events and the graph's result/evidence contract centrally.
- Run declared verification commands; fetch and validate the resulting Git bundle.
- Reject unrelated history or coordinator edits made during remote work. Retain
  both copies for recovery; never force-reset conflicting work.
- Journal import before fast-forwarding the coordinator's owned worktree. Restart
  after import reuses the same commit and accepted result.
- An independently enforced 60-second worker lease and stage deadline stop orphaned
  processes. Normal cancellation targets only that attempt's process group.
- A started worker attempt cannot run again implicitly. Ambiguous process failures
  require reconciliation; their files remain available for inspection.
- Worker directories are retained, including failed work. Remove only reviewed,
  no-longer-needed attempt directories; factory does not delete whole VM disks.

## Current boundaries

- Preparation resources allocated on the coordinator cannot be transferred. Such
  stages fail closed; use worker-native checks. Live device/session leasing remains
  outside this worker contract.
- Nonignored repository content and declared skills cross the worker boundary.
  Review `.gitignore` and repository contents before selecting remote execution.
- Git bundles are limited to 32 MiB; wire messages and output streams to 64 MiB.
- Git-backed plans travel as files. Prior conversational results and review
  feedback do not transfer; do not claim conversational continuity across workers.
- Offline tests use native-protocol fixtures in separate local worker processes.
  They prove orchestration and Git handoffs, not live Render, subscriptions,
  Xcode, or physical-device behavior.

## References

- [Render background workers](https://render.com/docs/background-workers)
- [Render SSH and Docker requirements](https://render.com/docs/ssh)
- [Render persistent disks](https://render.com/docs/disks)
- [Render pricing](https://render.com/pricing)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Claude hosted-product authentication](https://code.claude.com/docs/en/legal-and-compliance)
