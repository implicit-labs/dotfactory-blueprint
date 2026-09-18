# Setup

## Requirements

- Git
- Python 3.9 or newer
- Bash 3.2 or newer
- zsh for the optional shell configuration

Portless-backed local services additionally require Node 24 or newer and the
pinned `portless` 0.15.6 package.

The runtime uses the Python standard library. It does not require a package
manager or network access for its tests.

## Clone and verify

```bash
git clone https://github.com/implicit-labs/dotfactory-blueprint.git
cd dotfactory-blueprint
./factory/test.sh
./dotfiles/test.sh
```

Both commands must pass before customization.

## Configure the factory

Copy the public example to the ignored factory path:

```bash
cp factory/factory.example.json factory/factory.json
export DOTFACTORY_CONFIG="$PWD/factory/factory.json"
```

Edit `factory/factory.json` to choose the ledger location, register named DOT
workflows and profile files, select a workflow per project, set startup defaults,
configure scheduler limits, and configure projections. Relative workflow paths
resolve from the config file.
A runtime project
selector may narrow the enabled set without changing this registry.
`FactoryConfig` is immutable after loading: edit the JSON and restart the
factory to change the registry. Runtime pause/resume state will live in the
ledger once the listener is implemented.
Only selected projects resolve their repository and tracker environment
variables at startup, so an inactive project may remain unconfigured locally.
Once a project key is registered, its tracker type and stable project ID cannot
be changed; add a new key instead.
Fields ending in `_env` contain environment-variable names, never credentials.
Keep projections disabled until their named variables are available.
Runner integrations are separate from projections. For Codex, list `linear` in
`runners.<name>.disabled_mcp_servers` whenever Linear updates must flow only
through the factory or be handled manually. This disables the Codex MCP server,
withholds the Linear URL and internal ID from its prompt, and adds a cooperative
prompt rule against alternate direct access. Use sandbox or network policy when
alternate browser, shell, or API access must be mechanically blocked.
The launch checks the prepared workspace's configured server list first, so the
same policy also works on a clean Codex install where no Linear server exists.
Codex routes default to `gpt-5.6-sol` with medium reasoning and pass both values
explicitly at launch. Set `default_model` or `default_reasoning_effort` on the
route to change that baseline; DOT node and profile values take precedence.

For live Linear convergence, configure each Linear project's stable team and
project IDs, export the variables named by their `_env` fields, and export the
authorization value named by `projections.linear.token_env`. The factory binds
every workflow status to one team status ID before activation. Polling is the
recovery path; a signed webhook may only accelerate it. A timeout after a status
write remains ambiguous until a read confirms the remote issue.

Agent Sessions are optional because Linear requires an OAuth app token acting as
an agent. Inject it using `agent_token_env` (default `LINEAR_AGENT_TOKEN`),
separately from the issue/status credential. Missing agent credentials retain
classic comment fallback. Follow the
[native-session canary](../factory/LINEAR_AGENT_SESSIONS.md). Set
`agent_sessions_enabled` only with that actor configured and give
`agent_session_url_template` one `{execution_id}` placeholder. The factory emits
only workflow milestones, attention, errors, and a terminal response. It stores
session and activity identities before writes, reconciles unknown outcomes by
identity, and falls back to the one owned summary comment when the preview API is
unsupported. It never retries an unreconciled session create blindly.

For Logfire, set `project` to your Logfire project identity, choose its `us` or
`eu` region, and inject the matching OTLP endpoint and write-token header through
the named environment variables.
The runtime sends JSON over HTTP to `/v1/traces`; projection failures are durable
and fail-soft. Hosted datasets are separate and disabled by default. Enabling
them requires `LOGFIRE_PROJECT_API_KEY` with only `project:read_datasets` and
`project:write_datasets`; the telemetry write token is not accepted for dataset
operations.

Telemetry mapping v2 freezes a durable upload plan before network access. Its
hierarchy anchors are zero-duration ownership markers, not completed operations;
observation leaves carry the actual failure and timing facts. Inspect the
returned `logfire.delivery` status when a lifecycle tick reports a paused
projection. Partial acceptance and permanent errors block automatic delivery;
the ledger remains intact and the factory can continue work. Do not delete a
plan or invent a new command to force a partial batch to resend.

