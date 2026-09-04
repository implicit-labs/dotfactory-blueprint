"""Readable, idempotent Linear evidence projected from durable run facts."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Mapping

from .ledger import LedgerError, SQLiteLedger
from .linear_api import LinearAPIError, LinearGraphQLClient


MAX_COMMENT_CHARS = 12000
NOT_FOUND_CODES = {"ENTITY_NOT_FOUND", "NOT_FOUND", "R404"}
DUPLICATE_CODES = {"ENTITY_ALREADY_EXISTS", "ALREADY_EXISTS", "CONFLICT"}
SECRET_PATTERNS = (
    re.compile(r"\blin_api_[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\b(?:authorization|cookie|password|secret|token|api[_-]?key)"
               r"\b\s*[:=]\s*[^\s,;]+"),
)


def _scrub(value: Any, limit: int = 1200) -> str:
    text = str(value or "").replace("\x00", "")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def _duration(started_at: str | None, ended_at: str | None) -> str:
    if not started_at:
        return "unknown"
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(
            (ended_at or datetime.now(start.tzinfo).isoformat()).replace("Z", "+00:00")
        )
    except ValueError:
        return "unknown"
    seconds = max(0, round((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _state_duration(
    started_at: str | None, completed_at: str | None, *, as_of: str | None,
) -> str:
    if not started_at or not (completed_at or as_of):
        return "time unavailable"
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat((completed_at or as_of or "").replace("Z", "+00:00"))
    except ValueError:
        return "time unavailable"
    seconds = max(0.0, (end - start).total_seconds())
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{round(seconds)}s"
    minutes, remaining = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {remaining}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _node_path(state_runs: list[dict[str, Any]]) -> str:
    if not state_runs:
        return "unavailable"
    maximum = 60
    omitted = max(0, len(state_runs) - maximum)
    visible = state_runs[-maximum:]
    nodes = []
    for item in visible:
        node = f"`{_scrub(item.get('state_id'), 120)}`"
        status = str(item.get("status") or "unknown")
        if status != "completed":
            node += f" ({_scrub(status, 40)})"
        nodes.append(node)
    prefix = f"… {omitted} earlier → " if omitted else ""
    return prefix + " → ".join(nodes)


def _node_details(
    state_runs: list[dict[str, Any]], state_token_usage: Mapping[str, Any],
    *, as_of: str | None,
) -> list[str]:
    maximum = 60
    omitted = max(0, len(state_runs) - maximum)
    lines = [f"_{omitted} earlier node runs omitted._"] if omitted else []
    for index, item in enumerate(state_runs[-maximum:], start=omitted + 1):
        state_id = _scrub(item.get("state_id"), 120)
        status = str(item.get("status") or "unknown")
        status_suffix = "" if status == "completed" else f" ({_scrub(status, 40)})"
        elapsed = _state_duration(
            item.get("started_at"), item.get("completed_at"), as_of=as_of,
        )
        usage = state_token_usage.get(str(item.get("id"))) or {}
        runner_runs = int(usage.get("runner_runs") or 0)
        measured = int(usage.get("measured_runner_runs") or 0)
        if not runner_runs:
            token_phrase = "no model call"
        elif not measured:
            token_phrase = "usage unavailable"
        else:
            token_phrase = f"{int(usage.get('total_tokens') or 0):,} tokens"
            if not usage.get("complete"):
                token_phrase += " (partial)"
        lines.append(
            f"{index}. `{state_id}`{status_suffix} — {elapsed} · {token_phrase}"
        )
    return lines


def readable_incidents(
    error_groups: list[dict[str, Any]], *, recovered: bool, terminal: bool = False,
) -> list[dict[str, Any]]:
    """Nest provider causes under one trusted failure per runner attempt."""
    incidents: dict[str, list[dict[str, Any]]] = {}
    for group in error_groups:
        occurrence = (group.get("occurrences") or [{}])[0]
        key = str(
            occurrence.get("runner_run_id")
            or occurrence.get("attempt_id")
            or occurrence.get("responsible_span_id")
            or group["fingerprint"]
        )
        incidents.setdefault(key, []).append(group)

    result = []
    for key, groups in incidents.items():
        def authority(item: Mapping[str, Any]) -> tuple[int, int]:
            trusted = str(item.get("trust_class", "")).startswith("trusted")
            classified = str(item.get("code", "")).endswith("RUNNER_FAILED")
            return (int(trusted) + int(classified), int(
                (item.get("occurrences") or [{}])[-1].get("trace_seq", 0)
            ))

        primary = max(groups, key=authority)
        causes = [item for item in groups if item is not primary]
        result.append({
            "key": key,
            "status": (
                "recovered" if recovered else "historical" if terminal else "active"
            ),
            "primary": primary,
            "causes": sorted(causes, key=lambda item: int(
                (item.get("occurrences") or [{}])[0].get("trace_seq", 0)
            )),
        })
    return sorted(result, key=lambda item: int(
        (item["primary"].get("occurrences") or [{}])[0].get("trace_seq", 0)
    ))


def render_linear_run_summary(
    snapshot: Mapping[str, Any], projection: Mapping[str, Any],
    history: Mapping[str, Any],
) -> tuple[str, str]:
    """Return bounded Markdown and its stable digest."""
    summary = projection["summary"]
    waterfall = projection["waterfall"]
    attempts = list(history.get("attempts") or [])
    state_runs = list(history.get("state_runs") or [])
    state_token_usage = history.get("state_token_usage") or {}
    attention = list(snapshot.get("attention_requests") or [])
    completed = str(snapshot["status"]) == "completed"
    incidents = readable_incidents(
        list(projection.get("error_groups") or []),
        recovered=completed and str(snapshot["current_state_id"]) == "Done",
        terminal=completed,
    )
    if completed:
        label = "Done"
    elif attention:
        label = "Needs attention"
    elif incidents:
        label = "Investigating"
    else:
        label = "Running"

    attempt_statuses = [str(item.get("status") or "unknown") for item in attempts]
    completed_attempts = sum(value == "completed" for value in attempt_statuses)
    failed_attempts = sum(value == "failed" for value in attempt_statuses)
    active_attempts = sum(value == "active" for value in attempt_statuses)
    incident_phrase = ""
    if incidents:
        incident_phrase = (
            f" {len(incidents)} incident{'s were' if len(incidents) != 1 else ' was'} "
            + ("recovered." if completed else "recorded.")
        )
    result_line = (
        f"{'Completed' if completed else 'Currently'} in "
        f"`{_scrub(snapshot['current_state_id'], 120)}` after "
        f"{len(attempts)} attempt{'s' if len(attempts) != 1 else ''}."
        + incident_phrase
    )
    workspace = snapshot.get("workspace")
    workspace_status = str(workspace.get("status")) if workspace else "not allocated"
    trace = summary["trace"]
    latest_fact_at = None
    for item in waterfall.get("items") or []:
        latest_fact_at = item.get("ended_at") or item.get("started_at") or latest_fact_at
    lines = [
        f"## Dotfactory run — {label}", "", result_line, "",
        f"- **Execution:** `{_scrub(summary['execution_key'], 160)}` · "
        f"`{_scrub(summary['execution_id'], 160)}`",
        f"- **Duration:** {_duration(snapshot.get('created_at'), snapshot.get('completed_at') or latest_fact_at)}",
        f"- **Attempts:** {len(attempts)} total · {completed_attempts} completed · "
        f"{failed_attempts} failed · {active_attempts} active",
        f"- **Trace:** {trace['record_count']} records · "
        f"{'complete' if trace['complete'] else 'incomplete'} · "
        f"`{_scrub(trace['trace_id'], 160)}`",
        f"- **Workspace:** `{_scrub(workspace_status, 120)}`",
        f"- **Attention:** {len(attention)} open",
        "", "### Nodes traversed", "",
        f"**Path:** {_node_path(state_runs)}", "",
        *_node_details(state_runs, state_token_usage, as_of=latest_fact_at),
    ]
    if incidents:
        incident_status = incidents[-1]["status"]
        heading = {
            "recovered": "### Recovered incident",
            "historical": "### Historical incident",
            "active": "### Active incident",
        }[incident_status]
        lines.extend(["", heading])
        for incident in incidents[-3:]:
            primary = incident["primary"]
            lines.extend([
                "",
                f"**{_scrub(primary['code'], 160)}** — {_scrub(primary['message'])}",
            ])
            for cause in incident["causes"][:3]:
                lines.append(
                    f"Cause: `{_scrub(cause['code'], 160)}` — {_scrub(cause['message'])}"
                )
            remedy = _scrub(primary.get("safe_remedy"))
            if not completed and remedy:
                lines.append(f"Next: {remedy}")

    links = [
        item for item in summary.get("links") or []
        if isinstance(item, dict) and str(item.get("url", "")).startswith("https://")
    ]
    lines.extend(["", "+++ Technical details", ""])
    for incident in incidents[-3:]:
        primary = incident["primary"]
        lines.append(
            f"- `{_scrub(primary['code'], 160)}` · {primary['occurrence_count']} occurrence(s) "
            f"· fingerprint `{_scrub(primary['fingerprint'], 160)}`"
        )
    for link in links[:8]:
        lines.append(f"- [{_scrub(link.get('kind', 'evidence'), 100)}]({_scrub(link['url'], 1000)})")
    if not links:
        lines.append("- Full waterfall link unavailable; use the trace ID above.")
    lines.extend(["", "This comment is updated in place from the local durable ledger.", "+++"])

    visible = "\n".join(lines).strip()
    if len(visible) > MAX_COMMENT_CHARS - 220:
        visible = visible[:MAX_COMMENT_CHARS - 260].rstrip() + "\n\n[Technical detail truncated]"
    content_digest = hashlib.sha256(visible.encode("utf-8")).hexdigest()
    marker = (
        f"<!-- dotfactory-run-evidence:v1 execution={_scrub(summary['execution_id'], 160)} "
        f"digest={content_digest} -->"
    )
    body = visible + "\n\n" + marker
    return body, hashlib.sha256(body.encode("utf-8")).hexdigest()


class LinearEvidenceWorker:
    """Converge one persisted comment ID without blind mutation retries."""

    def __init__(self, ledger: SQLiteLedger, client: LinearGraphQLClient) -> None:
        self.ledger = ledger
        self.client = client

    def _read(
        self, item: Mapping[str, Any],
    ) -> tuple[bool, dict[str, Any] | None]:
        try:
            return True, self.client.comment(str(item["comment_id"]))
        except LinearAPIError as error:
            if error.code.upper() in NOT_FOUND_CODES:
                return True, None
            self.ledger.mark_linear_evidence_error(
                str(item["execution_id"]), error.as_dict(),
                ambiguous=True, terminal=False,
            )
            return False, None

    def drain_one(self, item: Mapping[str, Any]) -> str:
        execution_id = str(item["execution_id"])
        status = str(item["status"])
        if status in {"sending", "ambiguous"}:
            known, remote = self._read(item)
            if not known:
                return "ambiguous"
            refreshed = self.ledger.linear_evidence(execution_id)
            if remote is not None:
                remote_digest = hashlib.sha256(str(remote["body"]).encode("utf-8")).hexdigest()
                if remote_digest == str(refreshed["desired_digest"]):
                    self.ledger.confirm_linear_evidence(
                        execution_id, remote_url=str(remote.get("url") or ""),
                    )
                    return "confirmed"
            elif refreshed.get("applied_digest"):
                self.ledger.mark_linear_evidence_error(
                    execution_id,
                    {"code": "comment_missing", "message": "owned Linear comment is missing",
                     "retryable": False, "ambiguous": False},
                    ambiguous=False, terminal=True,
                )
                return "failed"
            item = refreshed

        operation = "create" if not item.get("applied_digest") else "update"
        attempt = self.ledger.begin_linear_evidence_attempt(
            execution_id, operation=operation,
        )
        try:
            if operation == "create":
                remote = self.client.create_comment(
                    issue_id=str(item["issue_id"]), comment_id=str(item["comment_id"]),
                    body=str(item["desired_body"]),
                )
            else:
                remote = self.client.update_comment(
                    comment_id=str(item["comment_id"]), body=str(item["desired_body"]),
                )
        except LinearAPIError as error:
            ambiguous = error.ambiguous or error.code.upper() in DUPLICATE_CODES
            self.ledger.finish_linear_evidence_attempt(
                execution_id, int(attempt["attempt_number"]), error=error.as_dict(),
                ambiguous=ambiguous, terminal=not error.retryable and not ambiguous,
            )
            return "ambiguous" if ambiguous else "failed" if not error.retryable else "pending"
        try:
            self.ledger.finish_linear_evidence_attempt(
                execution_id, int(attempt["attempt_number"]), remote=remote,
            )
        except LedgerError as error:
            self.ledger.finish_linear_evidence_attempt(
                execution_id, int(attempt["attempt_number"]),
                error={
                    "code": "invalid_remote_comment", "message": str(error),
                    "retryable": False, "ambiguous": False,
                },
                terminal=True,
            )
            return "failed"
        return "confirmed"

    def drain(self, limit: int = 100) -> dict[str, int]:
        counts = {"confirmed": 0, "ambiguous": 0, "pending": 0, "failed": 0}
        for item in self.ledger.pending_linear_evidence(limit):
            counts[self.drain_one(item)] += 1
        return counts
