# Factory control API v1

The host authenticates every request and supplies a `viewer`, `operator`, or
`approver` principal. The core does not parse credentials. Responses are JSON
with `Cache-Control: no-store`.

## Run the local HTTP gateway

After `init` and a first local `run` have created the ledger, run this from the
repository checkout containing the `serve` command:

```bash
export DOTFACTORY_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DOTFACTORY_CONFIG=/absolute/path/to/instance/factory.json
PYTHONPATH=factory/src python3 -m dotfactory serve --port 8765
```

Keep that terminal running. From a second terminal with the same token:

```bash
curl --fail-with-body -H "Authorization: Bearer $DOTFACTORY_API_TOKEN" \
  http://127.0.0.1:8765/v1/overview
curl --fail-with-body -H "Authorization: Bearer $DOTFACTORY_API_TOKEN" \
  'http://127.0.0.1:8765/v1/runs?limit=10'
```

- This is a new local API secret, **not** a Linear or Logfire key. Store/share it
  through your local secret manager; do not paste it into tickets or URLs.
  `--token-env NAME` selects another environment variable. Restart to rotate it.
- The default role is `viewer`. Start a separate explicitly authorized instance
  with `--role operator --subject your-client` for commands; use `approver` only
  for clients allowed to approve workflow edges. All roles can read the entire
  factory ledger; project query filters are **not** access controls.
- A running factory receives requests on its mode-0600 owner socket. A stopped
  factory is opened under an exclusive control-only lock for each request.
  Neither path launches agents, admits tickets, or starts a second scheduler.
- Stop with Ctrl-C or SIGTERM. The gateway closes its listener; it does not stop
  a separate factory run. There is no installed background service.
- Missing ledgers fail startup. An older running factory without HTTP forwarding
  must be restarted from this version. Lock contention, owner timeouts, transport
  errors, and oversized owner responses return 503 without replaying commands.
- Requests are bounded to 32 KiB bodies and 64 KiB serialized socket envelopes;
  active-owner responses are also bounded to 64 KiB. Use smaller page limits for
  large collections. An oversized unpaginated response may remain unavailable
  while the owner is running. Connections are serial and body reads time out
  after five seconds; this is a local operator gateway, not a public web server.
- Command timeouts can mean **outcome unknown**, not failure. Read the receipt at
  `/v1/commands/{command_id}`, then retry only with the identical
  `Idempotency-Key`, principal, and body. Never generate a new key for a retry.

Example command after restarting the gateway with `--role operator`:

```bash
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $DOTFACTORY_API_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cancel-example-1' \
  --data '{"action":"cancel","expected_state":"Todo","confirmed":true,"parameters":{"reason":"No longer needed"}}' \
  http://127.0.0.1:8765/v1/runs/EXECUTION_ID/commands
```

Use the execution's observed state, not the example's `Todo`. Role and subject
come from gateway startup, never request headers or body fields. The owner
trusts this principal only across its existing same-UID socket: processes under
that operating-system account already have local operator authority.

## Setup versus serving versus Linear

### Approve planned verification

The initialized `verified-python` Autoplanning lane continues automatically to
implementation after host validation. Only manual Planning stops at `PlanReview`.
For that manual path, export its plan
packet with `dotfactory delivery` and review `verification-plan.json`, `checks/`,
and `change.patch` before approving. From a gateway started with `--role approver`:

```bash
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $DOTFACTORY_API_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: approve-plan-1' \
  --data '{"action":"approve","expected_state":"PlanReview","parameters":{"plan_sha":"EXACT_REVIEWED_HEAD_FROM_PACKET","note":"Reviewed proposed acceptance checks"}}' \
  http://127.0.0.1:8765/v1/runs/EXECUTION_ID/commands
```

The exact reviewed commit is mandatory. Viewer/operator tokens cannot approve.
Approval moves the execution to Ready; it does **not** start a worker. If the
worker is stopped, resume the same issue with `dotfactory run --until-state Review`
and the same config/project/issue arguments. A running worker can pick up Ready.
Pinned checks remain bound across restarts. Automatic authorization is a recorded
Autoplanning handoff, not a synthetic human approval or an HTTP approve call.

### Command ownership

