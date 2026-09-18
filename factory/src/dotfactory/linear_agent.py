"""Optional Linear Agent Session projection with a durable comment fallback."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping

from .ledger import LedgerError, SQLiteLedger, redact_payload
from .linear_api import LinearAPIError, LinearGraphQLClient
from .linear_evidence import readable_incidents, render_linear_run_summary, worker_location_label
from .observability import canonical_json


DUPLICATE_CODES = {"ENTITY_ALREADY_EXISTS", "ALREADY_EXISTS", "CONFLICT"}
SECRET_PATTERNS = (
    re.compile(r"\blin_api_[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?i)\b(?:authorization|cookie|password|secret|token|api[_-]?key)"
        r"\b\s*[:=]\s*[^\s,;]+"
    ),
)


def _safe(value: Any, limit: int = 1200) -> str:
    text = str(value or "").replace("\x00", "")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_urls(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("label") or "").strip()
        url = str(item.get("url") or "").strip()
        if label and url:
            result.append({"label": label, "url": url})
    return sorted(result, key=lambda item: (item["url"], item["label"]))


def build_agent_projection(
    snapshot: Mapping[str, Any], projection: Mapping[str, Any],
    history: Mapping[str, Any], *, marker_url: str,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Build sparse, immutable Agent Activities from durable facts."""
    if not marker_url.startswith("https://"):
        raise LedgerError("Linear agent session marker URL must use HTTPS")
    summary = projection["summary"]
    urls = [{"label": "Dotfactory run", "url": marker_url}]
    for item in summary.get("links") or []:
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "")
        if not url.startswith("https://") or url == marker_url:
            continue
        label = str(item.get("kind") or "evidence").replace("_", " ").title()
        urls.append({"label": _safe(label, 80), "url": _safe(url, 1000)})
    deduped = {item["url"]: item for item in urls}
    external_urls = sorted(deduped.values(), key=lambda item: item["url"])[:10]

    state_runs = list(history.get("state_runs") or [])
    execution_key = _safe(summary["execution_key"], 160)
    first_state = _safe(
        state_runs[0].get("state_id") if state_runs else snapshot["current_state_id"],
        120,
    )
    activities: list[dict[str, Any]] = [{
        "semantic_key": "start",
        "content": {
            "type": "thought",
            "body": f"Started `{execution_key}` in `{first_state}`.",
        },
    }]
    for item in state_runs[-60:]:
        state_id = _safe(item.get("state_id"), 120)
        status = _safe(item.get("status") or "unknown", 40)
        activities.append({
            "semantic_key": f"state:{item['id']}:{status}",
            "content": {
                "type": "thought",
                "body": f"Workflow reached `{state_id}` — {status}.",
            },
        })

    # A separate immutable activity can arrive after the state-start event.
    # Never infer physical placement from transport or mutable current config.
    for handoff in list(snapshot.get("worker_handoffs") or [])[-60:]:
        if not handoff.get("attempt_id"):
            continue
        activities.append({
            "semantic_key": f"worker-location:{handoff['attempt_id']}",
            "content": {
                "type": "thought",
                "body": (
                    f"{worker_location_label(handoff.get('location', 'unknown'))} · "
                    f"`{_safe(handoff.get('state'), 120)}` · "
                    f"worker `{_safe(handoff.get('worker'), 80)}`."
                ),
            },
        })

    completed = str(snapshot.get("status")) == "completed"
    incidents = readable_incidents(
        list(projection.get("error_groups") or []), recovered=completed,
        terminal=completed,
    )
    if not completed:
        for incident in incidents[-3:]:
            primary = incident["primary"]
            fingerprint = _safe(primary.get("fingerprint"), 160)
            activities.append({
                "semantic_key": f"incident:{fingerprint}",
                "content": {
                    "type": "error",
                    "body": (
                        f"**{_safe(primary.get('code'), 160)}** — "
                        f"{_safe(primary.get('message'))}\n\n"
                        f"Next: {_safe(primary.get('safe_remedy') or 'Inspect the run link.')}"
                    ),
                },
            })
        for attention in list(snapshot.get("attention_requests") or [])[-3:]:
            attention_id = _safe(attention.get("id") or _digest(attention), 160)
            reason = _safe(
                attention.get("safe_remedy") or attention.get("reason")
                or attention.get("kind") or "Operator input is required."
            )
            activities.append({
                "semantic_key": f"attention:{attention_id}",
                "content": {"type": "elicitation", "body": reason},
            })

    if completed:
        body, _body_digest = render_linear_run_summary(snapshot, projection, history)
        successful = str(snapshot.get("current_state_id")) == "Done"
        activities.append({
            "semantic_key": f"terminal:{snapshot.get('current_state_run_id')}",
            "content": {
                "type": "response" if successful else "error",
                "body": body[:10000],
            },
        })
    return external_urls, activities


