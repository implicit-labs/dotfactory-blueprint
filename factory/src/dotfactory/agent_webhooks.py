"""Receipt-only Linear AgentSessionEvent endpoint; never launches agent work."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Mapping


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SIGNATURE = re.compile(r"[0-9a-fA-F]{64}\Z")
_TABLES = {"inbox_config", "events", "deliveries", "conflicts"}


class WebhookError(Exception):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise WebhookError("invalid_identifier")
    return value


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WebhookError("invalid_payload")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WebhookError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_number(value: str) -> None:
    raise WebhookError("invalid_json_number")


@dataclass(frozen=True)
class WebhookSettings:
    database: Path
    secret: str = field(repr=False)
    organization_id: str
    oauth_client_id: str
    app_user_id: str | None = None
    maximum_body_bytes: int = 262144
    maximum_age_seconds: int = 60

    def __post_init__(self) -> None:
        if not self.secret or len(self.secret) > 4096:
            raise ValueError("a webhook signing secret is required")
        for value in (self.organization_id, self.oauth_client_id):
            _identifier(value)
        if self.app_user_id is not None:
            _identifier(self.app_user_id)
        if not 1024 <= self.maximum_body_bytes <= 1048576:
            raise ValueError("maximum body size must be 1024..1048576 bytes")
        if not 1 <= self.maximum_age_seconds <= 60:
            raise ValueError("maximum webhook age must be 1..60 seconds")
        if str(self.database) == ":memory:":
            raise ValueError("the inbox requires a durable database path")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "WebhookSettings":
        env = os.environ if environ is None else environ
        required = ("LINEAR_WEBHOOK_DATABASE", "LINEAR_WEBHOOK_SECRET",
                    "LINEAR_WEBHOOK_ORGANIZATION_ID", "LINEAR_WEBHOOK_OAUTH_CLIENT_ID")
        if any(not env.get(name) for name in required):
            raise ValueError("required LINEAR_WEBHOOK settings are missing")
        return cls(
            Path(env[required[0]]), env[required[1]], env[required[2]], env[required[3]],
            env.get("LINEAR_WEBHOOK_APP_USER_ID") or None,
        )


class AgentWebhookInbox:
    """A separate receipt database, not a canonical workflow ledger or prompt queue."""

    def __init__(self, settings: WebhookSettings):
        if (3, 51, 0) <= sqlite3.sqlite_version_info <= (3, 51, 2):
            raise ValueError("SQLite 3.51.0..3.51.2 is unsafe for concurrent WAL connections")
        self.settings = settings
        self.path = settings.database
        self.closed = False
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("the inbox database must not be a symlink")
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._connect() as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if tables and tables != _TABLES:
                raise ValueError("database is not a Linear agent webhook inbox")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS inbox_config (
                    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                    schema_version INTEGER NOT NULL,
                    organization_id TEXT NOT NULL, oauth_client_id TEXT NOT NULL,
                    app_user_id TEXT, readiness_check INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_key TEXT PRIMARY KEY, semantic_sha256 TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL, action TEXT NOT NULL,
                    session_id TEXT NOT NULL, issue_id TEXT, activity_id TEXT,
                    app_user_id TEXT NOT NULL, webhook_id TEXT NOT NULL,
                    webhook_timestamp_ms INTEGER NOT NULL, received_at_ms INTEGER NOT NULL,
                    handling TEXT NOT NULL CHECK (handling='not_dispatched')
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY, event_key TEXT NOT NULL,
                    semantic_sha256 TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
                    received_at_ms INTEGER NOT NULL,
                    FOREIGN KEY(event_key) REFERENCES events(event_key)
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    delivery_id TEXT NOT NULL, event_key TEXT NOT NULL,
                    semantic_sha256 TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
                    received_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(delivery_id, semantic_sha256)
                );
            """)
            binding = (1, settings.organization_id, settings.oauth_client_id,
                       settings.app_user_id)
            existing = connection.execute(
                "SELECT schema_version, organization_id, oauth_client_id, app_user_id "
                "FROM inbox_config WHERE singleton=1"
            ).fetchone()
            if existing is not None and tuple(existing) != binding:
                raise ValueError("inbox schema or organization/app binding differs")
            connection.execute(
                "INSERT OR IGNORE INTO inbox_config "
                "(singleton, schema_version, organization_id, oauth_client_id, app_user_id) "
                "VALUES(1, ?, ?, ?, ?)", binding,
            )
        os.chmod(self.path, 0o600)
        with self._connect() as connection:
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise ValueError("the inbox requires SQLite WAL support")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self.closed:
            raise sqlite3.OperationalError("inbox closed")
        connection = sqlite3.connect(str(self.path), timeout=0.5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def close(self) -> None:
        self.closed = True

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE inbox_config SET readiness_check=readiness_check+1 WHERE singleton=1"
                )
            return True
        except sqlite3.Error:
            return False

    def receive(
        self, body: bytes, *, signature: str, delivery_id: str,
        event_type: str, now: float | None = None,
    ) -> str:
        if not 0 < len(body) <= self.settings.maximum_body_bytes:
            raise WebhookError("invalid_body_size", 413)
        if not _SIGNATURE.fullmatch(signature):
            raise WebhookError("invalid_signature", 401)
        expected = hmac.new(self.settings.secret.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, bytes.fromhex(signature)):
            raise WebhookError("invalid_signature", 401)
        try:
            payload = _object(json.loads(
                body.decode("utf-8"), object_pairs_hook=_unique_object,
                parse_constant=_invalid_number,
            ))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
            raise WebhookError("invalid_json") from error
        current_ms = int((time.time() if now is None else now) * 1000)
        timestamp = payload.get("webhookTimestamp")
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not 0 <= timestamp <= 9007199254740991
                or not math.isfinite(timestamp) or int(timestamp) != timestamp):
            raise WebhookError("invalid_timestamp")
        if abs(current_ms - timestamp) > self.settings.maximum_age_seconds * 1000:
            raise WebhookError("stale_webhook", 401)
        if payload.get("type") != "AgentSessionEvent" or event_type != "AgentSessionEvent":
            raise WebhookError("unsupported_event", 422)
        if (payload.get("organizationId") != self.settings.organization_id
                or payload.get("oauthClientId") != self.settings.oauth_client_id):
            raise WebhookError("wrong_binding", 403)
        session = _object(payload.get("agentSession"))
        app_user = _identifier(payload.get("appUserId"))
        if (session.get("organizationId") != self.settings.organization_id
                or session.get("appUserId") != app_user
                or (self.settings.app_user_id is not None
                    and app_user != self.settings.app_user_id)):
            raise WebhookError("wrong_binding", 403)
        action = payload.get("action")
        if action not in ("created", "prompted"):
            raise WebhookError("unsupported_action", 422)
        session_id = _identifier(session.get("id"))
        issue_id = session.get("issueId")
        if issue_id is not None:
            issue_id = _identifier(issue_id)
        activity_id = None
        if action == "prompted":
            activity_id = _identifier(_object(payload.get("agentActivity")).get("id"))
        webhook_id = _identifier(payload.get("webhookId"))
        delivery_id = _identifier(delivery_id)
        identity = [self.settings.organization_id, self.settings.oauth_client_id,
                    session_id, action, activity_id]
        event_key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        # Linear may refresh its signed send timestamp when retrying a delivery.
        semantic_payload = dict(payload)
        semantic_payload.pop("webhookTimestamp")
        try:
            semantic_hash = hashlib.sha256(json.dumps(
                semantic_payload, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True, allow_nan=False,
            ).encode()).hexdigest()
        except (ValueError, RecursionError) as error:
            raise WebhookError("invalid_json") from error
        raw_hash = hashlib.sha256(body).hexdigest()
        conflict = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior_delivery = connection.execute(
                "SELECT event_key, semantic_sha256 FROM deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            prior_event = connection.execute(
                "SELECT semantic_sha256 FROM events WHERE event_key=?", (event_key,),
            ).fetchone()
            conflict = bool(
                (prior_delivery and (prior_delivery["event_key"] != event_key
                                     or prior_delivery["semantic_sha256"] != semantic_hash))
                or (prior_event and prior_event["semantic_sha256"] != semantic_hash)
            )
            if conflict:
                connection.execute(
                    "INSERT OR IGNORE INTO conflicts VALUES(?, ?, ?, ?, ?)",
                    (delivery_id, event_key, semantic_hash, raw_hash, current_ms),
                )
            else:
                connection.execute(
                    "INSERT OR IGNORE INTO events VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_key, semantic_hash, raw_hash, action, session_id, issue_id,
                     activity_id, app_user, webhook_id, int(timestamp), current_ms,
                     "not_dispatched"),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO deliveries VALUES(?, ?, ?, ?, ?)",
                    (delivery_id, event_key, semantic_hash, raw_hash, current_ms),
                )
        if conflict:
            raise WebhookError("conflicting_duplicate", 409)
        return "duplicate" if prior_event else "recorded"


