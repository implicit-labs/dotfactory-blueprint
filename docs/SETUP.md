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

For live Linear convergence, configure each Linear project's stable team and
project IDs, export the variables named by their `_env` fields, and export the
authorization value named by `projections.linear.token_env`. The factory binds
every workflow status to one team status ID before activation. Polling is the
recovery path; a signed webhook may only accelerate it. A timeout after a status
write remains ambiguous until a read confirms the remote issue.

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
skills/stable-local-services/scripts/install.sh
```

The preflight must pass non-interactively. Do not bypass it with a raw port or
Portless `--force`. Trust-changing, LAN, tunnel, wildcard, and custom-domain
modes are outside the default capability.

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
  --config factory/factory.json --project example-ios --issue TASK-600
```

Omitting `--issue` enables Linear pickup discovery and therefore requires the
configured token. `--watch` keeps polling after the run reaches a stable
boundary. Without it, the command stops at a human checkpoint, attention
request, or terminal state and prints a deterministic lifecycle receipt.

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
The attention command uses a control-only runtime, skips runner and Linear
preflights, and changes only the audited control state; run the lifecycle again
to reconcile it. For a `result_ready` inspection, `--max-ticks 2` performs
the resume tick and stored-result commit without launching the new state attempt.
The commit completes the old attempt but preserves its execution and worktree.

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