class LinearAgentSessionWorker:
    """Converge one Agent Session per execution without blind create retries."""

    def __init__(self, ledger: SQLiteLedger, client: LinearGraphQLClient) -> None:
        self.ledger = ledger
        self.client = client

    @staticmethod
    def _session_dict(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["desired_external_urls"] = json.loads(
            item.pop("desired_external_urls_json")
        )
        error = item.pop("last_error_json")
        item["last_error"] = json.loads(error) if error else None
        return item

    @staticmethod
    def _activity_dict(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["content"] = json.loads(item.pop("content_json"))
        error = item.pop("last_error_json")
        item["last_error"] = json.loads(error) if error else None
        return item

    def session(self, execution_id: str) -> dict[str, Any]:
        row = self.ledger.connection.execute(
            "SELECT * FROM linear_agent_sessions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        if not row:
            raise LedgerError("Linear agent session not found")
        return self._session_dict(row)

    def _stage_session(
        self, execution_id: str, *, issue_id: str, marker_url: str,
        external_urls: list[dict[str, str]],
    ) -> dict[str, Any]:
        normalized = _normalized_urls(external_urls)
        if not issue_id.strip() or marker_url not in {
            item["url"] for item in normalized
        }:
            raise LedgerError("Linear agent session requires its issue and marker URL")
        if any(not item["url"].startswith("https://") for item in normalized):
            raise LedgerError("Linear agent session URLs must use HTTPS")
        desired_json = canonical_json(normalized)
        desired_digest = _digest(normalized)
        now = self.ledger.clock()
        with self.ledger.transaction() as db:
            prior = db.execute(
                "SELECT * FROM linear_agent_sessions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if not prior:
                db.execute(
                    "INSERT INTO linear_agent_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        execution_id, issue_id, marker_url, None, None, desired_json,
                        desired_digest, None, "pending", 0, None, None, now, now, None,
                    ),
                )
            else:
                if str(prior["issue_id"]) != issue_id or str(prior["marker_url"]) != marker_url:
                    raise LedgerError("Linear agent session identity changed")
                if (
                    str(prior["desired_digest"]) != desired_digest
                    and str(prior["status"]) not in {"fallback", "sending", "ambiguous"}
                ):
                    db.execute(
                        "UPDATE linear_agent_sessions SET desired_external_urls_json=?,"
                        "desired_digest=?,status='pending',last_error_json=NULL,"
                        "next_attempt_at=NULL,updated_at=? WHERE execution_id=?",
                        (desired_json, desired_digest, now, execution_id),
                    )
        return self.session(execution_id)

    def _begin_session_attempt(self, item: Mapping[str, Any], operation: str) -> int:
        now = self.ledger.clock()
        attempt_number = int(item["attempt_count"]) + 1
        with self.ledger.transaction() as db:
            db.execute(
                "INSERT INTO linear_agent_session_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), item["execution_id"], attempt_number, operation,
                    item["desired_digest"], "sending", None, None, now, None,
                ),
            )
            db.execute(
                "UPDATE linear_agent_sessions SET status='sending',attempt_count=?,"
                "updated_at=? WHERE execution_id=?",
                (attempt_number, now, item["execution_id"]),
            )
        return attempt_number

    def _confirm_session(
        self, item: Mapping[str, Any], remote: Mapping[str, Any],
        *, attempt_number: int | None = None, after_read: bool = False,
    ) -> None:
        session_id = str(remote.get("id") or "")
        if not session_id:
            raise LedgerError("Linear returned an invalid agent session")
        remote_urls = _normalized_urls(remote.get("externalLinks"))
        if _digest(remote_urls) != str(item["desired_digest"]):
            raise LedgerError("Linear agent session URLs did not converge")
        now = self.ledger.clock()
        response_hash = _digest(redact_payload(dict(remote)))
        with self.ledger.transaction() as db:
            if attempt_number is not None:
                db.execute(
                    "UPDATE linear_agent_session_attempts SET status=?,response_hash=?,"
                    "completed_at=? WHERE execution_id=? AND attempt_number=?",
                    (
                        "confirmed_after_read" if after_read else "confirmed",
                        response_hash, now, item["execution_id"], attempt_number,
                    ),
                )
            db.execute(
                "UPDATE linear_agent_sessions SET session_id=?,remote_url=?,"
                "applied_digest=desired_digest,status='active',last_error_json=NULL,"
                "next_attempt_at=NULL,updated_at=?,confirmed_at=? WHERE execution_id=?",
                (
                    session_id, str(remote.get("url") or ""), now, now,
                    item["execution_id"],
                ),
            )

    def _session_error(
        self, item: Mapping[str, Any], error: Mapping[str, Any], *,
        attempt_number: int | None = None, fallback: bool, ambiguous: bool = False,
    ) -> str:
        now = self.ledger.clock()
        status = "fallback" if fallback else "ambiguous" if ambiguous else "pending"
        retry_at = None if fallback else (
            datetime.fromisoformat(now.replace("Z", "+00:00"))
            + timedelta(seconds=30)
        ).isoformat()
        safe_error = canonical_json(redact_payload(dict(error)))
        with self.ledger.transaction() as db:
            if attempt_number is not None:
                db.execute(
                    "UPDATE linear_agent_session_attempts SET status=?,error_json=?,"
                    "completed_at=? WHERE execution_id=? AND attempt_number=?",
                    (status, safe_error, now, item["execution_id"], attempt_number),
                )
            db.execute(
                "UPDATE linear_agent_sessions SET status=?,last_error_json=?,"
                "next_attempt_at=?,updated_at=? WHERE execution_id=?",
                (status, safe_error, retry_at, now, item["execution_id"]),
            )
        return status

    def _reconcile_session(self, item: Mapping[str, Any]) -> str:
        attempt_number = int(item.get("attempt_count") or 0) or None
        try:
            if item.get("session_id"):
                remote = self.client.agent_session(str(item["session_id"]))
                matches = [remote] if remote else []
            else:
                sessions = self.client.issue_agent_sessions(str(item["issue_id"]))
                matches = [
                    remote for remote in sessions
                    if str(item["marker_url"]) in {
                        link["url"] for link in _normalized_urls(
                            remote.get("externalLinks")
                        )
                    }
                ]
        except LinearAPIError as error:
            return self._session_error(
                item, error.as_dict(), attempt_number=attempt_number,
                fallback=True, ambiguous=True,
            )
        if len(matches) == 1:
            try:
                self._confirm_session(
                    item, matches[0], attempt_number=attempt_number,
                    after_read=True,
                )
            except LedgerError as error:
                if item.get("session_id"):
                    self._session_error(
                        item,
                        {"code": "session_not_converged", "message": str(error)},
                        attempt_number=attempt_number, fallback=False,
                    )
                    return "pending"
                return self._session_error(
                    item, {"code": "session_mismatch", "message": str(error)},
                    attempt_number=attempt_number, fallback=True,
                )
            return "active"
        return self._session_error(
            item,
            {
                "code": "ambiguous_session_create",
                "message": "Agent session create could not be uniquely reconciled",
                "match_count": len(matches),
            },
            attempt_number=attempt_number, fallback=True, ambiguous=True,
        )

    def _drain_session(self, item: Mapping[str, Any]) -> str:
        if str(item["status"]) == "fallback":
            return "fallback"
        if str(item["status"]) in {"sending", "ambiguous"}:
            result = self._reconcile_session(item)
            if result != "pending":
                return result
            item = self.session(str(item["execution_id"]))
        if item.get("applied_digest") == item.get("desired_digest"):
            return "active"
        if item.get("next_attempt_at") and str(item["next_attempt_at"]) > self.ledger.clock():
            return "pending"
        operation = "update" if item.get("session_id") else "create"
        attempt = self._begin_session_attempt(item, operation)
        try:
            if operation == "create":
                remote = self.client.create_agent_session(
                    issue_id=str(item["issue_id"]),
                    external_urls=list(item["desired_external_urls"]),
                )
            else:
                remote = self.client.update_agent_session(
                    session_id=str(item["session_id"]),
                    external_urls=list(item["desired_external_urls"]),
                )
        except LinearAPIError as error:
            if error.ambiguous or error.code.upper() in DUPLICATE_CODES:
                refreshed = self.session(str(item["execution_id"]))
                return self._reconcile_session(refreshed)
            return self._session_error(
                item, error.as_dict(), attempt_number=attempt,
                fallback=not error.retryable,
            )
        try:
            self._confirm_session(item, remote, attempt_number=attempt)
        except LedgerError as error:
            return self._session_error(
                item, {"code": "invalid_agent_session", "message": str(error)},
                attempt_number=attempt, fallback=True,
            )
        return "active"

    def _stage_activity(
        self, execution_id: str, semantic_key: str, content: Mapping[str, Any],
    ) -> None:
        content_json = canonical_json(dict(content))
        content_digest = _digest(dict(content))
        now = self.ledger.clock()
        with self.ledger.transaction() as db:
            prior = db.execute(
                "SELECT content_digest FROM linear_agent_activities "
                "WHERE execution_id=? AND semantic_key=?",
                (execution_id, semantic_key),
            ).fetchone()
            if prior and str(prior["content_digest"]) != content_digest:
                if semantic_key.startswith("terminal:"):
                    return  # First terminal snapshot is immutable; session links can still update.
                raise LedgerError("Linear agent activity semantic content changed")
            if not prior:
                # The clock can repeat or move backwards. Preserve staging order
                # with strictly increasing logical timestamps, including after restart.
                prior_times = [datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                               for row in db.execute(
                                   "SELECT created_at FROM linear_agent_activities WHERE execution_id=?",
                                   (execution_id,),
                               )]
                staged_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
                if prior_times:
                    staged_at = max(staged_at, max(prior_times) + timedelta(microseconds=1))
                now = staged_at.isoformat(timespec="microseconds")
                db.execute(
                    "INSERT INTO linear_agent_activities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), execution_id, semantic_key, content_json,
                        content_digest, "pending", 0, None, None, None, None,
                        now, now, None,
                    ),
                )

    def _pending_activities(self, execution_id: str) -> list[dict[str, Any]]:
        rows = self.ledger.connection.execute(
            "SELECT * FROM linear_agent_activities WHERE execution_id=? "
            "AND status IN ('pending','sending','ambiguous') "
            "ORDER BY rowid",
            (execution_id,),
        ).fetchall()
        # Parse legacy Z/no-fraction timestamps too; textual ordering differs.
        rows.sort(key=lambda row: datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")))
        return [self._activity_dict(row) for row in rows[:100]]

    def _confirm_activity(
        self, item: Mapping[str, Any], remote: Mapping[str, Any], *,
        attempt_number: int, after_read: bool = False,
    ) -> None:
        if str(remote.get("id") or "") != str(item["id"]):
            raise LedgerError("Linear returned a different agent activity ID")
        expected_session = self.session(str(item["execution_id"]))["session_id"]
        if (remote.get("agentSession") or {}).get("id") != expected_session:
            raise LedgerError("Linear returned an activity from a different session")
        now = self.ledger.clock()
        response_hash = _digest(redact_payload(dict(remote)))
        with self.ledger.transaction() as db:
            db.execute(
                "UPDATE linear_agent_activity_attempts SET status=?,response_hash=?,"
                "completed_at=? WHERE activity_id=? AND attempt_number=?",
                (
                    "confirmed_after_read" if after_read else "confirmed",
                    response_hash, now, item["id"], attempt_number,
                ),
            )
            db.execute(
                "UPDATE linear_agent_activities SET status='confirmed',remote_id=?,"
                "response_hash=?,last_error_json=NULL,next_attempt_at=NULL,"
                "updated_at=?,confirmed_at=? WHERE id=?",
                (item["id"], response_hash, now, now, item["id"]),
            )

    def _activity_error(
        self, item: Mapping[str, Any], error: Mapping[str, Any], *,
        attempt_number: int, fallback: bool, ambiguous: bool = False,
    ) -> str:
        now = self.ledger.clock()
        status = "failed" if fallback else "ambiguous" if ambiguous else "pending"
        retry_at = None if fallback else (
            datetime.fromisoformat(now.replace("Z", "+00:00"))
            + timedelta(seconds=30)
        ).isoformat()
        safe_error = canonical_json(redact_payload(dict(error)))
        with self.ledger.transaction() as db:
            db.execute(
                "UPDATE linear_agent_activity_attempts SET status=?,error_json=?,"
                "completed_at=? WHERE activity_id=? AND attempt_number=?",
                (status, safe_error, now, item["id"], attempt_number),
            )
            db.execute(
                "UPDATE linear_agent_activities SET status=?,last_error_json=?,"
                "next_attempt_at=?,updated_at=? WHERE id=?",
                (status, safe_error, retry_at, now, item["id"]),
            )
        if fallback:
            session = self.session(str(item["execution_id"]))
            self._session_error(session, error, fallback=True)
        return "fallback" if fallback else status

    def _drain_activity(
        self, item: Mapping[str, Any], session_id: str,
    ) -> str:
        attempt_number = int(item["attempt_count"])
        if item.get("next_attempt_at") and str(item["next_attempt_at"]) > self.ledger.clock():
            return "pending"
        if str(item["status"]) in {"sending", "ambiguous"}:
            try:
                remote = self.client.agent_activity(str(item["id"]))
            except LinearAPIError as error:
                if not error.retryable:
                    return self._activity_error(
                        item, error.as_dict(), attempt_number=attempt_number,
                        fallback=True,
                    )
                return "ambiguous"
            if remote:
                self._confirm_activity(
                    item, remote, attempt_number=attempt_number, after_read=True,
                )
                return "confirmed"
        attempt_number += 1
        now = self.ledger.clock()
        with self.ledger.transaction() as db:
            db.execute(
                "INSERT INTO linear_agent_activity_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), item["id"], attempt_number, "sending",
                    item["content_digest"], None, None, now, None,
                ),
            )
            db.execute(
                "UPDATE linear_agent_activities SET status='sending',attempt_count=?,"
                "updated_at=? WHERE id=?",
                (attempt_number, now, item["id"]),
            )
        try:
            remote = self.client.create_agent_activity(
                session_id=session_id, activity_id=str(item["id"]),
                content=dict(item["content"]),
            )
        except LinearAPIError as error:
            ambiguous = error.ambiguous or error.code.upper() in DUPLICATE_CODES
            if ambiguous:
                try:
                    observed = self.client.agent_activity(str(item["id"]))
                except LinearAPIError:
                    observed = None
                if observed:
                    self._confirm_activity(
                        item, observed, attempt_number=attempt_number,
                        after_read=True,
                    )
                    return "confirmed"
            return self._activity_error(
                item, error.as_dict(), attempt_number=attempt_number,
                fallback=not error.retryable and not ambiguous, ambiguous=ambiguous,
            )
        self._confirm_activity(item, remote, attempt_number=attempt_number)
        return "confirmed"

    def sync(
        self, execution_id: str, *, issue_id: str, marker_url: str,
        external_urls: list[dict[str, str]], activities: list[dict[str, Any]],
    ) -> str:
        item = self._stage_session(
            execution_id, issue_id=issue_id, marker_url=marker_url,
            external_urls=external_urls,
        )
        result = self._drain_session(item)
        if result != "active":
            return result
        # Reconcile the frozen request before accepting newer desired links.
        item = self._stage_session(execution_id, issue_id=issue_id,
                                   marker_url=marker_url, external_urls=external_urls)
        result = self._drain_session(item)
        if result != "active":
            return result
        session = self.session(execution_id)
        for activity in activities:
            self._stage_activity(
                execution_id, str(activity["semantic_key"]),
                dict(activity["content"]),
            )
        for activity in self._pending_activities(execution_id):
            result = self._drain_activity(activity, str(session["session_id"]))
            if result != "confirmed":
                return result
        return "active"
