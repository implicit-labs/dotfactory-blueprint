"""WSGI adapter for the versioned factory observation and control API."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs

from .control import ControlError, ControlService, ObservationService, Principal
from .ledger import LedgerError


Authenticator = Callable[[dict[str, Any]], Optional[Principal]]
StartResponse = Callable[[str, list[tuple[str, str]]], Any]


class ControlHTTPApp:
    def __init__(
        self, observation: ObservationService, control: ControlService,
        authenticate: Authenticator,
    ) -> None:
        self.observation = observation
        self.control = control
        self.authenticate = authenticate

    def __call__(
        self, environ: dict[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        try:
            principal = self.authenticate(environ)
            if principal is None:
                return self._respond(
                    start_response, 401,
                    {"error": {"code": "unauthorized", "message": "authentication required"}},
                )
            method = str(environ.get("REQUEST_METHOD", "GET")).upper()
            path = str(environ.get("PATH_INFO", ""))
            query = parse_qs(str(environ.get("QUERY_STRING", "")), keep_blank_values=False)
            if method == "GET":
                return self._get(path, query, start_response)
            if method == "POST":
                return self._post(path, environ, principal, start_response)
            raise ControlError("method_not_allowed", "method not allowed", status=405)
        except ControlError as error:
            return self._respond(
                start_response, error.status,
                {"error": {"code": error.code, "message": str(error)}},
            )
        except LedgerError as error:
            status = 404 if "not found" in str(error) else 409
            code = "not_found" if status == 404 else "ledger_conflict"
            return self._respond(
                start_response, status,
                {"error": {"code": code, "message": str(error)}},
            )

    def _get(
        self, path: str, query: dict[str, list[str]], start_response: StartResponse
    ) -> Iterable[bytes]:
        if path == "/v1/overview":
            return self._respond(start_response, 200, self.observation.overview())
        if path == "/v1/runs":
            return self._respond(start_response, 200, self.observation.runs(
                project_key=self._one(query, "project_key"),
                status=self._one(query, "status"), state=self._one(query, "state"),
                limit=self._integer(query, "limit", 25), cursor=self._one(query, "cursor"),
            ))
        if path == "/v1/resources":
            return self._respond(start_response, 200, self.observation.resources(
                status=self._one(query, "status"),
                project_key=self._one(query, "project_key"),
                execution_id=self._one(query, "execution_id"),
                limit=self._integer(query, "limit", 25), cursor=self._one(query, "cursor"),
            ))
        command_match = re.fullmatch(r"/v1/commands/([^/]+)", path)
        if command_match:
            return self._respond(
                start_response, 200, self.observation.command(command_match.group(1))
            )
        run_match = re.fullmatch(r"/v1/runs/([^/]+)", path)
        if run_match:
            return self._respond(
                start_response, 200, self.observation.run(run_match.group(1))
            )
        child_match = re.fullmatch(
            r"/v1/runs/([^/]+)/(events|artifacts|feedback)", path
        )
        if child_match:
            execution_id, child = child_match.groups()
            if child == "events":
                payload = self.observation.events(
                    execution_id, after_seq=self._integer(query, "after_seq", 0),
                    limit=self._integer(query, "limit", 100),
                )
            elif child == "artifacts":
                payload = self.observation.artifacts(
                    execution_id, kind=self._one(query, "kind"),
                    limit=self._integer(query, "limit", 25),
                    cursor=self._one(query, "cursor"),
                )
            else:
                payload = self.observation.feedback(
                    execution_id, limit=self._integer(query, "limit", 25),
                    cursor=self._one(query, "cursor"),
                )
            return self._respond(start_response, 200, payload)
        raise ControlError("not_found", "endpoint not found", status=404)

    def _post(
        self, path: str, environ: dict[str, Any], principal: Principal,
        start_response: StartResponse,
    ) -> Iterable[bytes]:
        match = re.fullmatch(r"/v1/runs/([^/]+)/commands", path)
        if not match:
            raise ControlError("not_found", "endpoint not found", status=404)
        command_id = str(environ.get("HTTP_IDEMPOTENCY_KEY", ""))
        request = self._json_body(environ)
        receipt = self.control.execute(
            match.group(1), command_id=command_id, principal=principal, request=request
        )
        status = 200
        if receipt["status"] == "denied":
            status = 403
        elif receipt["status"] == "failed":
            status = 409
        return self._respond(
            start_response, status, {"api_version": "v1", "data": receipt}
        )

    def _json_body(self, environ: dict[str, Any]) -> dict[str, Any]:
        raw_length = str(environ.get("CONTENT_LENGTH", "0") or "0")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ControlError("invalid_content_length", "Content-Length is invalid") from error
        if length < 1 or length > 65536:
            raise ControlError("invalid_body_size", "JSON body must be 1 to 65536 bytes")
        raw = environ["wsgi.input"].read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ControlError("invalid_json", "body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise ControlError("invalid_json", "body must be a JSON object")
        return payload

    def _one(self, query: dict[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        if not values:
            return None
        if len(values) != 1:
            raise ControlError("invalid_query", f"{name} may appear once")
        return values[0]

    def _integer(
        self, query: dict[str, list[str]], name: str, default: int
    ) -> int:
        value = self._one(query, name)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError as error:
            raise ControlError("invalid_query", f"{name} must be an integer") from error

    def _respond(
        self, start_response: StartResponse, status: int, payload: dict[str, Any]
    ) -> list[bytes]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        phrases = {
            200: "OK", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            409: "Conflict",
        }
        start_response(
            f"{status} {phrases[status]}",
            [("Content-Type", "application/json"),
             ("Content-Length", str(len(body))), ("Cache-Control", "no-store")],
        )
        return [body]