class AgentWebhookHTTPServer(ThreadingHTTPServer):
    """Bounded local backend. Put HTTPS and request-rate limits at the ingress."""

    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], inbox: AgentWebhookInbox):
        self.inbox = inbox
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, AgentWebhookHandler)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: socket.socket, client_address: Any) -> None:
        # Do not leak webhook bodies, paths, or local state through traceback logging.
        print("linear_webhook_request_failed", file=sys.stderr)


class AgentWebhookHandler(BaseHTTPRequestHandler):
    server_version = "dotfactory-webhook"
    sys_version = ""

    def setup(self) -> None:
        self.request.settimeout(2)
        super().setup()

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Access logs can contain private query strings and identifiers.

    def _respond(self, status: int, code: str) -> None:
        raw = json.dumps({"status": code, "mode": "receipt_only"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, "alive")
        elif self.path == "/readyz":
            ready = self.server.inbox.ready()
            self._respond(200 if ready else 503, "ready" if ready else "storage_unavailable")
        else:
            self._respond(404, "not_found")

    def _one_header(self, name: str) -> str:
        values = self.headers.get_all(name, [])
        if len(values) != 1:
            raise WebhookError("invalid_headers")
        return values[0]

    def do_POST(self) -> None:
        try:
            if self.path != "/webhooks/linear":
                raise WebhookError("not_found", 404)
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding"):
                raise WebhookError("unsupported_encoding", 415)
            content_type = self._one_header("Content-Type").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise WebhookError("unsupported_content_type", 415)
            length_text = self._one_header("Content-Length")
            if not re.fullmatch(r"[0-9]{1,10}", length_text):
                raise WebhookError("invalid_content_length")
            length = int(length_text)
            if not 0 < length <= self.server.inbox.settings.maximum_body_bytes:
                raise WebhookError("invalid_body_size", 413)
            signature = self._one_header("Linear-Signature")
            delivery_id = self._one_header("Linear-Delivery")
            event_type = self._one_header("Linear-Event")
            deadline = time.monotonic() + 2
            body = bytearray()
            while len(body) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebhookError("request_timeout", 408)
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(length - len(body), 65536))
                if not chunk:
                    raise WebhookError("truncated_body")
                body.extend(chunk)
            self.connection.settimeout(2)
            result = self.server.inbox.receive(
                bytes(body), signature=signature, delivery_id=delivery_id,
                event_type=event_type,
            )
            self._respond(200, result)
        except WebhookError as error:
            self._respond(error.status, error.code)
        except (TimeoutError, socket.timeout):
            self._respond(408, "request_timeout")
        except (sqlite3.Error, OSError):
            self._respond(503, "storage_unavailable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve receipt-only ingress")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=os.environ.get("PORT", "8080"))
    receipts = commands.add_parser("receipts", help="inspect local IDs/hashes; never prompts")
    receipts.add_argument("--database", type=Path, required=True)
    receipts.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    if args.command == "receipts":
        if not 1 <= args.limit <= 100:
            parser.error("limit must be 1..100")
        with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM events ORDER BY received_at_ms DESC, event_key LIMIT ?",
                (args.limit,),
            ).fetchall()
            print(json.dumps({"mode": "receipt_only", "events": [dict(row) for row in rows],
                              "conflicts": connection.execute(
                                  "SELECT COUNT(*) FROM conflicts").fetchone()[0]}, sort_keys=True))
        return 0
    try:
        inbox = AgentWebhookInbox(WebhookSettings.from_env())
    except (ValueError, WebhookError) as error:
        print("webhook configuration refused: " + str(error), file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError):
        print("webhook storage unavailable; check the dedicated writable volume", file=sys.stderr)
        return 1
    try:
        server = AgentWebhookHTTPServer((args.host, args.port), inbox)
    except OSError:
        inbox.close()
        print("webhook listener unavailable; check the bind address and PORT", file=sys.stderr)
        return 1
    def stop(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        print("Linear webhook receiver ready (receipt-only; inbound work is not dispatched)", flush=True)
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        inbox.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
