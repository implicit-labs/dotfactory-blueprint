import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory import (  # noqa: E402
    DurableKernel, ObservationService, SQLiteLedger,
    execution_waterfall, readable_error_groups, render_waterfall_html,
    summary_fact,
)
from dotfactory.observability import stable_span_id  # noqa: E402


class WaterfallProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.temp.name) / "factory.db")
        self.ledger.configure_factory("waterfall-test")
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="linear-dotfactory",
        )
        self.kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "default.dot"
        )
        self.observation = ObservationService(self.ledger, self.kernel)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def failed_execution(self):
        execution = self.kernel.begin(
            "dotfactory", "TASK-571", {"title": "<unsafe waterfall>"},
            command_id="begin-waterfall",
        )
        claim = self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner", command_id="claim-waterfall",
        )
        preparation = self.ledger.begin_preparation(
            attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
            request_digest="request-waterfall",
        )
        self.ledger.fail_preparation(
            str(preparation["id"]), fence_token=claim["fence_token"],
            status="failed", error={
                "code": "RUNNER_MISSING", "category": "preflight",
                "message": "<script>bad()</script>", "retryable": False,
                "safe_remedy": "Install the configured runner.",
            },
        )
        return execution

    def test_fixed_range_projection_is_deterministic_and_payload_free(self):
        execution = self.failed_execution()
        records = self.ledger.trace_page(execution, limit=1000)
        errors = self.ledger.error_page(execution, limit=1000)
        by_id = {item["record_id"]: item["seq"] for item in records}
        errors = [
            dict(item, trace_seq=by_id[item["trace_record_id"]]) for item in errors
        ]
        first = execution_waterfall(records, errors)
        second = execution_waterfall(records, errors)
        self.assertEqual(first, second)
        self.assertEqual(records[-1]["seq"], first["through_trace_seq"])
        rendered = json.dumps(first, sort_keys=True)
        self.assertNotIn("payload", rendered)
        self.assertNotIn("request-waterfall", rendered)
        self.assertEqual(1, len(readable_error_groups(errors)))

    def test_terminal_execution_closes_durable_structural_spans_without_trace_rewrite(self):
        execution = self.kernel.begin(
            "dotfactory", "TASK-609", {"title": "terminal waterfall"},
            command_id="begin-terminal-waterfall",
        )
        self.kernel.transition(
            execution, "Canceled", actor="human", signal="linear_status_change",
            outcome="canceled",
            evidence=[{"kind": "decision", "uri": "linear://cancel"}],
            command_id="cancel-terminal-waterfall",
        )
        trace_before = json.dumps(
            self.ledger.trace_page(execution, limit=1000), sort_keys=True,
        )

        first = self.observation.execution_projection(execution)["waterfall"]
        second = self.observation.execution_projection(execution)["waterfall"]

        self.assertEqual(first, second)
        self.assertEqual(0, first["open_span_count"])
        self.assertTrue(all(
            item["ended_at"] is not None
            for item in first["items"] if item["kind"] == "span"
        ))
        self.assertEqual(trace_before, json.dumps(
            self.ledger.trace_page(execution, limit=1000), sort_keys=True,
        ))

    def test_running_execution_keeps_root_and_current_state_run_open(self):
        execution = self.kernel.begin(
            "dotfactory", "TASK-610", {"title": "running waterfall"},
            command_id="begin-running-waterfall",
        )

        waterfall = self.observation.execution_projection(execution)["waterfall"]
        open_spans = [
            item for item in waterfall["items"]
            if item["kind"] == "span" and item["ended_at"] is None
        ]

        self.assertEqual(2, waterfall["open_span_count"])
        self.assertEqual(
            {"execution_started", "state_run"},
            {item["phase"] for item in open_spans},
        )

    def test_explicit_trace_end_wins_over_completion_fact(self):
        execution = self.kernel.begin(
            "dotfactory", "TASK-616", {"title": "explicit end"},
            command_id="begin-explicit-end",
        )
        records = self.ledger.trace_page(execution, limit=1000)
        root_span_id = stable_span_id("execution", execution)
        explicit_end = "2026-09-02T05:04:18+00:00"
        ledger_end = "2026-09-02T05:04:17+00:00"
        terminal = dict(records[0])
        terminal.update({
            "seq": int(records[-1]["seq"]) + 1,
            "record_id": "explicit-terminal-point",
            "record_kind": "error",
            "phase": "execution_canceled",
            "status": "failed",
            "started_at": None,
            "ended_at": explicit_end,
            "observed_at": explicit_end,
        })
        records.append(terminal)

        waterfall = execution_waterfall(records, [], [{
            "entity_kind": "execution", "entity_id": execution,
            "completed_at": ledger_end,
        }])
        root = next(
            item for item in waterfall["items"]
            if item["kind"] == "span" and item["span_id"] == root_span_id
        )

        self.assertEqual(explicit_end, root["ended_at"])

    def test_summary_and_html_explain_error_and_escape_untrusted_text(self):
        execution = self.failed_execution()
        projection = self.observation.execution_projection(execution)
        summary = projection["summary"]
        html = self.observation.waterfall_html(execution)
        self.assertEqual("RUNNER_MISSING", summary["errors"][0]["code"])
        self.assertEqual(
            "Install the configured runner.",
            summary["errors"][0]["safe_remedy"],
        )
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", html)
        self.assertNotIn("<script>bad()</script>", html)
        self.assertIn("<table>", html)
        self.assertIn("aria-label=\"Run facts\"", html)

    def test_trace_and_error_pages_are_bounded(self):
        execution = self.failed_execution()
        first = self.observation.trace(execution, limit=1)
        self.assertEqual(1, len(first["data"]))
        self.assertIsNotNone(first["next_after_seq"])
        second = self.observation.trace(
            execution, limit=1, after_seq=first["next_after_seq"]
        )
        self.assertNotEqual(first["data"][0]["seq"], second["data"][0]["seq"])
        errors = self.observation.errors(execution, limit=1)
        self.assertEqual("RUNNER_MISSING", errors["data"][0]["code"])

    def test_contract_functions_reject_mixed_executions(self):
        first = self.failed_execution()
        second = self.kernel.begin(
            "dotfactory", "TASK-572", {"title": "second"},
            command_id="begin-second",
        )
        mixed = (
            self.ledger.trace_page(first, limit=1)
            + self.ledger.trace_page(second, limit=1)
        )
        with self.assertRaisesRegex(ValueError, "one execution"):
            execution_waterfall(mixed, [])

    def test_repeated_errors_group_without_losing_occurrence_links(self):
        base = {
            "fingerprint": "f" * 64, "code": "REPEATED", "category": "runner",
            "severity": "error", "message": "same failure",
            "safe_remedy": "Inspect the first failed span.", "retryable": True,
            "ambiguous_side_effect": False,
        }
        groups = readable_error_groups([
            dict(base, seq=1, trace_seq=8, error_id="error-1",
                 trace_record_id="trace-1", responsible_span_id="span-1",
                 occurred_at="2026-08-30T12:00:00+00:00"),
            dict(base, seq=2, trace_seq=14, error_id="error-2",
                 trace_record_id="trace-2", responsible_span_id="span-2",
                 occurred_at="2026-08-30T12:01:00+00:00"),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual(2, groups[0]["occurrence_count"])
        self.assertEqual([8, 14], [
            item["trace_seq"] for item in groups[0]["occurrences"]
        ])


if __name__ == "__main__":
    unittest.main()
