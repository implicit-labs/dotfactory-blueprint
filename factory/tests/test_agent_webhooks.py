import hashlib
import hmac
import http.client
import json
import os
import select
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory.agent_webhooks import (  # noqa: E402
    AgentWebhookHTTPServer, AgentWebhookInbox, WebhookError, WebhookSettings,
)

UNSAFE_SQLITE = (3, 51, 0) <= sqlite3.sqlite_version_info <= (3, 51, 2)
UNSAFE_REASON = "SQLite 3.51.0..3.51.2 cannot run the concurrent WAL receiver; use a safe Python runtime"


@unittest.skipIf(UNSAFE_SQLITE, UNSAFE_REASON)
class AgentWebhookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "inbox.db"
        self.settings = WebhookSettings(self.path, "synthetic-secret", "org", "app", "agent")
        self.inbox = AgentWebhookInbox(self.settings)

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def payload(self, **updates):
        result = {
            "action": "created", "type": "AgentSessionEvent",
            "webhookTimestamp": int(time.time() * 1000),
            "organizationId": "org", "oauthClientId": "app", "appUserId": "agent",
            "webhookId": "webhook", "createdAt": "2026-09-07T00:00:00Z",
            "agentSession": {"id": "session", "issueId": "issue",
                             "organizationId": "org", "appUserId": "agent"},
            "promptContext": "private-prompt-must-not-persist",
        }
        result.update(updates)
        return result

    def encode(self, payload):
        return json.dumps(payload).encode()

    def sign(self, raw):
        return hmac.new(self.settings.secret.encode(), raw, hashlib.sha256).hexdigest()

    def send(self, payload=None, raw=None, **kwargs):
        raw = self.encode(payload or self.payload()) if raw is None else raw
        values = dict(signature=self.sign(raw), delivery_id="delivery", event_type="AgentSessionEvent")
        values.update(kwargs)
        return self.inbox.receive(raw, **values)

    def rows(self, table="events"):
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def test_ack_committed_receipt_only_and_private_payload_not_stored(self):
        self.assertEqual("recorded", self.send())
        row = self.rows()[0]
        self.assertEqual("not_dispatched", row["handling"])
        self.assertEqual("session", row["session_id"])
        self.assertEqual("issue", row["issue_id"])
        self.assertNotIn("prompt", " ".join(row.keys()))
        for file in self.path.parent.iterdir():
            self.assertNotIn(b"private-prompt-must-not-persist", file.read_bytes())
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertNotIn("synthetic-secret", repr(self.settings))

    def test_retry_and_restart_do_not_duplicate_event(self):
        payload = self.payload()
        self.assertEqual("recorded", self.send(payload))
        self.inbox.close()
        self.inbox = AgentWebhookInbox(self.settings)
        self.assertEqual("duplicate", self.send(payload))
        payload["webhookTimestamp"] += 1
        self.assertEqual("duplicate", self.send(payload, delivery_id="second-delivery"))
        self.assertEqual(1, len(self.rows()))
        self.assertEqual(2, len(self.rows("deliveries")))

    def test_conflicting_delivery_or_semantic_identity_is_durable(self):
        payload = self.payload()
        self.send(payload)
        payload["promptContext"] = "different-private-prompt"
        for delivery in ("delivery", "second-delivery"):
            with self.assertRaisesRegex(WebhookError, "conflicting_duplicate"):
                self.send(payload, delivery_id=delivery)
        self.assertEqual(1, len(self.rows()))
        self.assertEqual(2, len(self.rows("conflicts")))

    def test_same_delivery_cannot_identify_a_different_event(self):
        self.send()
        payload = self.payload()
        payload["agentSession"]["id"] = "other-session"
        with self.assertRaisesRegex(WebhookError, "conflicting_duplicate"):
            self.send(payload)

    def test_prompted_activity_identity_is_distinct_and_not_dispatched(self):
        self.send()
        prompted = self.payload(action="prompted", agentActivity={
            "id": "activity", "body": "a private follow-up",
        })
        self.assertEqual("recorded", self.send(prompted, delivery_id="prompt-delivery"))
        self.assertEqual("duplicate", self.send(prompted, delivery_id="prompt-retry"))
        self.assertEqual(2, len(self.rows()))
        self.assertEqual({"not_dispatched"}, {row["handling"] for row in self.rows()})

    def test_signature_covers_exact_raw_body(self):
        raw = self.encode(self.payload())
        for signature in ("", "x", "0" * 64, "é" * 64):
            with self.subTest(signature=signature), self.assertRaisesRegex(WebhookError, "invalid_signature"):
                self.send(raw=raw, signature=signature)
        with self.assertRaisesRegex(WebhookError, "invalid_signature"):
            self.send(raw=raw + b" ", signature=self.sign(raw))
        self.assertEqual([], self.rows())

    def test_signed_timestamp_is_required_recent_and_finite(self):
        now = time.time()
        for timestamp in (None, "1", True, float("inf"), 10 ** 500, -1, now * 1000 + 0.25):
            with self.subTest(timestamp=timestamp), self.assertRaises(WebhookError):
                self.send(self.payload(webhookTimestamp=timestamp), now=now)
        for age in (-61, 61):
            with self.assertRaisesRegex(WebhookError, "stale_webhook"):
                self.send(self.payload(webhookTimestamp=int((now + age) * 1000)), now=now)

    def test_body_type_action_and_all_bindings_fail_closed(self):
        for changes in ({"type": "Issue"}, {"action": "update"},
                        {"organizationId": "other"}, {"oauthClientId": "other"},
                        {"appUserId": "other"}, {"agentSession": []},
                        {"action": "prompted", "agentActivity": {}}):
            with self.subTest(changes=changes), self.assertRaises(WebhookError):
                self.send(self.payload(**changes))
        for key in ("organizationId", "appUserId"):
            payload = self.payload()
            payload["agentSession"][key] = "wrong"
            with self.assertRaisesRegex(WebhookError, "wrong_binding"):
                self.send(payload)
        with self.assertRaisesRegex(WebhookError, "unsupported_event"):
            self.send(event_type="Issue")
        self.assertEqual([], self.rows())

    def test_malformed_oversized_and_deep_json_are_rejected(self):
        for raw in (b"", b"{}" * 150000, b"[]", b"null", b"{", b"\xff",
                    b'{"x":1,"x":2}', b'{"x":NaN}',
                    b"[" * 2000 + b"]" * 2000):
            with self.subTest(size=len(raw)), self.assertRaises(WebhookError):
                self.send(raw=raw)

    def test_closed_or_locked_store_never_acknowledges(self):
        with sqlite3.connect(self.path) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError):
                self.send()
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual([], self.rows())
        self.inbox.close()
        self.assertFalse(self.inbox.ready())
        with self.assertRaises(sqlite3.OperationalError):
            self.send()

    def test_configuration_and_durable_binding_guard(self):
        self.assertTrue(self.inbox.ready())
        with self.assertRaisesRegex(ValueError, "binding differs"):
            AgentWebhookInbox(WebhookSettings(self.path, "new-secret", "wrong-org", "app"))
        foreign = self.path.parent / "foreign.db"
        with sqlite3.connect(foreign) as connection:
            connection.execute("CREATE TABLE canonical_ledger (id INTEGER)")
        with self.assertRaisesRegex(ValueError, "not a Linear"):
            AgentWebhookInbox(WebhookSettings(foreign, "secret", "org", "app"))
        with self.assertRaises(ValueError):
            WebhookSettings.from_env({})
        with self.assertRaises(ValueError):
            WebhookSettings(Path(":memory:"), "secret", "org", "app")

    def test_concurrent_delivery_commits_one_event(self):
        raw = self.encode(self.payload())
        results = []
        def deliver():
            results.append(self.send(raw=raw))
        threads = [threading.Thread(target=deliver) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(["duplicate"] * 7 + ["recorded"], sorted(results))
        self.assertEqual(1, len(self.rows()))


    def test_every_request_connection_closes_including_failure(self):
        opened, closed = [], []
        original_connect = sqlite3.connect
        class TrackingConnection(sqlite3.Connection):
            def close(self):
                closed.append(self)
                return super().close()
        def tracked_connect(*args, **kwargs):
            connection = original_connect(*args, factory=TrackingConnection, **kwargs)
            opened.append(connection)
            return connection
        raw = self.encode(self.payload())
        with mock.patch("dotfactory.agent_webhooks.sqlite3.connect", side_effect=tracked_connect):
            for _ in range(100):
                self.send(raw=raw)
                self.assertTrue(self.inbox.ready())
            with self.assertRaises(WebhookError):
                self.send(self.payload(promptContext="conflicting body"))
        self.assertEqual(201, len(opened))
        self.assertEqual(opened, closed)
        self.assertFalse(Path(str(self.path) + "-wal").exists())

    def test_service_cli_sigterm_and_restart_preserve_receipts(self):
        self.send()
        env = {
            **os.environ, "PYTHONPATH": str(ROOT / "src"),
            "LINEAR_WEBHOOK_DATABASE": str(self.path),
            "LINEAR_WEBHOOK_SECRET": self.settings.secret,
            "LINEAR_WEBHOOK_ORGANIZATION_ID": "org",
            "LINEAR_WEBHOOK_OAUTH_CLIENT_ID": "app",
            "LINEAR_WEBHOOK_APP_USER_ID": "agent",
        }
        for _ in range(2):
            process = subprocess.Popen(
                [sys.executable, "-m", "dotfactory.agent_webhooks", "serve", "--port", "0"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                readable, _, _ = select.select([process.stdout], [], [], 5)
                self.assertTrue(readable, "service never reported readiness")
                self.assertIn("receipt-only", process.stdout.readline())
                process.terminate()
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(0, process.returncode, stderr)
                self.assertNotIn(self.settings.secret, stdout + stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(1, len(self.rows()))


@unittest.skipIf(UNSAFE_SQLITE, UNSAFE_REASON)
class AgentWebhookHTTPTests(unittest.TestCase):
    payload = AgentWebhookTests.payload
    encode = AgentWebhookTests.encode
    sign = AgentWebhookTests.sign

    def setUp(self):
        AgentWebhookTests.setUp(self)
        self.server = AgentWebhookHTTPServer(("127.0.0.1", 0), self.inbox)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        AgentWebhookTests.tearDown(self)

    def request(self, method="POST", path="/webhooks/linear", raw=None, headers=None):
        raw = self.encode(self.payload()) if raw is None else raw
        values = {"Content-Type": "application/json", "Linear-Signature": self.sign(raw),
                  "Linear-Delivery": "delivery", "Linear-Event": "AgentSessionEvent"}
        values.update(headers or {})
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=values)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_real_post_ack_restart_and_local_receipts_cli(self):
        raw = self.encode(self.payload())
        start = time.monotonic()
        self.assertEqual(200, self.request(raw=raw)[0])
        self.assertLess(time.monotonic() - start, 2)
        self.inbox.close()
        self.inbox = AgentWebhookInbox(self.settings)
        self.server.inbox = self.inbox
        self.assertEqual("duplicate", json.loads(self.request(raw=raw)[1])["status"])
        result = subprocess.run(
            [sys.executable, "-m", "dotfactory.agent_webhooks", "receipts", "--database", str(self.path)],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(1, len(evidence["events"]))
        self.assertNotIn("private-prompt", result.stdout)

    def test_http_auth_validation_and_no_public_receipt_lookup(self):
        for headers, raw, expected in (
            ({"Linear-Signature": "bad"}, None, 401),
            ({"Content-Type": "text/plain"}, None, 415),
            ({"Content-Encoding": "gzip"}, None, 415),
            ({"Content-Length": "999999"}, b"a", 413),
            ({}, b"{", 400),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(expected, self.request(raw=raw, headers=headers)[0])
        self.assertEqual(404, self.request("GET", "/receipts")[0])
        self.assertEqual(404, self.request(path="/webhooks/linear?secret=private")[0])

    def test_real_http_ready_and_storage_failure(self):
        self.assertEqual(200, self.request("GET", "/readyz")[0])
        with mock.patch.object(self.inbox, "receive", side_effect=sqlite3.OperationalError("disk full")):
            code, body = self.request()
            self.assertEqual(503, code)
            self.assertNotIn(b"disk full", body)
        self.inbox.close()
        self.assertEqual(503, self.request("GET", "/readyz")[0])
        self.assertEqual(200, self.request("GET", "/healthz")[0])

    def test_duplicate_content_length_is_rejected(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.putrequest("POST", "/webhooks/linear")
            for header, value in (("Content-Type", "application/json"),
                                  ("Content-Length", "2"), ("Content-Length", "3")):
                connection.putheader(header, value)
            connection.endheaders(b"{}")
            response = connection.getresponse()
            self.assertEqual(400, response.status)
            response.read()
        finally:
            connection.close()

    def test_incomplete_body_has_bounded_deadline(self):
        connection = socket.create_connection(self.server.server_address, timeout=5)
        try:
            start = time.monotonic()
            connection.sendall(
                b"POST /webhooks/linear HTTP/1.0\r\nContent-Type: application/json\r\n"
                b"Content-Length: 99\r\nLinear-Signature: " + b"0" * 64
                + b"\r\nLinear-Delivery: delivery\r\nLinear-Event: AgentSessionEvent\r\n\r\n{"
            )
            self.assertIn(b"408", connection.recv(4096))
            self.assertLess(time.monotonic() - start, 3.5)
        finally:
            connection.close()


class AgentWebhookRuntimeSafetyTests(unittest.TestCase):
    def test_unsafe_sqlite_wal_version_is_refused_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "never-created.db"
            settings = WebhookSettings(path, "secret", "org", "app")
            for version in ((3, 51, 0), (3, 51, 1), (3, 51, 2)):
                with mock.patch("dotfactory.agent_webhooks.sqlite3.sqlite_version_info", version):
                    with self.assertRaisesRegex(ValueError, "unsafe for concurrent WAL"):
                        AgentWebhookInbox(settings)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
