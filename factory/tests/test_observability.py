import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory import DurableKernel, SQLiteLedger  # noqa: E402
from dotfactory.ledger import LedgerError  # noqa: E402
from dotfactory.observability import ProjectionReceiptV1  # noqa: E402


class ObservabilityLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "factory.db"
        self.ledger = SQLiteLedger(self.path)
        self.ledger.configure_factory("observability-test")
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="linear-dotfactory",
        )
        self.kernel = DurableKernel(self.ledger, ROOT / "workflows" / "default.dot")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def begin_attempt(self, suffix="base"):
        execution_id = self.kernel.begin(
            "dotfactory", f"TASK-{suffix}", {"title": "trace contract"},
            command_id=f"begin-{suffix}",
        )
        claim = self.kernel.transition(
            execution_id, "Autoplanning", actor="agent", signal="listener_claim",
            owner="planner", command_id=f"claim-{suffix}",
        )
        return execution_id, claim

    def test_lifecycle_source_and_trace_record_commit_together(self):
        execution_id, _claim = self.begin_attempt()
        event_count = int(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM events WHERE execution_id=?", (execution_id,)
        ).fetchone()[0])
        trace_count = int(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM trace_records WHERE execution_id=? "
            "AND source_kind='event'", (execution_id,),
        ).fetchone()[0])
        self.assertEqual(event_count, trace_count)
        page = self.ledger.trace_page(execution_id, limit=100)
        self.assertEqual(list(range(1, len(page) + 1)), [
            item["seq"] - page[0]["seq"] + 1 for item in page
        ])
        self.assertTrue(all(item["ordering_quality"] == "exact" for item in page))
        self.assertTrue(all(item["trace_id"] == page[0]["trace_id"] for item in page))

    def test_crash_between_source_and_trace_rolls_back_both(self):
        before = tuple(self.ledger.connection.execute(
            "SELECT (SELECT COUNT(*) FROM events),"
            "(SELECT COUNT(*) FROM trace_records)"
        ).fetchone())

        def fail(boundary):
            if boundary == "after_trace_source_recorded":
                raise RuntimeError("crash")

        self.ledger.fault_hook = fail
        with self.assertRaisesRegex(RuntimeError, "crash"):
            self.kernel.begin(
                "dotfactory", "TASK-crash", {"title": "crash"},
                command_id="begin-crash",
            )
        self.ledger.fault_hook = None
        after = tuple(self.ledger.connection.execute(
            "SELECT (SELECT COUNT(*) FROM events),"
            "(SELECT COUNT(*) FROM trace_records)"
        ).fetchone())
        self.assertEqual(before, after)

    def test_pre_spawn_failure_becomes_redacted_error_fact(self):
        execution_id, claim = self.begin_attempt("failure")
        preparation = self.ledger.begin_preparation(
            attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
            request_digest="request-digest",
        )
        self.ledger.fail_preparation(
            str(preparation["id"]), fence_token=claim["fence_token"],
            status="failed", error={
                "code": "RUNNER_EXECUTABLE_MISSING", "category": "preflight",
                "message": "runner executable is missing", "retryable": False,
                "safe_remedy": "Install the configured runner.",
                "api_key": "SECRET-SENTINEL",
            },
        )
        errors = self.ledger.error_page(execution_id)
        self.assertEqual(1, len(errors))
        self.assertEqual("RUNNER_EXECUTABLE_MISSING", errors[0]["code"])
        self.assertEqual("preflight", errors[0]["category"])
        self.assertFalse(errors[0]["retryable"])
        self.assertEqual("Install the configured runner.", errors[0]["safe_remedy"])
        self.assertNotIn("SECRET-SENTINEL", json.dumps(
            self.ledger.trace_page(execution_id), sort_keys=True
        ))
        self.assertNotIn("SECRET-SENTINEL", json.dumps(errors, sort_keys=True))

    def test_projection_receipts_are_fixed_range_crash_safe_and_monotonic(self):
        execution_id, _claim = self.begin_attempt("projection")
        through = int(self.ledger.connection.execute(
            "SELECT MAX(seq) FROM trace_records WHERE execution_id=?", (execution_id,)
        ).fetchone()[0])
        attempt = self.ledger.start_projection_attempt(
            "logfire", command_id="publish-1", idempotency_key="publish-1",
            from_source_seq=1, through_source_seq=through,
        )
        pending = self.ledger.pending_projection_records(str(attempt["id"]), limit=1000)
        self.assertTrue(pending)
        first = pending[0]
        rejected = ProjectionReceiptV1(
            receipt_id="receipt-rejected", attempt_id=str(attempt["id"]),
            destination="logfire", source_record_id=str(first["record_id"]),
            status="rejected", idempotency_key="reject-first",
            recorded_at="2026-08-30T12:00:00+00:00", error_code="OTLP_REJECTED",
            detail={"api_key": "SECRET-SENTINEL"},
        )
        stored = self.ledger.record_projection_receipt(
            rejected, rejection={"message": "destination rejected record", "retryable": True}
        )
        self.assertEqual("[REDACTED]", stored["detail"]["api_key"])
        self.assertEqual("paused", self.ledger.projection_attempt(
            str(attempt["id"])
        )["status"])
        with self.assertRaisesRegex(LedgerError, "skip unaccepted"):
            self.ledger.advance_projection_watermark(
                str(attempt["id"]), through_source_seq=through
            )
        crash_receipt = ProjectionReceiptV1(
            receipt_id="receipt-crash", attempt_id=str(attempt["id"]),
            destination="logfire", source_record_id=str(pending[-1]["record_id"]),
            status="accepted", idempotency_key="accept-crash",
            recorded_at="2026-08-30T12:01:00+00:00",
        )

        def fail(boundary):
            if boundary == "after_projection_receipt_recorded":
                raise RuntimeError("crash")

        self.ledger.fault_hook = fail
        with self.assertRaisesRegex(RuntimeError, "crash"):
            self.ledger.record_projection_receipt(crash_receipt)
        self.ledger.fault_hook = None
        self.assertIsNone(self.ledger.connection.execute(
            "SELECT 1 FROM projection_receipts WHERE receipt_id='receipt-crash'"
        ).fetchone())
        self.assertIsNone(self.ledger.connection.execute(
            "SELECT 1 FROM trace_records WHERE source_kind='projection_receipt' "
            "AND source_id='receipt-crash'"
        ).fetchone())
        for index, record in enumerate(pending):
            receipt = ProjectionReceiptV1(
                receipt_id=f"receipt-{index}", attempt_id=str(attempt["id"]),
                destination="logfire", source_record_id=str(record["record_id"]),
                status="accepted", idempotency_key=f"accept-{index}",
                recorded_at=f"2026-08-30T12:00:{index:02d}+00:00",
                external_id=f"external-{index}",
            )
            self.ledger.record_projection_receipt(receipt)
        self.assertEqual([], self.ledger.pending_projection_records(
            str(attempt["id"]), limit=1000
        ))
        watermark = self.ledger.advance_projection_watermark(
            str(attempt["id"]), through_source_seq=through
        )
        self.assertEqual(through, watermark["through_source_seq"])
        self.assertEqual("completed", self.ledger.projection_attempt(
            str(attempt["id"])
        )["status"])
        with self.assertRaisesRegex(LedgerError, "cannot move backward"):
            self.ledger.advance_projection_watermark(
                str(attempt["id"]), through_source_seq=through - 1
            )

    def test_schema_nine_backfill_is_explicitly_reconstructed(self):
        execution_id, claim = self.begin_attempt("migration")
        preparation = self.ledger.begin_preparation(
            attempt_id=claim["attempt_id"], fence_token=claim["fence_token"],
            request_digest="migration-request",
        )
        self.ledger.mark_preparation_ready(
            str(preparation["id"]), fence_token=claim["fence_token"],
            result_digest="migration-ready", prepared={"runner": "fixture"},
        )
        run = self.ledger.plan_runner_run(
            execution_id=execution_id, attempt_id=claim["attempt_id"],
            preparation_id=str(preparation["id"]),
            preparation_digest="migration-ready", fence_token=claim["fence_token"],
            runner_key="fixture", adapter_kind="codex", adapter_version="1.0.0",
            protocol_version=1, execution_trace_id="1" * 32,
            trace_id="2" * 32, root_span_id="3" * 16, parent_trace_id=None,
            command=["fixture"], command_digest="command",
            prompt_digest="prompt", host_id="host", boot_id="boot",
        )
        self.ledger.mark_runner_starting(
            str(run["id"]), fence_token=claim["fence_token"]
        )
        self.ledger.mark_runner_running(
            str(run["id"]), fence_token=claim["fence_token"],
            pid=1234, process_group_id=1234,
        )
        self.ledger.append_runner_event(
            str(run["id"]), fence_token=claim["fence_token"], kind="assistant",
            protocol_type="fixture.message", stream="stdout",
            payload={"id": "message-1"}, span_id="4" * 16,
            parent_span_id="3" * 16, source_occurred_at=None,
            observed_at="2026-08-30T12:00:00+00:00", origin="provider",
            trust_class="untrusted-provider",
        )
        expected_events = int(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM events WHERE execution_id=?", (execution_id,)
        ).fetchone()[0])
        expected_runner = int(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM runner_events WHERE execution_id=?", (execution_id,)
        ).fetchone()[0])
        self.ledger.close()
        database = sqlite3.connect(self.path)
        database.execute("PRAGMA foreign_keys=OFF")
        for table in (
            "projection_rejections", "projection_receipts", "projection_watermarks",
            "projection_attempts", "error_facts", "trace_records",
        ):
            database.execute(f"DROP TABLE {table}")
        database.execute("PRAGMA user_version=9")
        database.commit()
        database.close()
        self.ledger = SQLiteLedger(self.path)
        page = self.ledger.trace_page(execution_id, limit=1000)
        self.assertEqual(expected_events + expected_runner + 1, len(page))
        self.assertTrue(all(item["ordering_quality"] == "reconstructed" for item in page))
        self.assertEqual(expected_runner, len([
            item for item in page if item["source_kind"] == "runner_event"
        ]))
        completeness = [item for item in page if item["record_kind"] == "completeness"]
        self.assertEqual(1, len(completeness))
        self.assertEqual(
            ["reconstructed_order", "raw_stream_coverage_unknown"],
            completeness[0]["completeness"]["reasons"],
        )
        before_restart = json.dumps(page, sort_keys=True, separators=(",", ":"))
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        after_restart = json.dumps(
            self.ledger.trace_page(execution_id, limit=1000),
            sort_keys=True, separators=(",", ":"),
        )
        self.assertEqual(before_restart, after_restart)

    def test_ten_thousand_record_pages_are_bounded_without_gaps(self):
        execution_id, claim = self.begin_attempt("scale")
        state_run_id = self.ledger.current(execution_id)["current_state_run_id"]
        with self.ledger.transaction() as db:
            for index in range(10000):
                self.ledger._event(
                    db, execution_id=execution_id,
                    state_run_id=state_run_id,
                    attempt_id=claim["attempt_id"], event_type="scale_observed",
                    payload={"index": index}, idempotency_key=f"scale:{index}",
                    destinations=(),
                )
        with self.assertRaisesRegex(LedgerError, "between 1 and 1000"):
            self.ledger.trace_page(execution_id, limit=1001)
        first = self.ledger.trace_page(execution_id, limit=1000)
        second = self.ledger.trace_page(
            execution_id, after_seq=int(first[-1]["seq"]), limit=1000
        )
        self.assertEqual(1000, len(first))
        self.assertEqual(1000, len(second))
        self.assertEqual(int(first[-1]["seq"]) + 1, int(second[0]["seq"]))


if __name__ == "__main__":
    unittest.main()
