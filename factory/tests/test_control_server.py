import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from dotfactory.cli import _demo_config
from dotfactory.control import Principal
from dotfactory.control_server import forward, make_server
from dotfactory.instance import FactoryConfig
from dotfactory.lifecycle import FactoryRuntime, _ledger_path


class ControlServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = _demo_config(Path(self.temp.name))
        self.config = FactoryConfig.load(self.config_path)
        with FactoryRuntime(self.config, control_only=True) as runtime:
            self.execution = runtime.start_issue("demo", "LOCAL-HTTP-1")
        self.token = "test-token-" + "x" * 32

    def start(self, role="viewer"):
        server = make_server(self.config, self.token, Principal("test-client", role, "http"), 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()

        def close():
            server.shutdown()
            thread.join(2)
            server.server_close()
        self.addCleanup(close)
        self.port = server.server_port
        return server

    def request(self, method="GET", path="/v1/overview", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        supplied = {"Authorization": "Bearer " + self.token}
        supplied.update(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body)
            supplied.setdefault("Content-Type", "application/json")
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def command(self):
        return self.request("POST", f"/v1/runs/{self.execution}/commands", {
            "action": "cancel", "expected_state": "Todo", "confirmed": True, "parameters": {},
        }, {"Idempotency-Key": "http-cancel-1", "X-Role": "approver"})

    def test_real_http_auth_reads_and_framing(self):
        server = self.start()
        self.assertEqual("127.0.0.1", server.server_address[0])
        with patch("dotfactory.control_server.forward") as dispatch:
            self.assertEqual(401, self.request(headers={"Authorization": ""})[0])
            self.assertEqual(401, self.request(headers={"Authorization": "Bearer wrong"})[0])
            dispatch.assert_not_called()
        self.assertEqual(200, self.request()[0])
        self.assertEqual(200, self.request(path=f"/v1/runs/{self.execution}")[0])
        self.assertEqual(404, self.request(path="/init")[0])
        self.assertEqual(405, self.request("PUT")[0])
        self.assertEqual(403, self.request(headers={"Origin": "https://example.invalid"})[0])
        self.assertEqual(413, self.request("POST", body="x" * 32769)[0])
        self.assertEqual(400, self.request("POST", body="{}", headers={"Content-Type": "text/plain"})[0])
        self.assertEqual(400, self.request("POST", f"/v1/runs/{self.execution}/commands", body="{",
                                          headers={"Content-Type": "application/json", "Idempotency-Key": "bad-json"})[0])

    def test_viewer_cannot_be_promoted_by_request_headers(self):
        self.start()
        self.assertEqual(403, self.command()[0])
        with FactoryRuntime(self.config, control_only=True) as runtime:
            self.assertEqual("Todo", runtime.ledger.current(self.execution)["current_state_id"])

    def test_offline_command_is_idempotent(self):
        self.start("operator")
        first = self.command()
        self.assertEqual(200, first[0])
        self.assertEqual(first, self.command())
        with FactoryRuntime(self.config, control_only=True) as runtime:
            self.assertEqual("Canceled", runtime.ledger.current(self.execution)["current_state_id"])
        self.assertIn("test-client", json.dumps(first))
        self.assertIn("http", json.dumps(first))

    def test_active_owner_preserves_viewer_role_and_uses_no_second_runtime(self):
        self.start()
        with FactoryRuntime(self.config, control_only=True) as runtime:
            runtime.enable_operator()
            with patch("dotfactory.control_server.FactoryRuntime") as second, ThreadPoolExecutor(1) as pool:
                pending = pool.submit(self.command)
                deadline = time.monotonic() + 4
                while not pending.done() and time.monotonic() < deadline:
                    runtime.operator_server.pump()
                    time.sleep(0.01)
                self.assertEqual(403, pending.result(timeout=1)[0])
                second.assert_not_called()

    def test_active_operator_command_is_idempotent(self):
        self.start("operator")
        with FactoryRuntime(self.config, control_only=True) as runtime:
            runtime.enable_operator()
            with ThreadPoolExecutor(1) as pool:
                results = []
                for _ in range(2):
                    pending = pool.submit(self.command)
                    deadline = time.monotonic() + 4
                    while not pending.done() and time.monotonic() < deadline:
                        runtime.operator_server.pump()
                        time.sleep(0.01)
                    results.append(pending.result(timeout=1))
                self.assertEqual(200, results[0][0])
                self.assertEqual(results[0], results[1])

    def prepare_planned_instance(self):
        from dotfactory.local_delivery import initialize
        from dotfactory.delivery import planned_receipt
        from test_verified_delivery import EditingRunner
        root = Path(self.temp.name)
        receipt = initialize(repository=str(root / "repository"), output=str(root / "planned"),
                             project="demo", linear_project="fixture-project")
        self.config_path = Path(receipt["config"])
        self.config = FactoryConfig.load(self.config_path)
        runner = EditingRunner()
        with FactoryRuntime(self.config, runner=runner) as runtime:
            self.execution = runtime.start_issue("demo", "LOCAL-HTTP-PLAN", description="Return Japanese greeting")
            from dotfactory.control import Principal
            runtime.control_service("demo").execute(
                self.execution, command_id="manual-plan", principal=Principal("reviewer", "approver", "test"),
                request={"action": "transition", "expected_state": "Todo", "parameters": {
                    "to_state": "Planning", "owner": "reviewer"}})
            runtime.run([self.execution], until_state="PlanReview")
            runtime.step()
            self.assertEqual(["Planning"], runner.calls)
            head = planned_receipt(runtime.ledger, self.execution)["receipt"]["source"]["head_sha"]
        return runner, head

    def test_http_operator_cannot_approve_planned_work(self):
        _runner, head = self.prepare_planned_instance()
        self.start("operator")
        status, _body = self.request("POST", f"/v1/runs/{self.execution}/commands", {
            "action": "approve", "expected_state": "PlanReview", "parameters": {"plan_sha": head},
        }, {"Idempotency-Key": "operator-approve", "X-Role": "approver"})
        self.assertEqual(403, status)
        with FactoryRuntime(self.config, control_only=True) as runtime:
            self.assertEqual("PlanReview", runtime.ledger.current(self.execution)["current_state_id"])

    def test_http_exact_plan_approval_survives_restart_and_reaches_review(self):
        from dotfactory.delivery import export_review
        runner, head = self.prepare_planned_instance()
        self.start("approver")
        path = f"/v1/runs/{self.execution}/commands"
        def approve(sha, command_id):
            return self.request("POST", path, {
                "action": "approve", "expected_state": "PlanReview",
                "parameters": {"plan_sha": sha, "note": "Fixture-only plan review"},
            }, {"Idempotency-Key": command_id})

        self.assertEqual(200, self.request(path=f"/v1/runs/{self.execution}")[0])
        self.assertEqual(400, approve("0" * 40, "wrong-plan")[0])
        # Forward approval to a running owner, keeping its thread the sole writer.
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runtime.enable_operator()
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(approve, head, "approve-plan")
                deadline = time.monotonic() + 4
                while not pending.done() and time.monotonic() < deadline:
                    runtime.operator_server.pump()
                    time.sleep(0.01)
                first = pending.result(timeout=1)
                self.assertEqual(200, first[0])
                self.assertEqual("Ready", runtime.ledger.current(self.execution)["current_state_id"])
        # Same request after owner shutdown returns its original durable receipt.
        self.assertEqual(first, approve(head, "approve-plan"))
        with FactoryRuntime(self.config, runner=runner) as runtime:
            runtime.run([self.execution], until_state="Review")
            self.assertEqual("Review", runtime.ledger.current(self.execution)["current_state_id"])
            export_review(runtime.ledger, self.execution, str(Path(self.temp.name) / "review"))
        proof = json.loads((Path(self.temp.name) / "review/review.json").read_text())["check"]
        self.assertTrue(proof["passed"])
        self.assertEqual(head, proof["approved_plan"]["head_sha"])
        self.assertEqual(["Planning", "Implementing", "Verifying"], runner.calls)
        self.assertEqual(200, self.request(path=f"/v1/runs/{self.execution}")[0])

    def test_ambiguous_transport_failure_never_falls_back(self):
        self.start("operator")
        for failure in (TimeoutError(), ConnectionResetError(), ValueError("unsafe socket")):
            with patch("dotfactory.control_server.send", side_effect=failure), patch("dotfactory.control_server.FactoryRuntime") as second:
                self.assertEqual(503, self.command()[0])
                second.assert_not_called()
        with patch("dotfactory.control_server.send", return_value={"ok": False}), patch("dotfactory.control_server.FactoryRuntime") as second:
            self.assertEqual(503, self.command()[0])
            second.assert_not_called()

    def test_owner_lock_without_socket_prevents_offline_writer(self):
        self.start()
        with FactoryRuntime(self.config, control_only=True):
            self.assertEqual(503, self.request()[0])

    def test_invalid_token_and_missing_ledger_do_not_initialize(self):
        principal = Principal("test", "viewer", "http")
        for token in ("", "short", "x" * 32 + "\n", "é" * 32):
            with self.assertRaises(ValueError):
                make_server(self.config, token, principal, 0)
        path = _ledger_path(self.config)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "ledger does not exist"):
            make_server(self.config, self.token, principal, 0)
        with self.assertRaisesRegex(RuntimeError, "ledger does not exist"):
            forward(self.config, {})
        self.assertFalse(path.exists())

    def test_cli_starts_and_sigterm_releases_port(self):
        process = subprocess.Popen([
            sys.executable, "-m", "dotfactory", "serve", "--config", str(self.config_path), "--port", "0",
        ], env={**os.environ, "DOTFACTORY_API_TOKEN": self.token}, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            import select
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "gateway did not start")
            receipt = process.stdout.readline()
            self.assertNotIn(self.token, receipt)
            self.port = int(json.loads(receipt)["url"].rsplit(":", 1)[1])
            self.assertEqual(200, self.request()[0])
            process.terminate()
            process.communicate(timeout=5)
            self.assertEqual(0, process.returncode)
            with self.assertRaises(OSError):
                self.request()
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
