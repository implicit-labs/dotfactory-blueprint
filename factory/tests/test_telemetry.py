import json
import tempfile
import unittest
from pathlib import Path

from dotfactory import DurableKernel, FactoryConfig, FactoryConfigError, SQLiteLedger
from dotfactory.cli import _demo_config
from dotfactory.telemetry import (
    LogfireProjectionWorker, LogfireSettings, TelemetryProjectionError,
    _unix_nano, otel_trace_document,
)


ROOT = Path(__file__).resolve().parents[1]


class TelemetryProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.temp.name) / "factory.db")
        self.ledger.configure_factory("telemetry-test")
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="project-dotfactory",
        )
        self.kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "default.dot"
        )
        self.execution = self.kernel.begin(
            "dotfactory", "TASK-571", {"title": "telemetry"},
            command_id="begin-telemetry",
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def settings(self):
        return LogfireSettings(
            endpoint="https://logfire-us.pydantic.dev",
            headers="Authorization=write-token-sentinel",
            project="example/dotfactory",
        )

    def test_mapping_is_deterministic_payload_free_otlp_json(self):
        records = self.ledger.trace_page(self.execution, limit=1000)
        first = otel_trace_document(records, service_name="dotfactory")
        second = otel_trace_document(records, service_name="dotfactory")
        self.assertEqual(first, second)
        encoded = json.dumps(first, sort_keys=True)
        self.assertIn("resourceSpans", encoded)
        self.assertIn(records[0]["record_id"], encoded)
        self.assertNotIn("payload", encoded)
        self.assertNotIn("write-token-sentinel", encoded)

    def test_success_records_receipts_and_fixed_range_watermark(self):
        calls = []

        def transport(endpoint, headers, body, timeout):
            calls.append((endpoint, headers, json.loads(body), timeout))
            return {}

        result = LogfireProjectionWorker(
            self.ledger, self.settings(), transport=transport,
        ).publish(command_id="publish-success")
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, len(calls))
        self.assertEqual(
            "https://logfire-us.pydantic.dev/v1/traces", calls[0][0]
        )
        self.assertEqual("application/json", calls[0][1]["content-type"])
        receipt_count = self.ledger.connection.execute(
            "SELECT COUNT(*) FROM projection_receipts WHERE destination=?",
            (self.settings().destination,),
        ).fetchone()[0]
        self.assertEqual(result["accepted_count"], receipt_count)
        stored = json.dumps([
            dict(row) for row in self.ledger.connection.execute(
                "SELECT * FROM projection_receipts"
            )
        ], sort_keys=True)
        self.assertNotIn("write-token-sentinel", stored)

    def test_large_ranges_are_drained_in_bounded_batches(self):
        source_count = self.ledger.connection.execute(
            "SELECT COUNT(*) FROM trace_records"
        ).fetchone()[0]
        calls = []

        def transport(_endpoint, _headers, body, _timeout):
            calls.append(len(json.loads(body)["resourceSpans"][0][
                "scopeSpans"
            ][0]["spans"]))
            return {}

        result = LogfireProjectionWorker(
            self.ledger, self.settings(), transport=transport, batch_size=1,
        ).publish(command_id="publish-batched")
        self.assertEqual("completed", result["status"])
        self.assertTrue(all(count == 1 for count in calls))
        self.assertGreater(len(calls), source_count)
        self.assertEqual(source_count, result["accepted_count"])

    def test_nanosecond_mapping_does_not_use_float_timestamps(self):
        self.assertEqual(
            "1704067200123456000", _unix_nano("2024-01-01T00:00:00.123456Z")
        )

    def test_unknown_failure_is_redacted_and_same_attempt_can_resume(self):
        outcomes = [TelemetryProjectionError(
            "LOGFIRE_TRANSPORT_UNKNOWN", "delivery unknown",
            retryable=True, ambiguous=True,
        ), {}]

        def transport(_endpoint, _headers, _body, _timeout):
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        worker = LogfireProjectionWorker(
            self.ledger, self.settings(), transport=transport,
        )
        failed = worker.publish(command_id="publish-resume")
        self.assertEqual("paused", failed["status"])
        self.assertEqual(0, failed["rejected_count"])
        detail = failed["delivery"]["outcome"]
        self.assertEqual("write_token", detail["credential_kind"])
        self.assertEqual("otlp_trace_write", detail["required_purpose"])
        resumed = worker.publish(command_id="publish-resume")
        self.assertEqual("completed", resumed["status"])
        self.assertEqual(0, len(outcomes))

    def test_missing_project_endpoint_and_credential_purpose_fail_before_io(self):
        with self.assertRaisesRegex(TelemetryProjectionError, "project identity"):
            LogfireSettings(
                endpoint="https://logfire-us.pydantic.dev",
                headers="Authorization=value", project="",
            )
        with self.assertRaisesRegex(TelemetryProjectionError, "configured region"):
            LogfireSettings(
                endpoint="https://logfire-eu.pydantic.dev",
                headers="Authorization=value", project="example/dotfactory",
                region="us",
            )
        with self.assertRaisesRegex(TelemetryProjectionError, "write-token"):
            LogfireSettings(
                endpoint="https://logfire-us.pydantic.dev", headers="x-api-key=value",
                project="example/dotfactory",
            )

    def test_config_keeps_credentials_in_runtime_environment_only(self):
        config_root = Path(self.temp.name) / "config"
        config_root.mkdir()
        path = _demo_config(config_root)
        values = json.loads(path.read_text(encoding="utf-8"))
        values["projections"]["logfire"] = {
            "enabled": True,
            "endpoint_env": "TEST_OTLP_ENDPOINT",
            "headers_env": "TEST_OTLP_HEADERS",
            "service_name_env": "TEST_OTEL_SERVICE",
            "project": "example/dotfactory", "region": "us",
            "dataset_enabled": False,
        }
        path.write_text(json.dumps(values), encoding="utf-8")
        config = FactoryConfig.load(path)
        with self.assertRaisesRegex(FactoryConfigError, "TEST_OTLP_ENDPOINT"):
            config.resolve_logfire_projection(environment={})
        resolved = config.resolve_logfire_projection(environment={
            "TEST_OTLP_ENDPOINT": "https://logfire-us.pydantic.dev",
            "TEST_OTLP_HEADERS": "Authorization=write-secret",
        })
        self.assertEqual("dotfactory", resolved["service_name"])
        self.assertNotIn("write-secret", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