For an integration that publishes native Linear Agent Sessions, deploy the dedicated
[receipt-only webhook receiver](../factory/deploy/linear-webhook/README.md)
before enabling `AgentSessionEvent` on the OAuth app. Keep the service's signing
secret and inbox volume separate from the factory's OAuth token and ledger.
This enables receipt auditing, not prompt-driven execution: mentions and replies
are **not dispatched** by this service. Keep the app private and do not advertise
it as an interactive agent until a reviewed inbox consumer exists. An authorized
outbound native-session publisher is a separate prerequisite; this receiver does
not provide one.

The default worktree pool is `<project checkout>/.worktrees`. Add it to the
project's `.gitignore` before activation:

```gitignore
/.worktrees/
```

An `EXAMPLE-123` first execution uses `.worktrees/EXAMPLE-123-1` and branch
`factory/example-123-1`. Preparation verifies the local pool is ignored before it
creates the directory or fetches Git. An explicit project `root` or `root_env`
may place the pool elsewhere; an environment root must be absolute. No root is
derived from the factory process working directory.

Install and verify stable local services only when `local-web` is enabled:

```bash
export PATH="/path/to/node-24-or-newer/bin:$PATH"
skills/stable-local-services/scripts/install.sh
portless trust
portless service install
dotfactory-portless-preflight
```

The installer refuses to change the global npm installation until Node is
compatible. Trust and service installation are explicit operator actions. The
preflight must then report zero doctor failures and warnings non-interactively.
Do not bypass it with a raw port or Portless `--force`. LAN, tunnel, wildcard,
and custom-domain modes are outside the default capability.

Load and validate the configuration:

```bash
PYTHONPATH=factory/src python3 -c \
  'from dotfactory import FactoryConfig; print(FactoryConfig.from_environment().values["factory_id"])'
```

Run a credential-free Git-backed lifecycle before using a real project:

```bash
PYTHONPATH=factory/src python3 -m dotfactory demo
```

For a configured project, start one named issue:

```bash
PYTHONPATH=factory/src python3 -m dotfactory run \
  --config factory/factory.json --project example-ios --issue TASK-600 --until-state Review
```

Omitting `--issue` enables Linear pickup discovery and therefore requires the
configured token. Discovery selects one issue per invocation. `--watch` keeps
polling that execution up to `--max-ticks` (100 by default) or interruption.
Without it, the command stops at a human checkpoint, attention
request, or terminal state and prints a deterministic lifecycle receipt. It
returns zero after an idle settled boundary, a confirmed target state, or an
operator drain. Attention, capacity, signal, and max-tick stops return nonzero
while preserving the receipt on stdout.

`--until-state Ready` stops after planning, before implementation dispatch.
`--until-state Review` stops after verification. A restart already at the target
does no work; unknown, unreachable, or previously passed targets fail. Linear
status confirmation is required when projection is enabled.

A new Linear execution adopts its current pickup checkpoint and snapshots the
issue description and revision. Explicit issues outside the selected project or
pickup states are rejected. With Linear disabled, use `--description-file` to
supply requirements. Requirements are immutable for that execution; use review
feedback to request changes. Each attempt receives a saved handoff containing
prior outcomes, artifact references, feedback, and failures. Bounds and omitted
items are disclosed in `omitted_or_truncated`; referenced files are not fetched.

The first composition holds one process lock per ledger and permits one SQLite
writer. It does not host the WSGI control API or webhook endpoint.

Use a stable scheduler owner per machine process. `claimed` and `prepared`
claims expire safely. `preparing`, `dispatching`, and `result_ready` do not;
the same owner reconciles them after restart. If that owner cannot return,
resolve the emitted attention request instead of editing the ledger.

For scheduler-owned attention, record the remedy with the exact IDs printed in
the lifecycle receipt:

```bash
PYTHONPATH=factory/src python3 -m dotfactory attention \
  --config factory/factory.json --project example-ios \
  --execution EXECUTION_ID --attention-id ATTENTION_ID \
  --expected-state Investigating --expected-attempt ATTEMPT_ID \
  --remedy retry --command-id operator:ATTENTION_ID:retry
```

Attention resolution and scheduler execution are separate crash-safe steps.
The attention command reaches the active runtime over its local socket. If no
runtime is active, it opens a control-only runtime and skips runner and Linear
preflights; run the lifecycle again to reconcile the recorded remedy. A stale
socket with no listener falls back only after acquiring the exclusive ledger lock. For a
`result_ready` inspection, use `--until-state` with the intended checkpoint
after the stored result. This avoids guessing a scheduler tick count.
The commit completes the old attempt but preserves its execution and worktree.

## Operate a running factory

