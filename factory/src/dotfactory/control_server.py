"""Authenticated loopback gateway; ledger writes stay with the exclusive owner."""

from __future__ import annotations

import hmac
import io
import json
import os
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlsplit

from .control import ControlError, ObservationService, Principal
from .http_api import ControlHTTPApp
from .lifecycle import FactoryRuntime, _ledger_path
from .operator import MAX_MESSAGE, send, socket_path


def dispatch_http(runtime: Any, message: dict[str, Any]) -> dict[str, Any]:
    """Called on the ledger thread by a trusted same-UID gateway/socket client."""
    principal = Principal(**message["principal"])

    class ProjectControl:
        def execute(self, execution: str, **kwargs: Any) -> Any:
            project = runtime.ledger.current(execution)["project_key"]
            if project not in runtime.kernels:
                raise ControlError("project_unavailable", "project is not selected", status=409)
            return runtime.control_service(project).execute(execution, **kwargs)

    kernel = next(iter(runtime.kernels.values()))
    app = ControlHTTPApp(ObservationService(runtime.ledger, kernel), ProjectControl(), lambda _: principal)
    body = message.get("body", "").encode("utf-8")
    environ = {
        "REQUEST_METHOD": message["method"], "PATH_INFO": message["path"],
        "QUERY_STRING": message.get("query", ""), "CONTENT_LENGTH": str(len(body)),
        "HTTP_IDEMPOTENCY_KEY": message.get("command_id", ""), "wsgi.input": io.BytesIO(body),
    }
    result: dict[str, Any] = {}

    def start(status: str, headers: list[tuple[str, str]]) -> None:
        result.update(status=int(status.split()[0]), headers=headers)

    result["body"] = b"".join(app(environ, start)).decode("utf-8")
    return result


def forward(config: Any, message: dict[str, Any]) -> dict[str, Any]:
    path = _ledger_path(config)
    if not path.is_file():
        raise RuntimeError("ledger does not exist; run a local task first")
    # Check the encoded envelope before attempting either transport.
    if len(json.dumps(message).encode()) + 1 > MAX_MESSAGE:
        raise ValueError("request exceeds operator transport limit")
    try:
        response = send(socket_path(path), message)
    except (FileNotFoundError, ConnectionRefusedError):
        # These errors occur before delivery. Lock acquisition arbitrates startup races.
        with FactoryRuntime(config, control_only=True) as runtime:
            return dispatch_http(runtime, message)
    if not response.get("ok"):
        raise RuntimeError("owner rejected request; do not retry with a new command ID")
    return response["data"]


def make_server(config: Any, token: str, principal: Principal, port: int = 8765) -> HTTPServer:
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("API token must contain at least 32 non-whitespace ASCII characters")
    if not _ledger_path(config).is_file():
        raise ValueError("ledger does not exist; run a local task first")

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, _format: str, *args: Any) -> None:
            pass  # Never log credentials, query strings, or task bodies.

        def reply(self, status: int, body: str, headers: Any = ()) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            for key, value in headers:
                if key.lower() not in ("content-length", "connection"):
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

        def error(self, status: int, message: str) -> None:
            self.reply(status, json.dumps({"error": {"message": message}}),
                       [("Content-Type", "application/json"), ("Cache-Control", "no-store")])

        def handle_api(self) -> None:
            authorization = self.headers.get_all("Authorization", [])
            expected = ("Bearer " + token).encode()
            if len(authorization) != 1 or not hmac.compare_digest(authorization[0].encode(), expected):
                self.error(401, "authentication required")
                return
            if self.headers.get("Origin"):
                self.error(403, "browser-origin requests are not supported")
                return
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                self.error(400, "ambiguous request framing")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 32768:
                    self.error(413, "body limit is 32768 bytes")
                    return
                if self.command == "POST" and self.headers.get_content_type() != "application/json":
                    self.error(400, "Content-Type must be application/json")
                    return
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("incomplete body")
                body = raw.decode("utf-8")
            except (ValueError, OSError):
                self.error(400, "invalid or incomplete body")
                return
            try:
                target = urlsplit(self.path)
                if target.scheme or target.netloc or not self.path.startswith("/"):
                    raise ValueError("absolute request target")
            except ValueError:
                self.error(400, "request target must be a local path")
                return
            message = {
                "operation": "http", "method": self.command, "path": target.path,
                "query": target.query, "body": body,
                "command_id": self.headers.get("Idempotency-Key", ""),
                "principal": {"subject": principal.subject, "role": principal.role,
                              "channel": principal.channel},
            }
            try:
                result = forward(config, message)
            except Exception:
                self.error(503, "control unavailable or outcome unknown; retry commands only with the same Idempotency-Key")
                return
            self.reply(result["status"], result["body"], result["headers"])

        do_GET = handle_api
        do_POST = handle_api
        do_PUT = handle_api
        do_DELETE = handle_api
        do_PATCH = handle_api
        do_OPTIONS = handle_api

    return HTTPServer(("127.0.0.1", port), Handler)


def serve(args: Any) -> int:
    from .instance import FactoryConfig
    config = FactoryConfig.load(args.config)
    principal = Principal(args.subject, args.role, "http")
    server = make_server(config, os.environ.get(args.token_env, ""), principal, args.port)
    stopped = False

    def stop(_number: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    previous = {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        server.timeout = 0.2
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "role": principal.role,
                          "subject": principal.subject, "token_env": args.token_env}), flush=True)
        while not stopped:
            server.handle_request()
    finally:
        server.server_close()
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 0