| Operation | Interface | Requires a running factory worker? |
|---|---|---|
| Create configuration | `dotfactory init` (CLI only; no init endpoint) | No |
| Inspect status | `dotfactory status` or gateway `GET /v1/runs` | No; ledger must exist |
| Start agent work | `dotfactory run` | Starts the worker |
| Host existing observation/control routes | `dotfactory serve` | No; forwards when a worker is running |

The listener binds **127.0.0.1 only**, without TLS. There is no LAN/public host
flag, CORS support, tunnel, or Linear webhook registration. Browser-origin
requests are rejected. Do not reverse-proxy it onto the internet as-is.
Linear cloud cannot call your laptop's loopback address. Remote integration
still needs an explicitly approved authenticated ingress/relay and webhook
identity/replay handling; serving these routes does not implement that listener.

## Reads

| Endpoint | Query | Returns |
|---|---|---|
| `GET /v1/overview` | — | Factory identity, projects, run counts, active leases and allocations, open attention, and projection outbox counts. |
| `GET /v1/runs` | `project_key`, `status`, `state`, `limit` 1–100, `cursor` | Run summaries and `next_cursor`. |
| `GET /v1/runs/{execution_id}` | — | Intent, state, active attempt, workspace summary, preparation, allocations, attention, Linear evidence delivery, projection lag, and available actions. |
| `GET /v1/runs/{execution_id}/events` | `after_seq` ≥ 0, `limit` 1–100 | Ordered normalized events and `next_after_seq`. |
| `GET /v1/runs/{execution_id}/trace` | `after_seq` ≥ 0, `limit` 1–100 | Canonical trace records and `next_after_seq`. |
| `GET /v1/runs/{execution_id}/errors` | `after_seq` ≥ 0, `limit` 1–100 | Normalized error facts and `next_after_seq`. |
| `GET /v1/runs/{execution_id}/summary` | — | Deterministic summary-fact v1 for sparse external projection. |
| `GET /v1/runs/{execution_id}/waterfall` | — | Payload-free waterfall-fact v1 from one fixed trace range. |
| `GET /v1/runs/{execution_id}/waterfall.html` | — | Self-contained accessible waterfall and grouped error view. |
| `GET /v1/runs/{execution_id}/artifacts` | `kind`, `limit` 1–100, `cursor` | Evidence references and redacted metadata. |
| `GET /v1/runs/{execution_id}/feedback` | `limit` 1–100, `cursor` | Feedback records. |
| `GET /v1/resources` | `status`, `project_key`, `execution_id`, `limit` 1–100, `cursor` | Legacy leases and scoped allocations without fence tokens. |
| `GET /v1/commands/{command_id}` | — | Durable request, principal, authorization, result/error, and audit events. |

Cursor values are opaque. Missing `limit` defaults to 25, except events, trace,
and errors, which default to 100. Waterfall and summary facts share the same
`through_trace_seq` boundary. The HTML view contains no trace payloads, prompts,
commands, session IDs, fence tokens, or credentials.

`linear_evidence` is `null` before a Linear-backed execution is staged. Otherwise
it reports the owned comment ID, desired/applied digest, `pending`, `sending`,
`ambiguous`, `confirmed`, or `failed` state, last redacted error, and remote URL.
The comment is a rebuildable view; its state never changes the workflow result.

### Projection health

Overview, run snapshots, and each execution in lifecycle receipts include
`projection_health` v1. Existing aggregate fields remain unchanged.

- Channels separate Linear status, evidence comments, agent sessions/activities,
  legacy event outboxes, and Logfire traces. Counts are local source records.
- Each channel reports configured `enabled`, pending/retry/ambiguous/failed counts,
  oldest outstanding age, last confirmed local delivery reference, and a safe error.
- `disabled` retains queued counts without presenting them as an active delivery
  failure. Legacy event outboxes have no sender in the composed runtime; they are
  not the dedicated Linear status queue or Logfire trace delivery queue.
- `unknown` means a standalone ledger reader has no runtime configuration.
  `idle` means enabled but without confirmed delivery; it is not live-provider proof.
- Native session `active` is confirmed delivery; `fallback` means the native
  projection stopped and the separately reported evidence-comment channel remains
  the human surface. Neither is an unexplained pending delivery.
