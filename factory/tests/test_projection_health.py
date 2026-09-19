import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dotfactory import DurableKernel, ObservationService, SQLiteLedger
from dotfactory.cli import _demo_config
from dotfactory.instance import FactoryConfig
from dotfactory.lifecycle import FactoryRuntime, fixture_runner
from dotfactory.linear_agent import LinearAgentSessionWorker
from dotfactory.projection_health import projection_health
from dotfactory.telemetry import LogfireProjectionWorker, LogfireSettings, TelemetryProjectionError


ROOT = Path(__file__).resolve().parents[1]


class ProjectionHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.temp.name) / "health.db")
        self.ledger.configure_factory("health")
        self.ledger.register_project("test", display_name="Test", tracker_kind="linear", tracker_project_id="test")
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows/default.dot")
        self.execution = self.kernel.begin("test", "TEST-1", {}, command_id="begin")
        self.ledger.projection_configuration = {"linear": True, "logfire": False, "linear_agent": False}
        self.ledger.projection_configuration["logfire_destination"] = "logfire:example/project:us:otel-v2"

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def channels(self, execution=None):
        return {(row["destination"], row["projection_type"]): row for row in
                projection_health(self.ledger, execution)["channels"]}

    def evidence(self, execution):
        body = "Controlled evidence"
        self.ledger.stage_linear_evidence(execution, issue_id=execution, body=body,
                                         digest=hashlib.sha256(body.encode()).hexdigest())

    def test_disabled_logfire_does_not_mask_confirmed_linear_status(self):
        self.ledger.connection.execute("UPDATE linear_mutations SET status='confirmed',confirmed_at=?", (self.ledger.clock(),))
        values = self.channels(self.execution)
        self.assertEqual("healthy", values["linear", "status"]["status"])
        self.assertIsNotNone(values["linear", "status"]["last_confirmed_receipt"])
        trace = values["logfire", "trace"]
        self.assertEqual("disabled", trace["status"])
        self.assertGreater(trace["pending"], 0)
        self.assertIsNotNone(trace["oldest_pending_age_seconds"])
        self.assertEqual("disabled", values["linear", "legacy_event"]["status"])
        overview = ObservationService(self.ledger, self.kernel).overview()["data"]
        run = ObservationService(self.ledger, self.kernel).run(self.execution)["data"]
        self.assertIn("projection_health", overview)
        self.assertIn("pending_projection_count", run)
        self.assertIn("projection_health", run)

    def test_retry_ambiguity_failure_and_run_isolation(self):
        self.evidence(self.execution)
        other = self.kernel.begin("test", "TEST-2", {}, command_id="other")
        self.evidence(other)
        self.ledger.confirm_linear_evidence(other)
        for ambiguous, terminal, expected in [(False, False, "retry"), (True, False, "ambiguous"), (False, True, "failed")]:
            with self.subTest(expected=expected):
                self.ledger.mark_linear_evidence_error(self.execution,
                    {"code": "LINEAR_TEST_FAILURE", "message": "sensitive response body", "token": "secret-value"},
                    ambiguous=ambiguous, terminal=terminal)
                row = self.channels(self.execution)["linear", "evidence_comment"]
                self.assertEqual(expected, row["status"])
                self.assertEqual(1, row[expected])
                self.assertEqual(0, row["confirmed"])
                self.assertNotIn("sensitive response body", json.dumps(row))
                self.assertNotIn("secret-value", json.dumps(row))
                self.assertEqual("healthy", self.channels(other)["linear", "evidence_comment"]["status"])
        self.ledger.confirm_linear_evidence(self.execution)
        row = self.channels(self.execution)["linear", "evidence_comment"]
        self.assertEqual("healthy", row["status"])
        self.assertIsNone(row["safe_error"])

    def test_unconfigured_reader_is_unknown_not_disabled(self):
        del self.ledger.projection_configuration
        self.assertIsNone(self.channels()["linear", "status"]["enabled"])
        self.assertEqual("unknown", self.channels()["logfire", "trace"]["status"])

    def test_native_session_active_and_fallback_are_not_pending(self):
        self.ledger.projection_configuration["linear_agent"] = True
        worker = LinearAgentSessionWorker(self.ledger, None)
        marker = "https://example.invalid/session"
        worker._stage_session(self.execution, issue_id="fixture", marker_url=marker,
                              external_urls=[{"url": marker, "label": "Run"}])
        for state, expected in [("active", "healthy"), ("fallback", "fallback")]:
            self.ledger.connection.execute(
                "UPDATE linear_agent_sessions SET status=?,confirmed_at=? WHERE execution_id=?",
                (state, self.ledger.clock(), self.execution),
            )
            row = self.channels(self.execution)["linear", "agent_session"]
            self.assertEqual(expected, row["status"])
            self.assertEqual(0, row["pending"])

    def test_lifecycle_receipt_contains_configured_health(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = FactoryConfig.load(_demo_config(Path(temporary)))
            with FactoryRuntime(config, runner=fixture_runner()) as runtime:
                execution = runtime.start_issue("demo", "DEMO-1")
                receipt = runtime.run([execution], until_state="Review")
                health = receipt.executions[0]["projection_health"]
                self.assertEqual(1, health["schema_version"])
                self.assertTrue(all(row["enabled"] is False for row in health["channels"]))

    def test_logfire_delivery_outcomes_and_confirmation(self):
        self.ledger.projection_configuration["logfire"] = True
        settings = LogfireSettings(endpoint="https://logfire-us.pydantic.dev", headers="Authorization=unused",
                                   service_name="test", project="example/project")
        def failed(*args):
            raise TelemetryProjectionError("LOGFIRE_TEST_TIMEOUT", "not exported", retryable=True, ambiguous=True)
        worker = LogfireProjectionWorker(self.ledger, settings, transport=failed)
        worker.publish(command_id="health-test")
        row = self.channels(self.execution)["logfire", "trace"]
        self.assertEqual("ambiguous", row["status"])
        self.assertGreater(row["ambiguous"], 0)
        self.assertEqual("LOGFIRE_TEST_TIMEOUT", row["safe_error"]["code"])
        ambiguous_count = row["ambiguous"]
        worker.publish(command_id="competing-unfrozen-attempt")
        row = self.channels(self.execution)["logfire", "trace"]
        self.assertEqual("ambiguous", row["status"])
        self.assertEqual(ambiguous_count, row["ambiguous"])
        worker.transport = lambda *args: {}
        worker.publish(command_id="health-test")
        row = self.channels(self.execution)["logfire", "trace"]
        self.assertEqual(0, row["ambiguous"])
        self.assertGreater(row["confirmed"], 0)
        self.assertIsNotNone(row["last_confirmed_receipt"])

    def test_health_reads_do_not_mutate_and_preserve_previous_confirmation(self):
        self.evidence(self.execution)
        self.ledger.confirm_linear_evidence(self.execution)
        self.ledger.mark_linear_evidence_error(self.execution, {"code": "RETRY_LATER"},
                                              ambiguous=False, terminal=False)
        before = self.ledger.connection.total_changes
        row = self.channels(self.execution)["linear", "evidence_comment"]
        self.assertEqual("retry", row["status"])
        self.assertIsNotNone(row["last_confirmed_receipt"])
        self.assertEqual(before, self.ledger.connection.total_changes)

    def test_logfire_confirmation_is_scoped_to_configured_destination(self):
        self.ledger.projection_configuration["logfire"] = True
        settings = LogfireSettings(endpoint="https://logfire-us.pydantic.dev", headers="Authorization=unused",
                                   service_name="test", project="other/project")
        LogfireProjectionWorker(self.ledger, settings, transport=lambda *args: {}).publish(command_id="other")
        row = self.channels(self.execution)["logfire", "trace"]
        self.assertEqual(0, row["confirmed"])
        self.assertIsNone(row["last_confirmed_receipt"])
        self.ledger.projection_configuration["logfire_destination"] = settings.destination
        row = self.channels(self.execution)["logfire", "trace"]
        self.assertGreater(row["confirmed"], 0)
        del self.ledger.projection_configuration
        row = self.channels(self.execution)["logfire", "trace"]
        self.assertEqual("unknown", row["status"])
        self.assertIsNone(row["last_confirmed_receipt"])
