"""Explicit live Agent Session proof; default mode only checks readiness."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .kernel import DurableKernel
from .ledger import LedgerError, SQLiteLedger
from .linear_agent import LinearAgentSessionWorker
from .linear_api import LinearAPIError, LinearGraphQLClient


def https_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("canary URLs must use HTTPS without credentials")
    return value


def check_identity(client: LinearGraphQLClient, *, issue_id: str, project_id: str,
                   organization_id: str, app_user_id: str) -> None:
    data = client.execute(
        "AgentCanaryIdentity",
        "query AgentCanaryIdentity($id:String!){viewer{id app organization{id}} "
        "issue(id:$id){id project{id}}}", {"id": issue_id},
    )
    viewer, issue = data.get("viewer") or {}, data.get("issue") or {}
    if (viewer.get("app") is not True or viewer.get("id") != app_user_id
            or (viewer.get("organization") or {}).get("id") != organization_id
            or issue.get("id") != issue_id
            or (issue.get("project") or {}).get("id") != project_id):
        raise ValueError("canary actor, organization, issue or project does not match")


def run_canary(database: Path, client: LinearGraphQLClient, *, issue_id: str,
               project_id: str, marker_url: str, updated_url: str) -> dict[str, Any]:
    """Publish synthetic activities only; never run agents or change issue state."""
    marker_url, updated_url = https_url(marker_url), https_url(updated_url)
    if marker_url == updated_url:
        raise ValueError("canary requires distinct marker and additional URLs")
    ledger = SQLiteLedger(database)
    try:
        ledger.configure_factory("linear-agent-canary")
        ledger.register_project("canary", display_name="Agent Session canary",
                                tracker_kind="linear", tracker_project_id=project_id)
        kernel = DurableKernel(ledger, Path(__file__).resolve().parents[2] / "workflows/default.dot")
        execution = kernel.begin("canary", issue_id,
                                 {"title": "Synthetic Agent Session canary", "linear_issue_id": issue_id},
                                 command_id="linear-agent-canary-v1")
        worker = LinearAgentSessionWorker(ledger, client)
        urls = [{"label": "Canary run", "url": marker_url}]
        activities = [{"semantic_key": "canary:start", "content": {
            "type": "thought", "body": "Synthetic factory canary started; no product work is running."}}]

        def sync() -> None:
            status = worker.sync(execution, issue_id=issue_id, marker_url=marker_url,
                                 external_urls=urls, activities=activities)
            if status != "active":
                raise ValueError("canary delivery is " + status + "; resume with the same database")

        # A completed local canary goes directly to final replay, without reverting links.
        finished = ledger.connection.execute(
            "SELECT 1 FROM linear_agent_activities WHERE execution_id=? AND semantic_key='canary:complete'",
            (execution,),
        ).fetchone()
        if not finished:
            sync()
        urls.append({"label": "Verification", "url": updated_url})
        activities.append({"semantic_key": "canary:progress", "content": {
            "type": "thought", "body": "Synthetic canary updated the session links."}})
        if not finished:
            sync()
        activities.append({"semantic_key": "canary:complete", "content": {
            "type": "response", "body": "Synthetic canary complete. Session links and durable activity delivery were exercised."}})
        sync()
        session_id = worker.session(execution)["session_id"]
        expected_ids = {row[0] for row in ledger.connection.execute(
            "SELECT id FROM linear_agent_activities WHERE execution_id=?", (execution,))}
    finally:
        ledger.close()

    # Reopen the actual ledger and replay through the real worker.
    ledger = SQLiteLedger(database)
    try:
        restarted = LinearAgentSessionWorker(ledger, client)
        before = ledger.connection.total_changes
        status = restarted.sync(execution, issue_id=issue_id, marker_url=marker_url,
                                external_urls=urls, activities=activities)
        if status != "active" or restarted.session(execution)["session_id"] != session_id:
            raise ValueError("restart did not preserve the native session")
        replay_writes = ledger.connection.total_changes - before
    finally:
        ledger.close()
    matches = [s for s in client.issue_agent_sessions(issue_id)
               if marker_url in {u.get("url") for u in s.get("externalLinks", [])}]
    if len(matches) != 1 or matches[0]["id"] != session_id:
        raise ValueError("canary did not resolve to exactly one remote session")
    remote = client.execute(
        "AgentCanaryReadback", "query AgentCanaryReadback($id:String!){agentSession(id:$id){"
        "id status externalLinks{label url} activities(first:100){nodes{id} "
        "pageInfo{hasNextPage}}}}", {"id": session_id},
    ).get("agentSession") or {}
    activity_page = remote.get("activities") or {}
    actual_ids = [a["id"] for a in activity_page.get("nodes", [])]
    actual_urls = {a["url"] for a in remote.get("externalLinks", [])}
    if (remote.get("status") != "complete" or actual_urls != {marker_url, updated_url}
            or set(actual_ids) != expected_ids or len(actual_ids) != len(expected_ids)
            or activity_page.get("pageInfo", {}).get("hasNextPage") is not False
            or replay_writes):
        raise ValueError("native terminal state, URLs, activity readback or replay did not verify")
    return {"status": "passed", "execution_id": execution, "session_id": session_id,
            "session_url": matches[0].get("url"), "activity_count": len(expected_ids),
            "remote_status": remote["status"], "restart_replay_writes": replay_writes,
            "webhook_receipt_verified": False,
            "scope": "native outbound projection; hosted receipt and restart proof remain separate"}


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="write synthetic activities to the specified issue")
    for name in ("issue-id", "project-id", "organization-id", "app-user-id", "marker-url", "updated-url", "webhook-url"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--database", required=True, type=Path, help="dedicated canary ledger; reuse on restart")
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(arguments)
    report: dict[str, Any] = {"status": "blocked", "executed": False}
    started = False
    try:
        token = os.environ.get("LINEAR_AGENT_TOKEN", "").strip()
        if not token:
            raise ValueError("LINEAR_AGENT_TOKEN is missing; an app-actor OAuth token is required")
        authorization = token if token.startswith("Bearer ") else "Bearer " + token
        client = LinearGraphQLClient(authorization)
        https_url(args.marker_url)
        https_url(args.updated_url)
        check_identity(client, issue_id=args.issue_id, project_id=args.project_id,
                       organization_id=args.organization_id, app_user_id=args.app_user_id)
        ready = https_url(args.webhook_url).rstrip("/") + "/readyz"
        with urllib.request.urlopen(ready, timeout=10) as response:
            if response.status != 200 or json.loads(response.read(4096)) != {
                "status": "ready", "mode": "receipt_only",
            }:
                raise ValueError("hosted receiver is not ready")
        report = {"status": "ready", "executed": False, "hosted_readiness": True}
        if args.execute:
            started = True
            report = run_canary(args.database, client, issue_id=args.issue_id,
                                project_id=args.project_id, marker_url=args.marker_url,
                                updated_url=args.updated_url)
            report.update({"executed": True, "hosted_readiness": True})
    except (ValueError, LinearAPIError, LedgerError, OSError) as error:
        # Never serialize provider response bodies, request headers or tokens.
        report = {"status": "blocked", "executed": started,
                  "reason": str(error) if isinstance(error, ValueError) else type(error).__name__}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=args.receipt.parent, prefix=".agent-canary-")
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, args.receipt)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"ready", "passed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
