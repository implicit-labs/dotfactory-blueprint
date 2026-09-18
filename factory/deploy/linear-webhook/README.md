# Linear Agent Session receipts

Receive signed `AgentSessionEvent` deliveries without launching agents or keeping
prompt text. This enables **proactive session testing**, not an inbound task queue.
No request causes a Linear API call, model run, status transition, or private-data
forwarding. `created` and `prompted` receipts remain `not_dispatched`.

## Deploy contract

| Setting | Requirement |
|---|---|
| Build | `docker build -f factory/deploy/linear-webhook/Dockerfile .` from repository root |
| HTTPS ingress | Stable private-service deployment; expose only `/webhooks/linear`, `/healthz`, `/readyz` |
| Process | One replica; `PORT` defaults to `8080` |
| Volume | Persistent local filesystem mounted at `/data`, writable by UID/GID `10001`; supports SQLite WAL and fsync |
| `LINEAR_WEBHOOK_SECRET` | Linear webhook signing secret, injected by the host secret store; **not** OAuth client secret or access token |
| `LINEAR_WEBHOOK_ORGANIZATION_ID` | Exact workspace organization ID |
| `LINEAR_WEBHOOK_OAUTH_CLIENT_ID` | Exact OAuth client ID from the app configuration |
| `LINEAR_WEBHOOK_APP_USER_ID` | Optional exact app actor ID; nested session actor must always match the signed top-level actor |
| `LINEAR_WEBHOOK_DATABASE` | Dedicated inbox path; default in image `/data/linear-agent-inbox.db`; never the factory ledger |
| Edge limits | Request body ≤256 KiB, header/body deadline ≤3 seconds, rate limits and TLS termination; no request-body logging |
| Readiness | `GET /readyz` confirms a SQLite write commits; `GET /healthz` only confirms process liveness |

Do not deploy on ephemeral serverless filesystems or share the SQLite file over a
network filesystem. A rolling replacement must preserve the volume and drain the
old process. `SIGTERM`/`SIGINT` stop accepting requests and drain in-flight handlers.
Signing secrets can rotate without changing the database binding; changing the
organization, OAuth client, or configured app actor requires a separate inbox.
Startup refuses SQLite 3.51.0–3.51.2, matching the repository's concurrent-WAL
safety gate. Verify the image's linked SQLite version before deploying.

## Enable and prove

### Render setup

Use `factory/deploy/linear-webhook/render.yaml` as the Blueprint path in the
reviewed repository. It defines one paid web service, a 1 GB persistent disk at
`/data`, `/readyz` health checks, and manual deployments. It does not provision
an agent worker or enable Linear webhooks. Select the reviewed branch/commit
for a canary; switch to main only after the source PR is merged.

Supply the three requested environment values through Render's secret settings.
The signing secret must match the Linear app's webhook configuration. Do not put
the app access token or personal Linear API key on this receipt-only service.

The proposed compute plan is `0.5c-512mb`. Confirm current pricing, persistent
disk availability and spending approval before creating the service.

Keep the image entrypoint enabled. Verify the mounted `/data` is writable by
UID/GID 10001; image-layer ownership alone does not prove mounted-disk access.
The optional root bootstrap prepares only the mount directory and drops all
privileges before serving. Do not solve a permission failure by running the
receiver itself as root. Verify a committed receipt survives a restart.

Disk-backed Render services use a single instance and stop the old instance
before replacement; expect a short deployment outage. Keep receipts separate
from the coordinator ledger and worker filesystem.

### Suspend and resume

Suspend compute when no Agent Session events are expected. Render leaves the
persistent receipt disk unchanged, but deliveries sent while suspended are not
accepted. Resume and check `/readyz` before using Agent Sessions again.

```bash
export DOTFACTORY_LISTENER_SERVICE_ID=srv-...
export RENDER_API_KEY=... # shell or secret manager only; never repository config
PYTHONPATH=factory/src python3 -m dotfactory listener status
PYTHONPATH=factory/src python3 -m dotfactory listener suspend
PYTHONPATH=factory/src python3 -m dotfactory listener resume
```

Each mutation prints an accepted control receipt. Run `status` afterward to
observe Render's current state; the command never reads or mutates the receipt
database. Render credentials are sent only to `https://api.render.com` and are
never included in output or errors.

References: [Blueprints](https://render.com/docs/blueprint-spec),
[persistent disks](https://render.com/docs/disks),
[pricing](https://render.com/pricing), and
[service controls](https://api-docs.render.com/reference/suspend-service-1).

### Live verification

1. Deploy with the correct volume and secret environment; verify `/readyz` is 200.
2. Set the private OAuth app webhook URL to `https://<host>/webhooks/linear` and
   enable only the Agent session events category. Use the matching signing secret.
3. From a separately authorized outbound integration, create a proactive native
   session and immediately provide an external URL/activity. This receiver does
   not include that publisher or satisfy its response obligation.
4. Inspect local receipts with
   `PYTHONPATH=factory/src python3 -m dotfactory.agent_webhooks receipts --database <inbox.db>`.
5. Restart the receiver against the same volume; resend the synthetic signed event
   with a fresh send timestamp and verify one event, a duplicate acknowledgement,
   and no inbound work launched.

Enabling the event category exposes Agent Session UI even for debugging. Do not
advertise mentions, delegation, follow-up prompts, or stop signals as supported.
No inbound prompt worker exists: user-created sessions may appear unresponsive.
Keep the integration in private canary use until an auditable dispatch/reply path
handles those inputs. If unsolicited events appear, disable the category and
inspect the receipt-only evidence; there is no prompt backlog to replay.

## Failure and storage contract

| Response | Meaning |
|---|---|
| 200 `recorded` / `duplicate` | Receipt transaction committed; **not** accepted agent work |
| 400 / 401 / 403 / 413 / 415 / 422 | Invalid payload, signature, freshness, binding, size, encoding, or event/action |
| 409 | Duplicate identity has conflicting content; a conflict hash was committed |
| 503 | Storage unavailable/busy or request capacity reached; not acknowledged |

The raw-body HMAC is checked before JSON parsing. Signed `webhookTimestamp` must
be within 60 seconds. The unsigned timestamp header cannot refresh a stale body.
Delivery IDs and session/action/activity identity deduplicate retries; content
hashing excludes only the signed send timestamp. A changed payload is a conflict,
not an overwrite. Prompt/context bodies, signatures, tokens and request URLs are
never stored or logged. Receipts retain IDs, hashes and timestamps on the private
volume with no automatic deletion. There is no public receipt endpoint.

Back up the live database with SQLite's online backup API or `.backup`, then verify
`PRAGMA quick_check` on the copy. Do not copy only the `.db` from a live WAL store.
Monitor disk space and `/readyz`; exhausted storage rejects delivery instead of
claiming success. Retention/export policy and alert routing remain operator-owned.

## Verify offline

`PYTHONPATH=factory/src python3 -m unittest discover -s factory/tests -p test_agent_webhooks.py`

Use a Python linked to safe SQLite (`python3 -c 'import sqlite3; print(sqlite3.sqlite_version)'`).
On 3.51.0–3.51.2, the suite explicitly skips receiver integration tests and still
checks startup refusal. A skipped integration test is not verification evidence.

Tests cover real localhost HTTP POSTs, bad signatures, stale/future timestamps,
wrong bindings, malformed/oversized bodies, duplicate headers, body deadlines,
conflicts, concurrent delivery, restart, privacy, and storage failure. They do not
prove a hosted HTTPS deployment or native Linear session success.

Protocol reference:
[official Linear webhook schema](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql).
