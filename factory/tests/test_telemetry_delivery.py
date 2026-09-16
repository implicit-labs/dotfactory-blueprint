import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from dotfactory import DurableKernel, SQLiteLedger
from dotfactory.observability import ProjectionReceiptV1, TraceRecordV1, stable_span_id, stable_trace_id
from dotfactory.telemetry import (
    LogfireProjectionWorker, LogfireSettings,
    TelemetryProjectionError, _http_transport,
)


ROOT = Path(__file__).resolve().parents[1]


def spans(body):
    return json.loads(body)["resourceSpans"][0]["scopeSpans"][0]["spans"]


def attributes(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


class DurableTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "factory.db"
        self.ledger = SQLiteLedger(self.path)
        self.ledger.configure_factory("durable-telemetry")
        self.ledger.register_project("dotfactory", display_name="dotfactory",
                                     tracker_kind="linear", tracker_project_id="project-test")
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")
        self.execution = self.kernel.begin("dotfactory", "DEMO-1", {}, command_id="begin")
        self.settings = LogfireSettings(endpoint="https://logfire-us.pydantic.dev", project="example/dotfactory", headers="Authorization=secret-sentinel")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def append(self, source_id, *, execution=None):
        execution = execution or self.execution
        with self.ledger.transaction() as db:
            self.ledger._insert_trace_record(db, TraceRecordV1(
                record_id=f"tr-test-{source_id}", source_kind="test", source_id=str(source_id),
                execution_id=execution, entity_kind="execution", entity_id=execution,
                trace_id=stable_trace_id(execution), span_id=stable_span_id("test", str(source_id)),
                record_kind="event", domain="test", phase="observed", name="test_observation",
                status="observed", origin="test", trust_class="trusted-runtime", observed_at=self.ledger.clock(),
            ))

    def worker(self, transport, **options):
        return LogfireProjectionWorker(self.ledger, self.settings, transport=transport, **options)

    def test_unknown_ack_reuses_saved_bytes_after_restart_settings_and_source_change(self):
        calls = []
        def unknown(_endpoint, _headers, body, _timeout):
            calls.append(body)
            raise TelemetryProjectionError("LOGFIRE_TRANSPORT_UNKNOWN", "secret-sentinel", retryable=True, ambiguous=True)
        result = self.worker(unknown).publish(command_id="unknown")
        through = result["through_source_seq"]
        frozen = self.ledger.connection.execute("SELECT body_json FROM otlp_batches").fetchone()[0]
        self.append("late")
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        self.settings = LogfireSettings(endpoint="https://logfire-us.pydantic.dev", project="example/dotfactory", headers="Authorization=new-secret", service_name="changed")
        result = self.worker(lambda _e, _h, body, _t: calls.append(body) or {}, batch_size=1).publish(command_id="unknown")
        self.assertEqual("completed", result["status"])
        self.assertEqual(through, result["through_source_seq"])
        self.assertEqual([frozen.encode(), frozen.encode()], calls)
        self.assertEqual(through, result["accepted_count"])
        self.assertNotIn("secret-sentinel", self.ledger.connection.execute("SELECT outcome_json FROM otlp_deliveries LIMIT 1").fetchone()[0])

    def test_mid_receipt_crash_rolls_back_batch_and_all_coverage(self):
        self.append("second")
        calls = []
        worker = self.worker(lambda _e, _h, body, _t: calls.append(body) or {})
        def fault(boundary):
            if boundary == "after_projection_receipt_recorded":
                raise RuntimeError("receipt crash")
        self.ledger.fault_hook = fault
        with self.assertRaisesRegex(RuntimeError, "receipt crash"):
            worker.publish(command_id="crash")
        self.assertEqual(0, self.ledger.connection.execute("SELECT COUNT(*) FROM projection_receipts").fetchone()[0])
        self.assertEqual(0, self.ledger.connection.execute("SELECT COUNT(*) FROM otlp_spans WHERE accepted=1").fetchone()[0])
        self.assertEqual("in_flight", self.ledger.connection.execute("SELECT status FROM otlp_batches").fetchone()[0])
        self.ledger.fault_hook = None
        result = worker.publish(command_id="crash")
        self.assertEqual("completed", result["status"])
        self.assertEqual(calls[0], calls[1])

    def test_partial_rejection_is_aggregate_and_never_retried(self):
        calls = []
        worker = self.worker(lambda _e, _h, body, _t: calls.append(body) or {"partialSuccess": {"rejectedSpans": "1", "errorMessage": "secret-sentinel"}})
        result = worker.publish(command_id="partial")
        count = len(spans(calls[0]))
        self.assertEqual(0, result["accepted_count"])
        self.assertEqual("blocked", result["delivery"]["status"])
        self.assertEqual(count - 1, result["delivery"]["outcome"]["accepted_spans"])
        self.assertTrue(result["delivery"]["outcome"]["ambiguous"])
        self.assertFalse(result["delivery"]["outcome"]["retryable"])
        self.assertNotIn("secret-sentinel", json.dumps(result))
        self.append("later")
        for _ in range(3):
            self.assertEqual("blocked", worker.drain()["delivery"]["status"])
            worker.publish(command_id="partial")
        self.assertEqual(1, len(calls))

    def test_warning_only_partial_success_is_accepted_once(self):
        calls = []
        worker = self.worker(lambda _e, _h, body, _t: calls.append(body) or {"partialSuccess": {"rejectedSpans": "0", "errorMessage": "warning"}})
        result = worker.publish(command_id="warning")
        self.assertEqual("completed", result["status"])
        worker.publish(command_id="warning")
        self.assertEqual(1, len(calls))
        self.assertTrue(json.loads(self.ledger.connection.execute("SELECT outcome_json FROM otlp_batches").fetchone()[0])["warning"])

    def test_invalid_source_blocks_locally_without_breaking_execution(self):
        self.ledger.connection.execute("UPDATE trace_records SET observed_at='invalid-timestamp'")
        worker = self.worker(lambda *_: self.fail("invalid source must not send"))
        result = worker.drain()
        self.assertEqual("blocked", result["delivery"]["status"])
        self.assertEqual("LOGFIRE_SOURCE_INVALID", result["delivery"]["outcome"]["code"])
        self.assertEqual("blocked", worker.drain()["delivery"]["status"])
        self.assertEqual(0, self.ledger.connection.execute("SELECT COUNT(*) FROM otlp_plans").fetchone()[0])
        self.assertEqual(1, self.ledger.connection.execute("SELECT COUNT(*) FROM workflow_executions").fetchone()[0])

    def test_permanent_failure_blocks_repeated_drain(self):
        calls = []
        def reject(_e, _h, body, _t):
            calls.append(body)
            raise TelemetryProjectionError("LOGFIRE_HTTP_401", "rejected", retryable=False)
        worker = self.worker(reject)
        self.assertEqual("blocked", worker.drain()["delivery"]["status"])
        worker.drain()
        self.assertEqual(1, len(calls))
        self.assertEqual(0, self.ledger.connection.execute("SELECT COUNT(*) FROM projection_receipts").fetchone()[0])

    def test_only_otlp_retryable_http_statuses_retry(self):
        for status in (400, 401, 408, 429, 500, 501, 502, 503, 504):
            with self.subTest(status=status), patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("https://example.invalid", status, "error", {}, None)):
                with self.assertRaises(TelemetryProjectionError) as context:
                    _http_transport("https://example.invalid", {}, b"{}", 1)
                self.assertEqual(status in (429, 502, 503, 504), context.exception.retryable)

    def test_new_ranges_reuse_frozen_anchors_without_resending(self):
        calls = []
        worker = self.worker(lambda _e, _h, body, _t: calls.append(body) or {}, batch_size=1)
        first = worker.drain()
        first_spans = [span for body in calls for span in spans(body)]
        anchor = next(span for span in first_spans if attributes(span).get("dotfactory.structural"))
        calls.clear()
        self.append("late")
        result = worker.drain()
        self.assertEqual("completed", result["status"])
        outgoing = [span for body in calls for span in spans(body)]
        self.assertEqual(1, len(outgoing))
        self.assertEqual(anchor["spanId"], outgoing[0]["parentSpanId"])
        self.assertEqual(anchor, json.loads(self.ledger.connection.execute("SELECT body_json FROM otlp_spans WHERE span_id=?", (anchor["spanId"],)).fetchone()[0]))
        self.assertEqual(1, result["accepted_count"])
        self.assertIsNone(worker.drain())
        self.assertEqual(1, first["accepted_count"])

    def test_legacy_receipts_do_not_satisfy_v2_coverage(self):
        legacy = "logfire:example/dotfactory:us"
        attempt = self.ledger.start_projection_attempt(legacy, command_id="legacy", idempotency_key="legacy")
        record = self.ledger.trace_page(self.execution)[0]
        self.ledger.record_projection_receipt(ProjectionReceiptV1(receipt_id="legacy", attempt_id=attempt["id"], destination=legacy,
            source_record_id=record["record_id"], status="accepted", idempotency_key="legacy", recorded_at=self.ledger.clock()))
        self.ledger.advance_projection_watermark(attempt["id"], through_source_seq=attempt["through_source_seq"])
        calls = []
        result = self.worker(lambda _e, _h, body, _t: calls.append(body) or {}).drain()
        self.assertEqual(self.settings.destination, result["destination"])
        self.assertEqual(1, result["from_source_seq"])
        self.assertIn(record["record_id"], {attributes(span).get("dotfactory.record.id") for body in calls for span in spans(body)})

    def test_configured_projects_have_independent_v2_coverage(self):
        calls = []
        first = self.worker(lambda _e, _h, body, _t: calls.append(body) or {}).drain()
        first_bodies = list(calls)
        calls.clear()
        other = LogfireSettings(
            endpoint="https://logfire-eu.pydantic.dev", project="example/other",
            region="eu", headers="Authorization=other-sentinel",
        )
        worker = LogfireProjectionWorker(
            self.ledger, other,
            transport=lambda _e, _h, body, _t: calls.append(body) or {},
        )
        second = worker.drain()
        self.assertNotEqual(first["destination"], second["destination"])
        self.assertEqual("logfire:example/other:eu:otel-v2", second["destination"])
        self.assertEqual(first["accepted_count"], second["accepted_count"])
        self.assertEqual(
            [span["spanId"] for body in first_bodies for span in spans(body)],
            [span["spanId"] for body in calls for span in spans(body)],
        )
        resource = json.loads(calls[0])["resourceSpans"][0]["resource"]
        self.assertIn({"key": "dotfactory.projection.destination", "value": {
            "stringValue": other.destination,
        }}, resource["attributes"])
        self.assertIsNone(worker.drain())

    def test_interleaved_executions_have_independent_roots_and_bounded_batches(self):
        other = self.kernel.begin("dotfactory", "DEMO-2", {}, command_id="begin-other")
        self.append("a")
        self.append("b", execution=other)
        calls = []
        result = self.worker(lambda _e, _h, body, _t: calls.append(body) or {}, batch_size=1).drain()
        all_spans = [span for body in calls for span in spans(body)]
        by_id = {span["spanId"]: span for span in all_spans}
        self.assertEqual(len(all_spans), len(by_id))
        self.assertEqual(2, sum("parentSpanId" not in span for span in all_spans))
        for span in all_spans:
            if span.get("parentSpanId"):
                self.assertEqual(span["traceId"], by_id[span["parentSpanId"]]["traceId"])
        self.assertTrue(all(len(spans(body)) == 1 for body in calls))
        self.assertEqual(4, result["accepted_count"])

    def test_cross_worker_lock_prevents_concurrent_send_and_releases_after_failure(self):
        other = SQLiteLedger(self.path)
        other_worker = LogfireProjectionWorker(other, self.settings, transport=lambda *_: self.fail("concurrent send"))
        try:
            def transport(*_):
                result = other_worker.publish(command_id="same")
                self.assertEqual("busy", result["delivery"]["status"])
                return {}
            result = self.worker(transport).publish(command_id="same")
            self.assertEqual("completed", result["status"])
            self.assertEqual("completed", other_worker.publish(command_id="same")["status"])
        finally:
            other.close()

    def test_actual_span_limit_survives_more_than_one_raw_page(self):
        for index in range(1002):
            self.append(index)
        calls = []
        result = self.worker(lambda _e, _h, body, _t: calls.append(body) or {}, batch_size=73).drain()
        self.assertEqual(1003, result["accepted_count"])
        self.assertTrue(all(0 < len(spans(body)) <= 73 for body in calls))
        self.assertEqual(1004, sum(len(spans(body)) for body in calls))

    def test_high_manual_range_does_not_advance_past_missing_early_sources(self):
        self.append("late")
        worker = self.worker(lambda *_: {})
        worker.publish(command_id="high", from_trace_seq=2, through_trace_seq=2)
        self.assertEqual(0, self.ledger.connection.execute("SELECT through_source_seq FROM projection_watermarks WHERE destination=?", (self.settings.destination,)).fetchone()[0])
        result = worker.drain()
        self.assertEqual("completed", result["status"])
        self.assertEqual(2, result["accepted_count"])

    def test_failed_scenario_has_complete_ownership_and_unique_failure_observations(self):
        from test_delivery_scenarios import DeliveryScenarioTests
        fixture = DeliveryScenarioTests()
        fixture.setUp()
        try:
            fixture.run_scenario(fail=True)
            calls = []
            result = LogfireProjectionWorker(fixture.ledger, self.settings, batch_size=1,
                transport=lambda _e, _h, body, _t: calls.append(body) or {}).drain()
            all_spans = [span for body in calls for span in spans(body)]
            by_id = {span["spanId"]: span for span in all_spans}
            self.assertEqual(len(all_spans), len(by_id))
            roots = [span for span in all_spans if "parentSpanId" not in span]
            self.assertEqual(1, len(roots))
            self.assertEqual("dotfactory.execution", roots[0]["name"])
            for span in all_spans:
                current, seen = span, set()
                while current.get("parentSpanId"):
                    self.assertNotIn(current["spanId"], seen)
                    seen.add(current["spanId"])
                    current = by_id[current["parentSpanId"]]
            runner = next(span for span in all_spans if span["name"] == "dotfactory.runner_run")
            lineage = []
            current = runner
            while current:
                lineage.append(current["name"])
                current = by_id.get(current.get("parentSpanId"))
            self.assertEqual(["dotfactory.runner_run", "dotfactory.preparation", "dotfactory.attempt",
                              "dotfactory.state_run", "dotfactory.execution"], lineage)
            tool = next(span for span in all_spans if span["name"] == "dotfactory.runner_operation")
            self.assertEqual(runner["spanId"], tool["parentSpanId"])
            failures = [span for span in all_spans if span["status"]["code"] == 2]
            self.assertGreaterEqual(len(failures), 2)
            self.assertTrue(any(attributes(span).get("dotfactory.error.code") == "SCENARIO_RUNNER_FAILED" for span in failures))
            self.assertNotIn("The scenario runner failed", json.dumps(all_spans))
            anchors = [span for span in all_spans if attributes(span)["dotfactory.structural"]]
            self.assertTrue(all(span["status"]["code"] == 0 and span["startTimeUnixNano"] == span["endTimeUnixNano"] for span in anchors))
            source_ids = {row[0] for row in fixture.ledger.connection.execute("SELECT record_id FROM trace_records")}
            self.assertEqual(source_ids, {attributes(span)["dotfactory.record.id"] for span in all_spans if not attributes(span)["dotfactory.structural"]})
            self.assertEqual(len(source_ids), result["accepted_count"])
            self.assertTrue(all(len(spans(body)) == 1 for body in calls))
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
