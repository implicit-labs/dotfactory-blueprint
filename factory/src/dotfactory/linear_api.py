"""Live Linear edge with bounded GraphQL and convergence semantics."""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .linear_reconciliation import (
    LinearObservationV1, LinearReconciler, LinearStatusBindingV1,
    content_hash, poll_observation_key, webhook_observation_key,
)


Transport = Callable[[str, Mapping[str, str], bytes, float], dict[str, Any]]
RETRYABLE_CODES = {"INTERNAL_SERVER_ERROR", "RATELIMITED", "RATE_LIMITED", "TIMEOUT"}
NOT_FOUND_CODES = {"ENTITY_NOT_FOUND", "NOT_FOUND", "R404"}


class LinearAPIError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, retryable: bool,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "message": str(self),
            "retryable": self.retryable, "ambiguous": self.ambiguous,
        }


def _default_transport(
    endpoint: str, headers: Mapping[str, str], body: bytes, timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint, data=body, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        retryable = error.code == 429 or error.code >= 500
        raise LinearAPIError(
            f"http_{error.code}", "Linear GraphQL request failed",
            retryable=retryable,
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LinearAPIError(
            "transport_error", "Linear GraphQL result is unknown",
            retryable=True, ambiguous=True,
        ) from error
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LinearAPIError(
            "invalid_response", "Linear returned invalid JSON",
            retryable=False,
        ) from error
    if not isinstance(decoded, dict):
        raise LinearAPIError(
            "invalid_response", "Linear returned a non-object response",
            retryable=False,
        )
    return decoded


class LinearGraphQLClient:
    def __init__(
        self, authorization: str, *, endpoint: str = "https://api.linear.app/graphql",
        timeout_seconds: int = 15, transport: Transport | None = None,
    ) -> None:
        if not authorization.strip():
            raise LinearAPIError("missing_auth", "Linear authorization is missing", retryable=False)
        if timeout_seconds < 1 or timeout_seconds > 60:
            raise LinearAPIError(
                "invalid_timeout", "Linear timeout must be between 1 and 60 seconds",
                retryable=False,
            )
        self.authorization = authorization
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _default_transport

    def execute(
        self, operation: str, query: str, variables: dict[str, Any],
    ) -> dict[str, Any]:
        body = json.dumps(
            {"operationName": operation, "query": query, "variables": variables},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        response = self.transport(
            self.endpoint,
            {
                "Authorization": self.authorization,
                "Content-Type": "application/json",
                "User-Agent": "dotfactory-linear/1",
            },
            body, float(self.timeout_seconds),
        )
        errors = response.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) else {}
            extensions = first.get("extensions", {}) if isinstance(first, dict) else {}
            code = str(extensions.get("code", "graphql_error"))
            raise LinearAPIError(
                code, "Linear GraphQL operation failed",
                retryable=code.upper() in RETRYABLE_CODES,
            )
        data = response.get("data")
        if not isinstance(data, dict):
            raise LinearAPIError(
                "missing_data", "Linear GraphQL response has no data",
                retryable=False,
            )
        return data

    def preflight(
        self, *, project_key: str, workflow_digest: str, team_id: str,
        project_id: str, required_status_names: list[str],
    ) -> list[LinearStatusBindingV1]:
        data = self.execute(
            "FactoryLinearPreflight",
            "query FactoryLinearPreflight($teamId:String!,$projectId:String!){"
            "team(id:$teamId){id states{nodes{id name type}}}project(id:$projectId){id}}",
            {"teamId": team_id, "projectId": project_id},
        )
        team = data.get("team")
        project = data.get("project")
        if not isinstance(team, dict) or team.get("id") != team_id:
            raise LinearAPIError("team_not_found", "configured Linear team was not found", retryable=False)
        if not isinstance(project, dict) or project.get("id") != project_id:
            raise LinearAPIError(
                "project_not_found", "configured Linear project was not found", retryable=False
            )
        nodes = team.get("states", {}).get("nodes", [])
        by_name: dict[str, list[dict[str, Any]]] = {}
        for node in nodes if isinstance(nodes, list) else []:
            if isinstance(node, dict):
                by_name.setdefault(str(node.get("name", "")), []).append(node)
        bindings = []
        for name in required_status_names:
            matches = by_name.get(name, [])
            if len(matches) != 1:
                raise LinearAPIError(
                    "status_binding_invalid",
                    f"Linear status {name} must resolve exactly once in the configured team",
                    retryable=False,
                )
            node = matches[0]
            bindings.append(LinearStatusBindingV1(
                project_key=project_key, workflow_digest=workflow_digest,
                team_id=team_id, status_id=str(node["id"]), status_name=name,
                status_type=str(node["type"]),
            ))
        return bindings

    def issue(self, issue_id: str) -> dict[str, Any]:
        data = self.execute(
            "FactoryIssue",
            "query FactoryIssue($id:String!){issue(id:$id){id identifier title description url updatedAt "
            "state{id name} team{id} project{id}}}",
            {"id": issue_id},
        )
        issue = data.get("issue")
        if not isinstance(issue, dict):
            raise LinearAPIError("issue_not_found", "Linear issue was not found", retryable=False)
        return issue

    def comment(self, comment_id: str) -> dict[str, Any] | None:
        try:
            data = self.execute(
                "FactoryComment",
                "query FactoryComment($id:String!){comment(id:$id){id body url issue{id}}}",
                {"id": comment_id},
            )
        except LinearAPIError as error:
            if error.code.upper() in NOT_FOUND_CODES:
                return None
            raise
        comment = data.get("comment")
        if comment is None:
            return None
        if not isinstance(comment, dict) or not str(comment.get("id", "")).strip():
            raise LinearAPIError(
                "invalid_comment", "Linear returned an invalid comment", retryable=False
            )
        return dict(comment)

    def create_comment(
        self, *, issue_id: str, comment_id: str, body: str,
    ) -> dict[str, Any]:
        return self._mutate_comment(
            operation="FactoryCommentCreate", field="commentCreate",
            query=(
                "mutation FactoryCommentCreate($input:CommentCreateInput!){"
                "commentCreate(input:$input){success comment{id body url issue{id}}}}"
            ),
            variables={"input": {
                "id": comment_id, "issueId": issue_id, "body": body,
                "doNotSubscribeToIssue": True,
            }},
        )

    def update_comment(self, *, comment_id: str, body: str) -> dict[str, Any]:
        return self._mutate_comment(
            operation="FactoryCommentUpdate", field="commentUpdate",
            query=(
                "mutation FactoryCommentUpdate($id:String!,$input:CommentUpdateInput!){"
                "commentUpdate(id:$id,input:$input){success comment{id body url issue{id}}}}"
            ),
            variables={"id": comment_id, "input": {"body": body}},
        )

    @staticmethod
    def _agent_session_value(data: dict[str, Any], field: str) -> dict[str, Any]:
        result = data.get(field)
        session = result.get("agentSession") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict) or result.get("success") is not True
            or not isinstance(session, dict) or not str(session.get("id", "")).strip()
        ):
            raise LinearAPIError(
                "invalid_agent_session", "Linear returned no agent session",
                retryable=False,
            )
        return dict(session)

    def agent_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            data = self.execute(
                "FactoryAgentSession",
                "query FactoryAgentSession($id:String!){agentSession(id:$id){"
                "id url status issue{id} externalLinks{label url}}}",
                {"id": session_id},
            )
        except LinearAPIError as error:
            if error.code.upper() in NOT_FOUND_CODES:
                return None
            raise
        session = data.get("agentSession")
        return dict(session) if isinstance(session, dict) else None

    def issue_agent_sessions(self, issue_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor = None
        seen = set()
        for _page in range(20):
            data = self.execute(
                "FactoryIssueAgentSessions",
                "query FactoryIssueAgentSessions($id:String!,$after:String){issue(id:$id){"
                "agentSessions(first:50,after:$after){nodes{id url status issue{id} "
                "externalLinks{label url}} pageInfo{hasNextPage endCursor}}}}",
                {"id": issue_id, "after": cursor},
            )
            issue = data.get("issue")
            sessions = issue.get("agentSessions") if isinstance(issue, dict) else None
            nodes = sessions.get("nodes") if isinstance(sessions, dict) else None
            page = sessions.get("pageInfo") if isinstance(sessions, dict) else None
            if (not isinstance(nodes, list) or any(not isinstance(item, dict) for item in nodes)
                    or not isinstance(page, dict) or not isinstance(page.get("hasNextPage"), bool)):
                raise LinearAPIError("invalid_agent_sessions", "Linear returned invalid agent sessions", retryable=False)
            result.extend(dict(item) for item in nodes)
            if not page["hasNextPage"]:
                return result
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                break
            seen.add(cursor)
        raise LinearAPIError("agent_sessions_incomplete", "Linear session listing could not be completed", retryable=False)

    def planning_session(self, session_id: str) -> dict[str, Any]:
        """Read all prompt activities before acting; partial pages cannot authorize approval."""
        result = None
        activities = []
        cursor = None
        seen = set()
        for _ in range(20):
            data = self.execute("FactoryPlanningSession",
                "query FactoryPlanningSession($id:String!,$after:String){organization{id} "
                "agentSession(id:$id){id appUser{id} issue{id project{id} team{id}} "
                "activities(first:100,after:$after){nodes{id createdAt queued signal user{id} "
                "content{... on AgentActivityPromptContent{type body}}} "
                "pageInfo{hasNextPage endCursor}}}}", {"id": session_id, "after": cursor})
            session = data.get('agentSession')
            connection = session.get('activities') if isinstance(session, dict) else None
            if not isinstance(connection, dict) or not isinstance(connection.get('nodes'), list):
                raise LinearAPIError('invalid_planning_session', 'Linear returned invalid planning activities', retryable=False)
            page = connection.get('pageInfo', {})
            if not isinstance(page, dict) or type(page.get('hasNextPage')) is not bool:
                raise LinearAPIError('incomplete_planning_session', 'Linear activity pagination is incomplete', retryable=False)
            identity = {k: v for k, v in session.items() if k != 'activities'}
            identity['organization'] = data.get('organization')
            if result is not None and result != identity:
                raise LinearAPIError('changed_planning_session', 'Linear session identity changed during pagination', retryable=True)
            result = identity
            for node in connection['nodes']:
                if not isinstance(node, dict) or not isinstance(node.get('id'), str) or not isinstance(node.get('createdAt'), str):
                    raise LinearAPIError('invalid_planning_activity', 'Linear returned an invalid activity', retryable=False)
                activities.append(node)
            if not page['hasNextPage']:
                return dict(result, activities=activities)
            cursor = page.get('endCursor')
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                break
            seen.add(cursor)
        raise LinearAPIError('incomplete_planning_session', 'Linear activity pagination could not be completed', retryable=False)

    def create_agent_session(
        self, *, issue_id: str, external_urls: list[dict[str, str]],
    ) -> dict[str, Any]:
        try:
            data = self.execute(
                "FactoryAgentSessionCreate",
                "mutation FactoryAgentSessionCreate($input:AgentSessionCreateOnIssue!){"
                "agentSessionCreateOnIssue(input:$input){success agentSession{"
                "id url status issue{id} externalLinks{label url}}}}",
                {"input": {"issueId": issue_id, "externalUrls": external_urls}},
            )
        except LinearAPIError as error:
            if error.retryable and error.code.upper() not in {
                "RATELIMITED", "RATE_LIMITED", "HTTP_429",
            }:
                raise LinearAPIError(
                    error.code, "Linear agent session result is unknown",
                    retryable=True, ambiguous=True,
                ) from error
            raise
        return self._agent_session_value(data, "agentSessionCreateOnIssue")

    def update_agent_session(
        self, *, session_id: str, external_urls: list[dict[str, str]],
    ) -> dict[str, Any]:
        try:
            data = self.execute(
                "FactoryAgentSessionUpdate",
                "mutation FactoryAgentSessionUpdate($id:String!,$input:AgentSessionUpdateInput!){"
                "agentSessionUpdate(id:$id,input:$input){success agentSession{"
                "id url status issue{id} externalLinks{label url}}}}",
                {"id": session_id, "input": {"externalUrls": external_urls}},
            )
        except LinearAPIError as error:
            if error.retryable and error.code.upper() not in {
                "RATELIMITED", "RATE_LIMITED", "HTTP_429",
            }:
                raise LinearAPIError(
                    error.code, "Linear agent session update result is unknown",
                    retryable=True, ambiguous=True,
                ) from error
            raise
        return self._agent_session_value(data, "agentSessionUpdate")

    def agent_activity(self, activity_id: str) -> dict[str, Any] | None:
        try:
            data = self.execute(
                "FactoryAgentActivity",
                "query FactoryAgentActivity($id:String!){agentActivity(id:$id){"
                "id agentSession{id}}}",
                {"id": activity_id},
            )
        except LinearAPIError as error:
            if error.code.upper() in NOT_FOUND_CODES:
                return None
            raise
        activity = data.get("agentActivity")
        return dict(activity) if isinstance(activity, dict) else None

    def create_agent_activity(
        self, *, session_id: str, activity_id: str, content: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            data = self.execute(
                "FactoryAgentActivityCreate",
                "mutation FactoryAgentActivityCreate($input:AgentActivityCreateInput!){"
                "agentActivityCreate(input:$input){success agentActivity{"
                "id agentSession{id}}}}",
                {"input": {
                    "id": activity_id, "agentSessionId": session_id,
                    "content": content,
                }},
            )
        except LinearAPIError as error:
            if error.retryable and error.code.upper() not in {
                "RATELIMITED", "RATE_LIMITED", "HTTP_429",
            }:
                raise LinearAPIError(
                    error.code, "Linear agent activity result is unknown",
                    retryable=True, ambiguous=True,
                ) from error
            raise
        result = data.get("agentActivityCreate")
        activity = result.get("agentActivity") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict) or result.get("success") is not True
            or not isinstance(activity, dict)
            or str(activity.get("id", "")) != activity_id
        ):
            raise LinearAPIError(
                "invalid_agent_activity", "Linear returned no matching agent activity",
                retryable=False,
            )
        return dict(activity)

    def _mutate_comment(
        self, *, operation: str, field: str, query: str,
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            data = self.execute(operation, query, variables)
        except LinearAPIError as error:
            if error.retryable and error.code.upper() not in {
                "RATELIMITED", "RATE_LIMITED", "HTTP_429",
            }:
                raise LinearAPIError(
                    error.code, "Linear comment mutation result is unknown",
                    retryable=True, ambiguous=True,
                ) from error
            raise
        result = data.get(field)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise LinearAPIError(
                "mutation_rejected", "Linear rejected the comment mutation",
                retryable=False,
            )
        comment = result.get("comment")
        if not isinstance(comment, dict) or not str(comment.get("id", "")).strip():
            raise LinearAPIError(
                "missing_mutation_comment", "Linear mutation returned no comment",
                retryable=False,
            )
        return dict(comment)

    def eligible_issues(
        self, *, project_id: str, status_names: list[str], limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not project_id.strip():
            raise LinearAPIError(
                "missing_project", "Linear project ID is missing", retryable=False
            )
        if not status_names:
            raise LinearAPIError(
                "missing_statuses", "eligible Linear statuses are missing",
                retryable=False,
            )
        if limit < 1 or limit > 100:
            raise LinearAPIError(
                "invalid_limit", "Linear issue limit must be between 1 and 100",
                retryable=False,
            )
        data = self.execute(
            "FactoryEligibleIssues",
            "query FactoryEligibleIssues($projectId:ID!,$statusNames:[String!]!,$first:Int!){"
            "issues(first:$first,filter:{project:{id:{eq:$projectId}},"
            "state:{name:{in:$statusNames}}}){nodes{id identifier title url createdAt "
            "updatedAt state{id name} team{id} project{id}}}}",
            {"projectId": project_id, "statusNames": sorted(set(status_names)),
             "first": limit},
        )
        issues = data.get("issues")
        nodes = issues.get("nodes") if isinstance(issues, dict) else None
        if not isinstance(nodes, list) or any(
            not isinstance(item, dict) for item in nodes
        ):
            raise LinearAPIError(
                "invalid_issue_list", "Linear returned an invalid issue list",
                retryable=False,
            )
        return sorted(
            (dict(item) for item in nodes),
            key=lambda item: (str(item.get("createdAt", "")),
                              str(item.get("identifier", ""))),
        )

    _QUEUE_FIELDS = (
        "id identifier title description url createdAt updatedAt priority "
        "state{id name} team{id} project{id} "
        "labels(first:100){nodes{name} pageInfo{hasNextPage}} "
        "inverseRelations(first:100){nodes{type issue{id state{type}}} pageInfo{hasNextPage}}"
    )

    def queue_issue(self, identifier: str) -> dict[str, Any]:
        data = self.execute(
            "FactoryQueueIssue",
            "query FactoryQueueIssue($id:String!){issue(id:$id){" + self._QUEUE_FIELDS + "}}",
            {"id": identifier},
        )
        issue = data.get("issue")
        if not isinstance(issue, dict) or issue.get("identifier") != identifier:
            raise LinearAPIError("invalid_queue_issue", "Linear returned no matching queue issue", retryable=False)
        return issue

    def queue_issues(self, *, project_id: str, status_names: list[str],
                     should_continue: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
        if not project_id or not status_names:
            raise LinearAPIError("invalid_queue_scope", "queue requires project and pickup statuses", retryable=False)
        cursor = None
        seen_cursors = set()
        by_id = {}
        for _ in range(100):
            if should_continue and not should_continue():
                return []  # Never admit from a partially scanned queue.
            data = self.execute(
                "FactoryQueueIssues",
                "query FactoryQueueIssues($projectId:ID!,$statusNames:[String!]!,$after:String){"
                "issues(first:100,after:$after,filter:{project:{id:{eq:$projectId}},"
                "state:{name:{in:$statusNames}}}){nodes{" + self._QUEUE_FIELDS
                + "} pageInfo{hasNextPage endCursor}}}",
                {"projectId": project_id, "statusNames": sorted(set(status_names)), "after": cursor},
            )
            connection = data.get("issues")
            if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
                raise LinearAPIError("invalid_queue_page", "Linear returned an invalid queue page", retryable=False)
            for issue in connection["nodes"]:
                if not isinstance(issue, dict) or not issue.get("id"):
                    raise LinearAPIError("invalid_queue_page", "Linear returned an invalid queue issue", retryable=False)
                by_id[str(issue["id"])] = issue
            page = connection.get("pageInfo", {})
            if page.get("hasNextPage") is False:
                return list(by_id.values())
            cursor = page.get("endCursor")
            if page.get("hasNextPage") is not True or not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise LinearAPIError("invalid_queue_cursor", "Linear queue cursor did not advance", retryable=False)
            seen_cursors.add(cursor)
        raise LinearAPIError("queue_page_limit", "queue exceeds 100 pages; narrow its project scope", retryable=False)

    def viewer_id(self) -> str:
        data = self.execute(
            "FactoryViewer", "query FactoryViewer{viewer{id}}", {}
        )
        viewer = data.get("viewer")
        if not isinstance(viewer, dict) or not str(viewer.get("id", "")).strip():
            raise LinearAPIError(
                "viewer_not_found", "Linear authorization has no viewer identity",
                retryable=False,
            )
        return str(viewer["id"])

    def update_issue_status(self, issue_id: str, status_id: str) -> dict[str, Any]:
        try:
            data = self.execute(
                "FactoryIssueStatusUpdate",
                "mutation FactoryIssueStatusUpdate($id:String!,$statusId:String!){"
                "issueUpdate(id:$id,input:{stateId:$statusId}){success issue{id identifier "
                "updatedAt state{id name}}}}",
                {"id": issue_id, "statusId": status_id},
            )
        except LinearAPIError as error:
            if error.retryable and error.code.upper() not in {"RATELIMITED", "RATE_LIMITED", "HTTP_429"}:
                raise LinearAPIError(
                    error.code, "Linear mutation result is unknown",
                    retryable=True, ambiguous=True,
                ) from error
            raise
        result = data.get("issueUpdate")
        if not isinstance(result, dict) or result.get("success") is not True:
            raise LinearAPIError(
                "mutation_rejected", "Linear rejected the status mutation", retryable=False
            )
        issue = result.get("issue")
        if not isinstance(issue, dict):
            raise LinearAPIError(
                "missing_mutation_issue", "Linear mutation returned no issue", retryable=False
            )
        return issue


@dataclass(frozen=True)
class LinearWebhookVerifier:
    secret: str
    maximum_age_seconds: int = 60

    def __post_init__(self) -> None:
        if self.maximum_age_seconds < 1 or self.maximum_age_seconds > 300:
            raise LinearAPIError(
                "invalid_webhook_window",
                "webhook age window must be between 1 and 300 seconds",
                retryable=False,
            )

    def verify(
        self, body: bytes, *, signature: str, now: datetime | None = None,
    ) -> dict[str, Any]:
        if not self.secret:
            raise LinearAPIError("missing_webhook_secret", "webhook secret is missing", retryable=False)
        expected = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise LinearAPIError("invalid_signature", "webhook signature is invalid", retryable=False)
        try:
            payload = json.loads(body.decode("utf-8"))
            timestamp_ms = int(payload["webhookTimestamp"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise LinearAPIError(
                "invalid_webhook", "webhook timestamp is missing or invalid", retryable=False
            ) from error
        current = now or datetime.now(timezone.utc)
        observed = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        if abs((current - observed).total_seconds()) > self.maximum_age_seconds:
            raise LinearAPIError("stale_webhook", "webhook timestamp is outside the allowed window", retryable=False)
        if not isinstance(payload, dict):
            raise LinearAPIError("invalid_webhook", "webhook payload must be an object", retryable=False)
        return payload


class LinearConvergenceWorker:
    def __init__(
        self, ledger: Any, kernel: Any, client: LinearGraphQLClient,
        *, self_actor_id: str | None = None,
    ) -> None:
        self.ledger = ledger
        self.kernel = kernel
        self.client = client
        self.reconciler = LinearReconciler(ledger, kernel)
        self.self_actor_id = self_actor_id

    def _validate_issue_scope(
        self, execution_id: str, issue: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.ledger.current(execution_id)
        if str(issue.get("identifier", "")) != current["work_item_identifier"]:
            raise LinearAPIError(
                "wrong_issue", "Linear issue does not match the execution work item",
                retryable=False,
            )
        project = self.ledger.connection.execute(
            "SELECT tracker_project_id FROM projects WHERE project_key=?",
            (current["project_key"],),
        ).fetchone()
        issue_project = issue.get("project")
        if (
            not isinstance(issue_project, dict) or not project
            or str(issue_project.get("id", "")) != str(project["tracker_project_id"])
        ):
            raise LinearAPIError(
                "wrong_project", "Linear issue belongs to a different project",
                retryable=False,
            )
        binding = self.ledger.connection.execute(
            "SELECT team_id FROM linear_status_bindings WHERE project_key=? "
            "AND workflow_digest=? ORDER BY team_id LIMIT 1",
            (current["project_key"], current["workflow_digest"]),
        ).fetchone()
        issue_team = issue.get("team")
        if binding and (
            not isinstance(issue_team, dict)
            or str(issue_team.get("id", "")) != str(binding["team_id"])
        ):
            raise LinearAPIError(
                "wrong_team", "Linear issue belongs to a different team",
                retryable=False,
            )
        return current

    def preflight(
        self, *, execution_id: str, team_id: str, project_id: str,
    ) -> list[dict[str, Any]]:
        current = self.ledger.current(execution_id)
        _, states, _ = self.kernel.graph_for_execution(execution_id)
        return self._preflight_bindings(
            project_key=current["project_key"],
            workflow_digest=current["workflow_digest"], states=states,
            team_id=team_id, project_id=project_id,
        )

    def discover_self_actor(self) -> str:
        self.self_actor_id = self.client.viewer_id()
        return self.self_actor_id

    def preflight_project(
        self, *, project_key: str, team_id: str, project_id: str,
    ) -> list[dict[str, Any]]:
        return self._preflight_bindings(
            project_key=project_key, workflow_digest=self.kernel.definition.digest,
            states=self.kernel.states, team_id=team_id, project_id=project_id,
        )

    def _preflight_bindings(
        self, *, project_key: str, workflow_digest: str,
        states: dict[str, dict[str, Any]], team_id: str, project_id: str,
    ) -> list[dict[str, Any]]:
        required = sorted({
            str(state["linear_status"]) for state in states.values()
            if state.get("linear_status")
        })
        bindings = self.client.preflight(
            project_key=project_key, workflow_digest=workflow_digest, team_id=team_id,
            project_id=project_id, required_status_names=required,
        )
        stored = self.ledger.bind_linear_statuses(
            project_key, workflow_digest, team_id, bindings
        )
        if not self.self_actor_id:
            self.discover_self_actor()
        return stored

    def observe_issue(self, execution_id: str, issue: dict[str, Any]) -> dict[str, Any]:
        # Discovery carries labels, relations and priority that the ordinary
        # status query does not request. Hash the common observation contract,
        # not the query's incidental selection set, for revision idempotency.
        issue = {key: issue[key] for key in (
            "id", "identifier", "title", "description", "url", "updatedAt",
            "state", "team", "project",
        ) if key in issue}
        state = issue.get("state", {})
        observed_at = self.ledger.clock()
        current = self._validate_issue_scope(execution_id, issue)
        observation = LinearObservationV1(
            execution_id=execution_id,
            project_key=current["project_key"],
            issue_id=str(issue["id"]), issue_identifier=str(issue["identifier"]),
            status_id=str(state["id"]), status_name=str(state["name"]),
            remote_updated_at=str(issue["updatedAt"]), observed_at=observed_at,
            payload_hash=content_hash(issue), source="poll",
            observation_key=poll_observation_key(
                str(issue["id"]), str(issue["updatedAt"]), str(state["id"])
            ),
        )
        stored = self.reconciler.ingest(observation)
        return self.reconciler.reconcile(
            stored["id"], current_status_id=str(state["id"]),
            current_remote_updated_at=str(issue["updatedAt"]),
        )

    def poll(self, execution_id: str, issue_id: str) -> dict[str, Any]:
        return self.observe_issue(execution_id, self.client.issue(issue_id))

    def observe_webhook(
        self, execution_id: str, body: bytes, *, signature: str,
        delivery_id: str, verifier: LinearWebhookVerifier,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not self.self_actor_id:
            raise LinearAPIError(
                "self_actor_unknown",
                "Linear webhook processing requires authenticated actor preflight",
                retryable=False,
            )
        payload = verifier.verify(body, signature=signature, now=now)
        issue = payload.get("data")
        if not isinstance(issue, dict) or not isinstance(issue.get("state"), dict):
            raise LinearAPIError(
                "unsupported_webhook", "webhook does not contain an issue state",
                retryable=False,
            )
        current_execution = self._validate_issue_scope(execution_id, issue)
        state = issue["state"]
        observation = LinearObservationV1(
            execution_id=execution_id,
            project_key=current_execution["project_key"],
            issue_id=str(issue["id"]), issue_identifier=str(issue["identifier"]),
            status_id=str(state["id"]), status_name=str(state["name"]),
            remote_updated_at=str(issue["updatedAt"]),
            observed_at=self.ledger.clock(), payload_hash=content_hash(payload),
            source="webhook", observation_key=webhook_observation_key(delivery_id),
            delivery_id=delivery_id,
            actor_id=(str(payload["actor"]["id"])
                      if isinstance(payload.get("actor"), dict)
                      and payload["actor"].get("id") else None),
        )
        stored = self.reconciler.ingest(observation)
        current = self.client.issue(str(issue["id"]))
        self._validate_issue_scope(execution_id, current)
        return self.reconciler.reconcile(
            stored["id"], current_status_id=str(current["state"]["id"]),
            current_remote_updated_at=str(current["updatedAt"]),
            allow_transition=(
                not observation.actor_id
                or not self.self_actor_id
                or observation.actor_id != self.self_actor_id
            ),
        )

    def drain_one(self, mutation: dict[str, Any]) -> dict[str, Any]:
        current = self.ledger.current(mutation["execution_id"])
        bindings = self.ledger.require_linear_status_bindings(
            current["project_key"], current["workflow_digest"],
            [mutation["desired_status"]],
        )
        desired_id = str(bindings[0]["status_id"])
        issue_id = str(mutation["request"]["issue_identifier"])
        remote = self.client.issue(issue_id)
        self._validate_issue_scope(mutation["execution_id"], remote)
        remote_status = str(remote["state"]["name"])
        attempt = self.ledger.start_linear_mutation_attempt(mutation["id"])
        if remote_status == mutation["desired_status"]:
            result = self.ledger.confirm_linear_mutation(
                mutation["id"], attempt["attempt_id"],
                observed_status=remote_status, response=remote,
                remote_id=str(remote["id"]),
            )
            self.observe_issue(mutation["execution_id"], remote)
            return result
        expected = mutation.get("expected_observed_status")
        if expected is not None and remote_status != expected:
            result = self.ledger.confirm_linear_mutation(
                mutation["id"], attempt["attempt_id"],
                observed_status=remote_status, response=remote,
                remote_id=str(remote["id"]),
            )
            self.observe_issue(mutation["execution_id"], remote)
            return result
        try:
            self.client.update_issue_status(str(remote["id"]), desired_id)
            confirmed = self.client.issue(str(remote["id"]))
        except LinearAPIError as error:
            if error.ambiguous:
                self.ledger.mark_linear_mutation_ambiguous(
                    mutation["id"], attempt["attempt_id"], error=error.as_dict()
                )
            else:
                next_attempt_at = None
                if error.retryable:
                    ledger_now = datetime.fromisoformat(
                        str(self.ledger.clock()).replace("Z", "+00:00")
                    )
                    next_attempt_at = (
                        ledger_now + timedelta(seconds=30)
                    ).isoformat()
                self.ledger.fail_linear_mutation(
                    mutation["id"], attempt["attempt_id"],
                    retryable=error.retryable, error=error.as_dict(),
                    next_attempt_at=next_attempt_at,
                )
            raise
        result = self.ledger.confirm_linear_mutation(
            mutation["id"], attempt["attempt_id"],
            observed_status=str(confirmed["state"]["name"]),
            response=confirmed, remote_id=str(confirmed["id"]),
        )
        self.observe_issue(mutation["execution_id"], confirmed)
        return result

    def drain(self, limit: int = 25) -> list[dict[str, Any]]:
        results = []
        for _index in range(limit):
            pending = self.ledger.pending_linear_mutations(limit)
            if not pending:
                break
            results.append(self.drain_one(pending[0]))
        return results

    def recover(self) -> int:
        return self.ledger.recover_linear_mutations()