- Unconfirmed Logfire records conservatively inherit their fixed delivery attempt's
  worst batch state. Accepted records are counted separately, once. A request in
  flight is ambiguous until acknowledged; reads do not retry or change state.
- Error bodies, request payloads, tokens, and remote URLs are never included in
  this health object. Configuration says whether delivery is enabled, not whether
  a sender process is currently running.
- Logfire confirmations belong to the configured project and region. A standalone
  reader without that configuration cannot attribute delivery to a destination.

## Commands

`POST /v1/runs/{execution_id}/commands` requires an `Idempotency-Key` header of
1–200 letters, digits, `.`, `_`, `:`, or `-`.

```json
{
  "action": "transition",
  "expected_state": "Ready",
  "confirmed": false,
  "parameters": {
    "to_state": "Implementing",
    "owner": "builder-1",
    "outcome": null,
    "evidence": [],
    "feedback": []
  }
}
```

| Field | Required | Contract |
|---|---:|---|
| `action` | yes | `cancel`, `retry`, `approve`, `transition`, or `attention`. |
| `expected_state` | yes | Rejects a command from a stale phone view. |
| `confirmed` | policy-dependent | Required when the selected edge declares confirmation or targets a terminal. |
| `parameters` | no | Action-specific object; defaults to `{}`. |

### Action parameters

| Action | Parameters | Result |
|---|---|---|
| `cancel` | optional `reason` | Follows the unique eligible edge whose action is `cancel`; active work is closed with decision evidence. |
| `retry` | `owner` when the target is work; optional `reason` | Follows the unique eligible edge whose action is `retry`. |
| `approve` | `note` when feedback is required | Follows the unique eligible `approve` edge and applies its role and feedback policy. |
| `transition` | required `to_state`; optional `owner`, `outcome`, `evidence`, `feedback` | Applies one human-authorized workflow edge. Entering work requires `owner`; leaving work requires `outcome` and evidence. |
| `attention` | `attention_id`, `remedy`, and the visible `expected_attempt_id` when attempt-scoped | Applies one allowed `retry`, `release`, `retain`, `quarantine`, or `cancel` remedy. Release requires approver confirmation. |

Attention commands reject resolved requests, changed workflow state, replaced
attempts, stale internal fences, unauthorized roles, and reused command IDs
with different inputs. Exact command retries return the original receipt.

While a runner dispatch has no durable result, control exposes only cancellation.
After a crash, inspect the original errors/trace and `ambiguous-dispatch` attention;
do not force a new workflow transition or retry the launch. Cancellation abandons
the execution without inventing a runner result. It does not establish ownership
of or kill an unknown surviving process. Terminal workspace cleanup follows the
configured retention policy on the running lifecycle. Canceled dispatches with
uncertain side effects are quarantined even under `until_terminal`. Inspect and
stop any surviving process before an approver uses confirmed attention `release`;
`retain` and `quarantine` also resolve the cleanup decision without deleting work.
No process is killed by this release command. Retained or unsafe workspaces
are not silently deleted. No adapter currently proves safe continuation of an
ambiguous dispatch.

### Receipt

```json
{
  "api_version": "v1",
  "data": {
    "command_id": "mobile-01",
    "execution_id": "019...",
    "action": "cancel",
    "status": "completed",
    "authorization_decision": "allowed",
    "authorization_reason": "operator may issue cancel",
    "result": {
      "run": {},
      "reconciliation": {
        "pending": true,
        "desired_linear_status": "Canceled",
        "observed_linear_status": "Todo",
        "pending_projection_count": 2
      }
    },
    "error": null,
    "events": []
  }
}
```

Identical retries return the same receipt. Reusing the command ID with different
principal, target, or request inputs returns `409`.

## Status codes

| Status | Meaning |
|---:|---|
| 200 | Read or command completed; includes an existing idempotent receipt. |
| 400 | Invalid JSON, query, or command shape. |
| 401 | Host authentication failed. |
| 403 | Authenticated principal lacks the required role or confirmation. |
| 404 | Endpoint, run, or command does not exist. |
| 409 | Stale state, invalid workflow edge, failed command, or idempotency conflict. |
