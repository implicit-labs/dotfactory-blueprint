# dotfactory

Portable agent configuration and a crash-safe orchestration kernel.

| Area | Purpose |
|---|---|
| `dotfiles/` | shell and agent-harness configuration |
| `factory/` | workflow contract, SQLite ledger, and projection workers |
| `skills/` | independently installable agent capabilities |
| `docs/decisions/` | durable architecture decisions |
| [`review_protocol.md`](review_protocol.md) | change, review, and merge checklist |

The factory provides a one-process lifecycle command. It is not a hosted control
service or PR listener.

## What the ledger does

The ledger is the factory's local memory and completion boundary. It stores:

| Record | Meaning |
|---|---|
| project | one configured source lane, such as an app or service |
| workflow execution | one numbered run of an issue, such as `TASK-567-2` |
| state run | one period spent in a checkpoint or work state |
| attempt | one owned agent or human try, including its lease and outcome |
| event and artifact | ordered evidence produced by the attempt |
| outbox item | a committed update waiting to reach Linear or Logfire |
| preparation | fenced worktree and resource setup before runner launch |
| allocation | an attempt- or execution-owned external resource |
| scheduler dispatch | fenced admission, launch intent, and recorded runner result |
| attention request | a deduplicated operator escalation with safe remedies |

An agent exiting successfully is not enough to complete work. The factory first
validates the outcome and commits the outcome, next state, and outgoing updates
to SQLite in one transaction. A crash or remote outage can delay projection,
but cannot erase or reroute the accepted work. Linear and Logfire are views of
the ledger; neither is the source of truth.

The local SQLite ledger currently retains every canonical row indefinitely.
There is no automatic deletion, TTL, pruning, or vacuum policy. Raw provider
streams and referenced artifact files are separate from the database and are
not counted as ledger storage. Add a read-only storage report before adopting
any pruning policy.

One factory may register several projects while selecting only the projects it
should run now. Project keys scope work items; repeated executions keep the
issue key for the first run and add `-2`, `-3`, and so on. Repeating the same
idempotent command returns the original run instead of creating a suffix.

Workflows are authored as typed DOT graphs. Node IDs are internal state identity;
labels and Linear statuses are optional projections. Node and profile attributes
select prompts, skills, runners, models, retries, timeouts, evidence, and exit
contracts. The kernel snapshots the resolved graph and digest when a run starts,
so later configuration edits do not reinterpret active or completed work.

Repository-backed attempts use `PreparationEngine` before live dispatch. It
creates or reconciles one issue-named worktree per execution, expands logical
resource names, journals external mutations, compensates partial setup, and
returns immutable `PreparedLaunch`. Live runner adapters never receive a raw
`RunnerRequest`.

Every observed Linear status change is compared with the run's snapshotted
workflow. An allowed human move is committed; a premature, unknown, or ambiguous
move is recorded as rejected without changing local state and queues
reconciliation. Feedback requirements and pending handoffs come from edge policy,
not special state names. A
listener can record only changes it receives; webhook delivery is required to
capture a status that changes back between polling intervals.

For each Linear-backed execution, the lifecycle also creates one Dotfactory-owned
comment and updates it in place. The comment shows the current outcome, attempts,
attention, workspace cleanup, trace identity, and a compact incident/cause view.
Its UUID and desired body are committed before network I/O; an unknown write is
read back by exact ID before another mutation. Linear outages delay this view but
cannot fail or roll back the run. The full waterfall remains outside the issue.
The separate `evidence-to-linear` capability owns verification screenshots,
recordings, and reports; it does not mutate the runtime summary comment.

## Observe and control runs

The versioned [control API](factory/CONTROL_API.md) exposes bounded, redacted
run views and audited `cancel`, `retry`, `approve`, and `transition` commands.
The host must authenticate requests and supply verified roles. The package
provides a WSGI adapter, not a hosted server or authentication provider.

## Run one lifecycle

