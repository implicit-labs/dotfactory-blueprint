# Factory control API v1

The host authenticates every request and supplies a `viewer`, `operator`, or
`approver` principal. The core does not parse credentials. Responses are JSON
with `Cache-Control: no-store`.

## Reads

| Endpoint | Query | Returns |
|---|---|---|
| `GET /v1/overview` | — | Factory identity, projects, run counts, active leases and allocations, open attention, and projection outbox counts. |
| `GET /v1/runs` | `project_key`, `status`, `state`, `limit` 1–100, `cursor` | Run summaries and `next_cursor`. |
| `GET /v1/runs/{execution_id}` | — | Intent, state, active attempt, workspace summary, preparation, allocations, attention, projection lag, and available actions. |
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
