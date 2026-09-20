# Verify native Linear Agent Sessions

## Configure

| Setting | Purpose |
|---|---|
| `projections.linear.token_env` | Existing issue/status/comment credential |
| `projections.linear.agent_token_env` | Separate app-actor OAuth token; defaults to `LINEAR_DOTFACTORY_AGENT_TOKEN` |
| `agent_sessions_enabled` | Opt in to native session projection |
| `agent_session_url_template` | HTTPS run URL with one `{execution_id}` placeholder |

Inject the app token through the named environment variable. Raw OAuth access
tokens receive the `Bearer` prefix; an existing prefix is retained. Missing agent
credentials leave classic comments available and produce an unavailable
`linear_agent` preflight. Never substitute a personal API key for the app token.

Use a private OAuth app with `actor=app`. If using client-credentials tokens,
request a token for the run through the host secret manager and keep it out of
config, receipts, SQLite and Git. Do not reuse another integration's app or rotate
its credentials. Follow Linear's actor-authorization and OAuth token-lifecycle
documentation for the current provider contract.

## Data sent to Linear

Enable this projection only for an issue whose audience may see the run's
workflow states, attention reasons, bounded error messages, terminal summary,
and configured evidence links. Agent activities are durable remote copies;
removing a local ledger does not remove already published activities.

The publisher redacts recognized credential patterns, but it is not a general
classifier for confidential content. Keep secrets and sensitive free text out of
operator reasons, error messages, and evidence URLs. Use links whose access
controls match the issue audience; do not put credentials in URL paths or query
strings. The receiver has a narrower contract: it stores receipt identities and
hashes, never inbound prompt bodies, and cannot dispatch work.

## Host receipts

Follow the [receiver deploy contract](deploy/linear-webhook/README.md): one
replica, persistent `/data` volume writable by UID 10001, stable HTTPS, and
runtime-injected webhook signing secret plus organization/app binding.
Build from the repository root using `factory/deploy/linear-webhook/Dockerfile`.
Check `/readyz` before enabling Agent session events on the private OAuth app.

A running receiver only records receipts. It does not dispatch prompts or publish
activities. Hosting cost approval and app configuration are separate from source
verification; no service is created by the canary command.

## Preflight

Use an existing issue in its owning project, a dedicated local canary database,
and two real HTTPS links. Supply UUIDs for identity checks. The default command
reads the app actor, organization and issue/project, and probes hosted readiness;
it writes only a local receipt with permissions `0600`.

```bash
PYTHONPATH=factory/src python3 -m dotfactory.linear_agent_canary \
  --issue-id "$CANARY_ISSUE_ID" --project-id "$CANARY_PROJECT_ID" \
  --organization-id "$CANARY_ORGANIZATION_ID" --app-user-id "$CANARY_APP_USER_ID" \
  --marker-url "$CANARY_RUN_URL" --updated-url "$CANARY_EVIDENCE_URL" \
  --webhook-url "$CANARY_RECEIVER_ORIGIN" \
  --database /private/tmp/linear-agent-canary.db \
  --receipt /private/tmp/linear-agent-canary.json
```

`LINEAR_DOTFACTORY_AGENT_TOKEN` must be present. A personal actor, identity mismatch, missing
token, or failed readiness probe blocks before remote writes.

## Execute and replay

Add `--execute` to the same command to create synthetic activities on the chosen
issue. It does not run a model, change issue status, or claim product delivery.

The first terminal response for a state run remains an immutable snapshot;
later evidence can update session links without another terminal response.

The canary checks:

1. One session with a unique run marker.
2. Start and progress activities, an external-link update, and a final response.
3. Reopening the ledger and replaying produces no additional durable writes.
4. Remote readback has exactly the expected activity IDs and both URLs, with
   session status `complete` and no duplicate session marker.

Reuse the **same database and arguments** after interruption or retryable failure.
The canary never automatically replaces an ambiguous session. Do not delete the
database to force a second create. A fallback or inconclusive readback is blocked,
not a successful native-session result.

## Live proof checklist

- Inspect the actual session in Linear; confirm readable sparse activities and links.
- Inspect the hosted receiver's local receipt store for the matching native event.
- Restart the hosted receiver with its persistent volume; resend a synthetic signed
  event and confirm one receipt plus a duplicate acknowledgement.
- Record image build and linked SQLite version. Offline tests do not prove the image.

The canary explicitly reports `webhook_receipt_verified: false`; its HTTPS probe
proves readiness only. Keep hosted receipt/restart evidence separate and record
both before treating a deployment as verified.