Verify the complete Git-backed toy path without credentials:

```bash
PYTHONPATH=factory/src python3 -m dotfactory demo
```

The command prints persistent paths to a lifecycle receipt and local waterfall.

Run a configured issue until the next human, attention, or terminal boundary:

```bash
export DOTFACTORY_CONFIG="$PWD/factory/factory.json"
PYTHONPATH=factory/src python3 -m dotfactory run \
  --project example-service --issue TASK-600
```

Omit `--issue` to discover the oldest Linear issue in a workflow pickup status.
Add `--watch` to keep polling. The runtime holds one ledger instance lock and
does not enable concurrent writers.

Projection and runner access are independent. The example Codex route disables
its configured Linear MCP so that connector cannot bypass the durable kernel.
Manual Linear operation keeps both the projection disabled and this MCP rule.
The prompt also forbids alternate Linear access; enforce shell and network
isolation separately when a cooperative prompt policy is insufficient.

Resolve scheduler attention as a separate audited step:

```bash
PYTHONPATH=factory/src python3 -m dotfactory attention \
  --project example-service --execution EXECUTION_ID \
  --attention-id ATTENTION_ID --expected-state Investigating \
  --expected-attempt ATTEMPT_ID --remedy retry \
  --command-id operator:ATTENTION_ID:retry
```

The command uses a control-only runtime: it does not preflight or launch runners
and does not contact Linear. It authorizes recovery but does not execute the
recorded safe phase. Restart `run` to reconcile it. Repeating the same command
ID is idempotent.

## Set up Pydantic Logfire (optional)

[Pydantic Logfire](https://pydantic.dev/docs/logfire/) is the optional
observability destination. It is not required for the ledger, and enabling it
does not make Logfire authoritative.

As checked on 2026-08-28, Pydantic's free Personal plan provides 30-day
retention and up to 10 million telemetry records per month with no paid
overage. Older Logfire data is pruned, while the local ledger remains available
for full replay. See
[Logfire pricing](https://pydantic.dev/pricing) and the
[billing and retention guide](https://pydantic.dev/docs/logfire/manage/logfire-costs/).

1. Create a Logfire project and a
   [write token](https://pydantic.dev/docs/logfire/manage/create-write-tokens/).
2. Keep the token outside Git and export the standard OpenTelemetry settings:

   ```bash
   export OTEL_EXPORTER_OTLP_ENDPOINT=https://logfire-us.pydantic.dev
   export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
   export OTEL_EXPORTER_OTLP_HEADERS='Authorization=your-write-token'
   export OTEL_SERVICE_NAME=dotfactory
   ```

   Use `https://logfire-eu.pydantic.dev` for the EU region.
3. Copy `factory/factory.example.json` to the ignored
   `factory/factory.json`, register the projects this factory may operate, then
   set `projections.logfire.enabled` to `true`.

The current repository provides the durable outbox and fail-soft projection
worker, but not the hosted runner or Logfire sink. Enabling the config alone
does not transmit data yet. A runner supplies a sink using Logfire's
[OpenTelemetry interface](https://pydantic.dev/docs/logfire/guides/alternative-clients/),
then retries committed outbox items until delivery succeeds.
Previously delivered events can be replayed through a durable session with a
fixed event range, command ID, initiator, progress, and failure record. Retrying
the same command resumes only unfinished items. Delivery is at least once, so
every sink must deduplicate using the stable `event_id`. Run rebuilds with the
ordinary worker stopped so live delivery and historical replay do not overlap.

## Quickstart

```bash
git clone https://github.com/implicit-labs/dotfactory-blueprint.git
cd dotfactory-blueprint
./factory/test.sh
./dotfiles/test.sh
```

Continue with [setup](docs/SETUP.md), then choose supported extension points in
[customization](docs/CUSTOMIZATION.md).

Never commit credentials or real instance configuration. Configuration files
name environment variables; the environment or a secret manager supplies their
values.