The `run` command opens an owner-only Unix socket beside its ledger. The runtime
handles commands on its existing ledger thread, including during live runner
heartbeats. Tracker reads run separately from the writer so an outage cannot
block local control. The local OS account is the approver; this is not an isolation
boundary against other processes running as that same account.

```bash
PYTHONPATH=factory/src python3 -m dotfactory operator status \
  --config factory/factory.json --project example-ios --execution EXECUTION_ID
PYTHONPATH=factory/src python3 -m dotfactory operator artifacts \
  --config factory/factory.json --project example-ios --execution EXECUTION_ID
PYTHONPATH=factory/src python3 -m dotfactory operator command \
  --config factory/factory.json --project example-ios --execution EXECUTION_ID \
  --command-id operator:EXECUTION_ID:cancel --request-file cancel.json
PYTHONPATH=factory/src python3 -m dotfactory operator drain \
  --config factory/factory.json --project example-ios
```

Use the returned `available_actions` and exact current state to construct a
[control request](../factory/CONTROL_API.md). For example, `cancel.json`:

```json
{"action":"cancel","expected_state":"Implementing","confirmed":true}
```

Reuse the command ID and identical request after a lost response. Cancellation
terminates the child owned by the live runner. Drain lets the current dispatch
finish, then exits before starting another; it does not cancel the issue.
Polling cannot establish ownership of an unknown process after a crash, so
ambiguous dispatch recovery still requires inspection (see ambiguous dispatch recovery).

Terminal cleanup honors durable retain/quarantine decisions across restarts.
A retained workspace exposes a `release` attention remedy. Use `attention`
with `--confirm`, the terminal `--expected-state`, and no `--expected-attempt`.
Release still requires clean files, matching provenance, and no outstanding
resource allocation. It never force-removes dirty work.

`ControlHTTPApp` exposes the [v1 control contract](../factory/CONTROL_API.md) to
a WSGI host. The host must authenticate each request and return a `Principal`;
there is no permissive default authenticator. Do not expose the adapter directly
to a network without that boundary.

## Retention

The SQLite ledger retains all canonical records indefinitely. The runtime does
not automatically delete, prune, or vacuum the database. As checked on
2026-08-28, Logfire Personal keeps telemetry for 30 days, so Logfire is an
operational view rather than long-term storage. Stop the normal projection
worker before replaying delivered outbox records into a fresh or cleared
external destination. Give each rebuild a stable command ID and initiator;
retry that command to resume its durable session. Projection sinks must
deduplicate at-least-once delivery by `event_id`.

Raw provider streams and artifact files live outside SQLite. Include them
separately when measuring local storage. Measure usage before introducing any
deletion policy.

## Recovery

| State | Action |
|---|---|
| `busy` | retry preparation; do not complete or recreate the runner attempt |
| retry deadline reached | request attention; do not consume DOT runner retries |
| `needs_attention` | follow only the request's allowed remedies |
| dirty workspace | retain and quarantine; never force-remove it |
| unknown process or route | retain and escalate; ownership is not proven |
| `release_pending` | finish provider cleanup before dispatching new work |
| ambiguous preparation | inspect mutations, then retry the recorded safe phase |
| ambiguous dispatch | retain and escalate; there is no automated retry or cancel |
| scheduler `result_ready` | authorize retry, then replay the stored result; never rerun the runner |

Cleanup is planned in the ledger before mutation. Worktrees remain until the
execution is terminal or an operator explicitly requests cleanup.

## Configure the shell and harnesses

Review `dotfiles/.zshrc.template`, then source it from your own `~/.zshrc`.
Keep API keys in a password manager or private environment file outside this
checkout.

Harness templates live under `dotfiles/harness-config/`. Render an OMP profile
to standard output with:

```bash
dotfiles/harness-config/omp/render.sh default
```

Install the rendered output and other harness files only after reviewing their
target paths in `dotfiles/harness-config/README.md`. Authentication state,
tokens, session history, hooks, and per-project trust remain machine-local.

## Update

Pull the repository, inspect the changes, rerun both test commands, then reapply
only the templates you use. Do not overwrite machine-owned authentication or
session state.

## First verified change

Follow [Verified Python delivery](VERIFIED-DELIVERY.md) for `init`, automatic
planning, frozen checks, implementation, and final Review. `doctor` checks local
readiness without starting work. `status` reads active or stopped executions.
Use [the local HTTP gateway](../factory/CONTROL_API.md#run-the-local-http-gateway)
to serve the existing control API; setup remains CLI-only.
