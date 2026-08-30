import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory import (  # noqa: E402
    ControlError,
    ControlHTTPApp,
    ControlService,
    DurableKernel,
    ObservationService,
    Principal,
    SQLiteLedger,
)


class ControlAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "factory.db"
        self.ledger = SQLiteLedger(self.path)
        self.ledger.configure_factory("test-control")
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="linear-dotfactory",
        )
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")
        self.observation = ObservationService(self.ledger, self.kernel)
        self.control = ControlService(self.ledger, self.kernel)
        self.operator = Principal("operator", "operator", "codex_mobile")
        self.approver = Principal("operator", "approver", "codex_mobile")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def begin(self, identifier="TASK-568", command_id="begin"):
        return self.kernel.begin(
            "dotfactory", identifier, {"title": "mobile control", "api_token": "hidden"},
            command_id=command_id,
        )

    def move_to_review(self):
        execution = self.begin()
        planning = self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner", command_id="claim-plan",
        )
        self.kernel.transition(
            execution, "Ready", actor="agent", signal="agent_handoff",
            attempt_id=planning["attempt_id"], fence_token=planning["fence_token"],
            outcome="succeeded", evidence=[{"kind": "plan", "uri": "local://plan"}],
            command_id="finish-plan",
        )
        implementation = self.kernel.transition(
            execution, "Implementing", actor="agent", signal="listener_claim",
            owner="builder", command_id="claim-build",
        )
        verification = self.kernel.transition(
            execution, "Verifying", actor="agent", signal="agent_handoff",
            owner="verifier", attempt_id=implementation["attempt_id"],
            fence_token=implementation["fence_token"], outcome="succeeded",
            evidence=[{"kind": "commit", "uri": "git://commit"}],
            command_id="finish-build",
        )
        self.kernel.transition(
            execution, "Review", actor="agent", signal="agent_handoff",
            attempt_id=verification["attempt_id"],
            fence_token=verification["fence_token"], outcome="succeeded",
            evidence=[{"kind": "test", "uri": "local://tests"}],
            command_id="finish-verify",
        )
        return execution

    def test_cancel_is_idempotent_audited_and_reconciled(self):
        execution = self.begin()
        request = {
            "action": "cancel", "expected_state": "Todo", "confirmed": True,
            "parameters": {"reason": "No longer needed"},
        }
        first = self.control.execute(
            execution, command_id="mobile-cancel", principal=self.operator, request=request
        )
        second = self.control.execute(
            execution, command_id="mobile-cancel", principal=self.operator, request=request
        )
        self.assertEqual("completed", first["status"])
        self.assertEqual(first, second)
        self.assertEqual("Canceled", self.ledger.current(execution)["current_state_id"])
        self.assertEqual(
            [
                "control_command_received", "control_command_authorized",
                "control_command_completed", "control_reconciliation_queued",
            ],
            [item["event_type"] for item in first["events"]],
        )

    def test_command_id_cannot_be_reused_with_different_inputs(self):
        execution = self.begin()
        self.control.execute(
            execution, command_id="same", principal=self.operator,
            request={
                "action": "cancel", "expected_state": "Todo", "confirmed": True,
                "parameters": {},
            },
        )
        with self.assertRaisesRegex(ControlError, "different inputs"):
            self.control.execute(
                execution, command_id="same", principal=self.operator,
                request={
                    "action": "cancel", "expected_state": "Ready", "confirmed": True,
                    "parameters": {},
                },
            )

    def test_retry_recovers_transition_committed_before_receipt(self):
        execution = self.begin()
        request = {
            "action": "cancel", "expected_state": "Todo", "confirmed": True,
            "parameters": {"reason": "Stop after commit"},
        }
        original = self.ledger.finish_control_command
        with mock.patch.object(
            self.ledger, "finish_control_command", side_effect=RuntimeError("process died")
        ):
            with self.assertRaisesRegex(RuntimeError, "process died"):
                self.control.execute(
                    execution, command_id="commit-before-receipt",
                    principal=self.operator, request=request,
                )
        self.assertEqual("Canceled", self.ledger.current(execution)["current_state_id"])
        self.assertEqual(
            "authorized", self.ledger.control_command("commit-before-receipt")["status"]
        )
        self.ledger.finish_control_command = original
        recovered = self.control.execute(
            execution, command_id="commit-before-receipt",
            principal=self.operator, request=request,
        )
        self.assertEqual("completed", recovered["status"])
        self.assertTrue(recovered["result"]["recovered"])

    def test_denied_command_is_durable_and_does_not_move_state(self):
        execution = self.begin()
        receipt = self.control.execute(
            execution, command_id="viewer-cancel",
            principal=Principal("reader", "viewer", "codex_mobile"),
            request={
                "action": "cancel", "expected_state": "Todo", "confirmed": True,
                "parameters": {},
            },
        )
        self.assertEqual("denied", receipt["status"])
        self.assertEqual("Todo", self.ledger.current(execution)["current_state_id"])
        self.assertEqual("denied", receipt["authorization_decision"])

    def test_approval_requires_approver_and_records_feedback(self):
        execution = self.move_to_review()
        denied = self.control.execute(
            execution, command_id="operator-approve", principal=self.operator,
            request={
                "action": "approve", "expected_state": "Review",
                "parameters": {"note": "Ship it"},
            },
        )
        self.assertEqual("denied", denied["status"])
        bypass = self.control.execute(
            execution, command_id="operator-transition-done", principal=self.operator,
            request={
                "action": "transition", "expected_state": "Review", "confirmed": True,
                "parameters": {
                    "to_state": "Done",
                    "feedback": [{
                        "source": "control_api", "kind": "approval", "author": "operator",
                        "body": "Attempted bypass", "url": "control://bypass",
                    }],
                },
            },
        )
        self.assertEqual("denied", bypass["status"])
        approved = self.control.execute(
            execution, command_id="approver-approve", principal=self.approver,
            request={
                "action": "approve", "expected_state": "Review",
                "parameters": {"note": "Evidence reviewed; ship it."},
            },
        )
        self.assertEqual("completed", approved["status"])
        history = self.ledger.run_history(execution)
        self.assertEqual("Done", history["execution"]["current_state_id"])
        self.assertEqual("approval", history["feedback"][-1]["body"]["kind"])

    def test_retry_derives_the_prior_work_state(self):
        execution = self.begin()
        planning = self.kernel.transition(
            execution, "Planning", actor="human", signal="linear_status_change",
            owner="planner", command_id="claim",
        )
        investigation = self.kernel.transition(
            execution, "Investigating", actor="human", signal="linear_status_change",
            owner="investigator", attempt_id=planning["attempt_id"],
            fence_token=planning["fence_token"], outcome="failed",
            evidence=[{"kind": "error", "uri": "local://error"}], command_id="fail",
        )
        retried = self.control.execute(
            execution, command_id="retry-plan", principal=self.operator,
            request={
                "action": "retry", "expected_state": "Investigating", "confirmed": True,
                "parameters": {"owner": "planner-2", "reason": "Dependency restored"},
            },
        )
        self.assertEqual("completed", retried["status"])
        current = self.ledger.current(execution)
        self.assertEqual("Planning", current["current_state_id"])
        self.assertNotEqual(investigation["attempt_id"], current["attempt"]["id"])

    def test_generic_transition_uses_explicit_control_signal(self):
        execution = self.begin()
        receipt = self.control.execute(
            execution, command_id="start-human-plan", principal=self.operator,
            request={
                "action": "transition", "expected_state": "Todo",
                "parameters": {"to_state": "Planning", "owner": "operator"},
            },
        )
        self.assertEqual("completed", receipt["status"])
        decision = self.ledger.decision_for_command(
            f"execution:{execution}:transition:control:start-human-plan"
        )
        self.assertEqual("control_command", decision["signal"])

    def test_stale_state_is_recorded_as_failed(self):
        execution = self.begin()
        with self.assertRaisesRegex(ControlError, "expected Ready, found Todo"):
            self.control.execute(
                execution, command_id="stale", principal=self.operator,
                request={
                    "action": "transition", "expected_state": "Ready",
                    "parameters": {"to_state": "Implementing", "owner": "builder"},
                },
            )
        self.assertEqual("failed", self.ledger.control_command("stale")["status"])

    def test_read_models_are_bounded_redacted_and_hide_fences(self):
        first = self.begin(identifier="TASK-568", command_id="first")
        second = self.begin(identifier="TASK-569", command_id="second")
        claim = self.kernel.transition(
            first, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner", command_id="claim",
        )
        self.ledger.acquire_resource(
            "port:8080", attempt_id=claim["attempt_id"],
            fence_token=claim["fence_token"], expires_at="2099-01-01T00:00:00+00:00",
            idempotency_key="lease-port",
        )
        page = self.observation.runs(limit=1)
        self.assertEqual(1, len(page["data"]))
        self.assertIsNotNone(page["next_cursor"])
        next_page = self.observation.runs(limit=1, cursor=page["next_cursor"])
        self.assertNotEqual(page["data"][0]["id"], next_page["data"][0]["id"])
        detail = self.observation.run(first)
        resources = self.observation.resources(execution_id=first)
        rendered = json.dumps({"detail": detail, "resources": resources})
        self.assertNotIn("fence_token", rendered)
        self.assertNotIn("hidden", rendered)
        self.assertEqual("[REDACTED]", detail["data"]["intent"]["api_token"])
        self.assertEqual(second, page["data"][0]["id"])

    def test_http_adapter_requires_auth_and_returns_command_receipt(self):
        execution = self.begin()
        app = ControlHTTPApp(
            self.observation, self.control,
            lambda environ: self.operator if environ.get("HTTP_AUTHORIZATION") else None,
        )
        status, payload = self.call_wsgi(app, "GET", "/v1/overview")
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", payload["error"]["code"])
        status, payload = self.call_wsgi(
            app, "POST", f"/v1/runs/{execution}/commands",
            headers={"AUTHORIZATION": "Bearer test", "IDEMPOTENCY_KEY": "http-cancel"},
            body={
                "action": "cancel", "expected_state": "Todo", "confirmed": True,
                "parameters": {"reason": "Stop"},
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("completed", payload["data"]["status"])
        status, payload = self.call_wsgi(
            app, "GET", "/v1/commands/http-cancel",
            headers={"AUTHORIZATION": "Bearer test"},
        )
        self.assertEqual(200, status)
        self.assertEqual("http-cancel", payload["data"]["command_id"])

    def test_schema_four_migrates_control_journal(self):
        self.ledger.close()
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE control_command_events")
        connection.execute("DROP TABLE control_commands")
        connection.execute("PRAGMA user_version=4")
        connection.commit()
        connection.close()
        self.ledger = SQLiteLedger(self.path)
        tables = {
            row[0] for row in self.ledger.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("control_commands", tables)
        self.assertIn("control_command_events", tables)
        self.assertEqual(9, self.ledger.connection.execute("PRAGMA user_version").fetchone()[0])

    def call_wsgi(self, app, method, path, headers=None, body=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        environ = {
            "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
        }
        for key, value in (headers or {}).items():
            environ[f"HTTP_{key}"] = value
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = response_headers

        response = b"".join(app(environ, start_response))
        return captured["status"], json.loads(response)


if __name__ == "__main__":
    unittest.main()
