import copy
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dotfactory import FactoryConfig, FactoryRuntime, SQLiteLedger
from dotfactory.budgets import evaluate, usage
from dotfactory.cli import _demo_config, main
from dotfactory.control import ObservationService
from dotfactory.instance import FactoryConfigError
from dotfactory.lifecycle import fixture_runner
from dotfactory.linear_api import LinearAPIError, LinearGraphQLClient
from dotfactory.operator import send, socket_path
from dotfactory.work_queue import WorkQueue, rejection
from dotfactory.token_usage import normalize_token_usage


def candidate(number, *, labels=None, priority=0):
    return {
        "id": f"issue-{number}", "identifier": f"DEMO-{number}", "title": f"Task {number}",
        "description": "Implement the task", "updatedAt": "revision-1", "priority": priority,
        "createdAt": f"2026-09-18T12:00:{number:02d}Z", "state": {"name": "Todo", "id": "todo"},
        "project": {"id": "demo-project"}, "team": {"id": "demo-team"},
        "labels": {"nodes": [{"name": name} for name in (labels if labels is not None else ["factory-ready"])],
                   "pageInfo": {"hasNextPage": False}},
        "inverseRelations": {"nodes": [], "pageInfo": {"hasNextPage": False}},
    }


class Tracker:
    def __init__(self):
        self.issues = []
        self.scans = 0
        self.on_fresh = None

    def queue_issues(self, **_):
        self.scans += 1
        return copy.deepcopy(self.issues)

    def queue_issue(self, identifier):
        value = copy.deepcopy(next(item for item in self.issues if item["identifier"] == identifier))
        return self.on_fresh(value) if self.on_fresh else value


def record_usage(ledger, launch, counts=(10, 5), kind="codex", reports=1, failed=False,
                 trailing_missing=False, trailing_role=None, canceled_uncertain=False):
    request = launch.request
    run = ledger.plan_runner_run(
        execution_id=request.execution_id, attempt_id=request.attempt_id,
        preparation_id=launch.preparation_id, preparation_digest=launch.preparation_digest,
        fence_token=request.fence_token, runner_key="codex", adapter_kind=kind,
        adapter_version="fixture", protocol_version=1, execution_trace_id="1" * 32,
        trace_id="2" * 32, root_span_id="3" * 16, parent_trace_id=None,
        command=["fixture"], command_digest="command", prompt_digest="prompt", host_id="host", boot_id="boot",
    )
    ledger.mark_runner_starting(run["id"], fence_token=request.fence_token)
    ledger.mark_runner_running(run["id"], fence_token=request.fence_token, pid=1234, process_group_id=1234)
    for index in range(reports if counts is not None else 0):
        ledger.append_runner_event(
            run["id"], fence_token=request.fence_token, kind="result", stream="stdout",
            protocol_type={"codex": "turn.completed", "claude-code": "result", "omp-rpc": "message_end"}.get(kind, "unknown"),
            payload={"usage": {"input": counts[0], "output": counts[1]}, "index": index},
            span_id="4" * 16, parent_span_id="3" * 16, source_occurred_at=None,
            observed_at=ledger.clock(), origin="provider", trust_class="untrusted-provider",
        )
    if trailing_missing:
        ledger.append_runner_event(
            run["id"], fence_token=request.fence_token, kind="result", stream="stdout",
            protocol_type={"codex": "turn.completed", "claude-code": "result", "omp-rpc": "message_end"}[kind],
            payload={"message_role": trailing_role}, span_id="5" * 16, parent_span_id="3" * 16,
            source_occurred_at=None, observed_at=ledger.clock(), origin="provider", trust_class="untrusted-provider",
        )
    if canceled_uncertain:
        ledger.finish_runner_run(run["id"], fence_token=request.fence_token, status="canceled",
                                  error={"ambiguous_side_effect": True})
    elif failed:
        ledger.finish_runner_run(run["id"], fence_token=request.fence_token, status="failed")
    else:
        ledger.record_runner_result(run["id"], fence_token=request.fence_token,
                                    result={}, receipt={}, session_id=None)


class WorkQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = _demo_config(self.root)
        self.values = json.loads(self.path.read_text())
        self.values["work_queue"] = {"enabled": True}
        self.tracker = Tracker()

    def tearDown(self):
        self.temp.cleanup()

    def config(self):
        self.path.write_text(json.dumps(self.values))
        return FactoryConfig.load(self.path)

    def runtime(self, runner=None):
        runtime = FactoryRuntime(self.config(), runner=runner or fixture_runner(50))
        runtime.linear_workers["demo"] = SimpleNamespace(
            client=self.tracker, poll=lambda *_: None, observe_issue=lambda *_: None,
        )
        runtime._drain_linear = lambda: None  # This fixture proves local authority, not remote delivery.
        return runtime

    def test_empty_two_admitted_one_excluded_restart_exactly_once(self):
        with self.runtime() as runtime:
            queue = WorkQueue(runtime)
            self.assertEqual("idle", queue.step()["status"])
            self.tracker.issues = [candidate(1), candidate(2), candidate(3, labels=[])]
            self.assertEqual("admitted", queue.step()["status"])
            first = runtime._existing_execution("demo", "DEMO-1")
            scans = self.tracker.scans
            queue.step()
            self.assertEqual(scans, self.tracker.scans, "existing work must precede discovery")
        with self.runtime() as runtime:
            queue = WorkQueue(runtime)
            for _ in range(12):
                queue.step()
            runs = runtime.ledger.list_runs(limit=100)
            self.assertEqual(2, len(runs))
            self.assertEqual({"Review"}, {run["current_state_id"] for run in runs})
            self.assertEqual(first, runtime._existing_execution("demo", "DEMO-1"))
            self.assertFalse(runtime._has_execution("demo", "DEMO-3"))
            self.assertEqual(6, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM scheduler_dispatches WHERE status='completed'"
            ).fetchone()[0])
            runtime.drain_requested = True
            scans = self.tracker.scans
            self.assertEqual("drained", queue.step()["status"])
            self.assertEqual(scans, self.tracker.scans)

    def test_priority_and_fresh_admission_precedence(self):
        self.tracker.issues = [candidate(1), candidate(2, priority=1), candidate(3, priority=2)]
        self.tracker.on_fresh = lambda issue: {**issue, "labels": candidate(1, labels=[])["labels"]} if issue["identifier"] == "DEMO-2" else issue
        with self.runtime() as runtime:
            result = WorkQueue(runtime).step()
            self.assertEqual("DEMO-3", result["receipt"]["detail"]["issue"])
            self.assertFalse(runtime._has_execution("demo", "DEMO-2"))
            self.assertEqual("admitted_queue", runtime.ledger.run_snapshot(
                result["receipt"]["detail"]["execution_id"])["intent"]["source"])

    def test_test_demo_blocked_wrong_scope_and_incomplete_are_not_admitted(self):
        cases = []
        for labels in (["TEST", "factory-ready"], ["Demo", "factory-ready"], []):
            cases.append(candidate(1, labels=labels))
        blocked = candidate(1)
        blocked["inverseRelations"]["nodes"] = [{"type": "blocks", "issue": {"state": {"type": "started"}}}]
        cases.append(blocked)
        incomplete = candidate(1)
        incomplete["labels"]["pageInfo"]["hasNextPage"] = True
        cases.extend([incomplete, {**candidate(1), "project": {"id": "other"}},
                      {**candidate(1), "state": {"name": "Backlog"}}, {**candidate(1), "inverseRelations": {}}])
        for issue in cases:
            self.assertIsNotNone(rejection(issue, {}, "demo-project", ["Todo"]))
        blocked["inverseRelations"]["nodes"][0]["issue"]["state"]["type"] = "completed"
        self.assertIsNone(rejection(blocked, {}, "demo-project", ["Todo"]))

    def test_invalid_high_priority_description_does_not_starve_valid_work(self):
        self.tracker.issues = [{**candidate(1, priority=1), "description": "x" * 65537}, candidate(2)]
        with self.runtime() as runtime:
            result = WorkQueue(runtime).step()
            self.assertEqual("DEMO-2", result["receipt"]["detail"]["issue"])
            self.assertFalse(runtime._has_execution("demo", "DEMO-1"))
            scans = [item for item in runtime.ledger.operating_receipts("demo") if item["detail"]["status"] == "scanned"]
            self.assertEqual(1, scans[0]["detail"]["excluded"]["invalid_description"])

    def test_queue_requires_opt_in_single_child_and_tracker(self):
        with self.runtime() as runtime:
            runtime.config.values["work_queue"]["enabled"] = False
            with self.assertRaisesRegex(ValueError, "enabled"):
                WorkQueue(runtime)
        self.values["scheduler"]["limits"]["host"] = 2
        with self.runtime() as runtime:
            with self.assertRaisesRegex(ValueError, "one active child"):
                WorkQueue(runtime)

    def test_attention_blocks_new_admission(self):
        self.tracker.issues = [candidate(1), candidate(2)]
        with self.runtime() as runtime:
            queue = WorkQueue(runtime)
            execution = queue.step()["receipt"]["detail"]["execution_id"]
            runtime.ledger.open_attention(execution_id=execution, attempt_id=None, preparation_id=None,
                                          dedupe_key="test", category="test", detail={})
            self.assertEqual("servicing_existing", queue.step()["status"])
            self.assertFalse(runtime._has_execution("demo", "DEMO-2"))

    def test_tracker_failure_still_reconciles_existing_dispatch(self):
        with self.runtime() as runtime:
            with patch.object(runtime, "step", side_effect=LinearAPIError("offline", "offline", retryable=True)), patch.object(
                runtime.scheduler, "reconcile", return_value=None,
            ) as recovery:
                self.assertEqual("tracker_unavailable", WorkQueue(runtime).step()["status"])
                recovery.assert_called_once()

    def test_canceled_uncertain_child_stops_even_existing_dispatch(self):
        with self.runtime() as runtime:
            queue = WorkQueue(runtime)
            with patch.object(queue, "_uncertain_child", return_value=True), patch.object(runtime, "step") as step:
                self.assertEqual("needs_attention", queue.step()["status"])
                step.assert_not_called()
                self.assertEqual(0, self.tracker.scans)

    def test_project_selection_cannot_bypass_an_uncertain_child(self):
        with self.runtime() as runtime:
            base = fixture_runner(50)
            def runner(launch):
                record_usage(runtime.ledger, launch, canceled_uncertain=True)
                return base.run(launch)
            runtime.scheduler.runner = SimpleNamespace(run=runner)
            runtime.start_issue("demo", "DEMO-1", admission_snapshot=candidate(1))
            runtime.step()
            queue = WorkQueue(runtime)
            self.assertTrue(queue._uncertain_child())
            with patch.object(runtime, "kernels", {}):
                self.assertTrue(queue._uncertain_child())

    def test_nonfinite_usage_is_not_accepted_as_a_count(self):
        self.assertIsNone(normalize_token_usage({"input": float("nan"), "output": float("inf")}))

    def test_project_budget_prevents_admission_after_review(self):
        self.values["budgets"] = {"project_limit": 45}
        self.tracker.issues = [candidate(1), candidate(2)]
        with self.runtime() as runtime:
            base = fixture_runner(50)
            def runner(launch):
                record_usage(runtime.ledger, launch)
                return base.run(launch)
            runtime.scheduler.runner = SimpleNamespace(run=runner)
            queue = WorkQueue(runtime)
            queue.step()
            for _ in range(5):
                queue.step()
            first = runtime._existing_execution("demo", "DEMO-1")
            self.assertEqual("Review", runtime.ledger.current(first)["current_state_id"])
            self.assertFalse(runtime._has_execution("demo", "DEMO-2"))
            self.assertTrue(any(item["detail"]["status"] == "budget_blocked"
                                for item in runtime.ledger.operating_receipts("demo")))

    def test_idle_wait_pumps_operator_drain(self):
        with self.runtime() as runtime:
            runtime.enable_operator()
            responses = []
            thread = threading.Thread(target=lambda: responses.append(send(
                socket_path(runtime.ledger.path), {"operation": "drain", "project": "demo"},
            )))
            thread.start()
            runtime._wait(2)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(responses[0]["data"]["draining"])
            self.assertEqual("drained", WorkQueue(runtime).step()["status"])

    def test_cli_drain_exits_and_leaves_durable_receipt(self):
        with self.runtime() as runtime:
            with patch("dotfactory.cli.FactoryRuntime", return_value=runtime), patch(
                "dotfactory.cli._install_signals",
            ), patch.object(runtime, "_wait", side_effect=lambda _: setattr(runtime, "drain_requested", True)), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(0, main(["work", "--config", str(self.path), "--project", "demo"]))
            self.assertIn('"status": "drained"', output.getvalue())
        ledger = SQLiteLedger(self.root / "factory.db")
        try:
            self.assertEqual("drained", ledger.operating_receipts("demo")[0]["detail"]["status"])
        finally:
            ledger.close()

    def test_budgets_stop_next_dispatch_before_preparation_and_survive_restart(self):
        for number, scope in enumerate(("project_limit", "execution_limit"), 1):
            with self.subTest(scope=scope):
                self.values["budgets"] = {scope: 15}
                self.values["ledger_path"] = str(self.root / (scope + ".db"))
                base = fixture_runner(50)
                runtime = self.runtime()
                def runner(launch):
                    record_usage(runtime.ledger, launch)
                    return base.run(launch)
                runtime.scheduler.runner = SimpleNamespace(run=runner)
                with runtime:
                    execution = runtime.start_issue("demo", f"DEMO-{number}", admission_snapshot=candidate(number))
                    self.assertEqual("completed", runtime.step()["scheduler"]["disposition"])
                    self.assertEqual("budget_blocked", runtime.step()["scheduler"]["disposition"])
                    self.assertEqual(1, len(base.launches))
                    receipt = runtime.ledger.operating_receipts("demo")[0]
                    self.assertEqual("blocked", receipt["detail"]["status"])
                    self.assertIn("No new dispatch", receipt["detail"]["message"])
                with self.runtime() as restarted:
                    decision = evaluate(restarted.ledger, self.values["budgets"], "demo", execution)
                    self.assertEqual("blocked", decision["status"])
                    self.assertEqual(15, decision["scopes"][0]["used"])
                    self.assertEqual(1, restarted.ledger.connection.execute("SELECT COUNT(*) FROM preparations").fetchone()[0])
                    view = ObservationService(restarted.ledger, restarted.kernels["demo"]).runs(project_key="demo")
                    self.assertTrue(view["operating_receipts"])

    def test_missing_usage_blocks_project_admission_not_zero_spend(self):
        self.values["budgets"] = {"project_limit": 1000}
        with self.runtime() as runtime:
            base = fixture_runner(50)
            def runner(launch):
                record_usage(runtime.ledger, launch, counts=None)
                return base.run(launch)
            runtime.scheduler.runner = SimpleNamespace(run=runner)
            execution = runtime.start_issue("demo", "DEMO-1", admission_snapshot=candidate(1))
            runtime.step()
            decision = evaluate(runtime.ledger, self.values["budgets"], "demo", execution)
            self.assertEqual("usage_unavailable", decision["scopes"][0]["status"])
            self.assertEqual("budget_blocked", runtime.step()["scheduler"]["disposition"])

    def test_provider_usage_semantics(self):
        for number, (kind, expected) in enumerate((("codex", 30), ("claude-code", 15), ("omp-rpc", 30), ("unknown", 0)), 1):
            self.values["ledger_path"] = str(self.root / (kind + ".db"))
            with self.runtime() as runtime:
                base = fixture_runner(50)
                def runner(launch):
                    record_usage(runtime.ledger, launch, kind=kind, reports=2)
                    return base.run(launch)
                runtime.scheduler.runner = SimpleNamespace(run=runner)
                runtime.start_issue("demo", f"DEMO-{number}", admission_snapshot=candidate(number))
                runtime.step()
                measured = usage(runtime.ledger, "demo")
                self.assertEqual(expected, measured["used"])
                self.assertEqual(int(kind == "unknown"), measured["unknown_runs"])

    def test_receipts_deduplicate_unchanged_polls_and_migrate_v13(self):
        with self.runtime() as runtime:
            one = runtime.ledger.record_operating_receipt("queue", "demo", None, {"status": "idle"})
            self.assertEqual(one, runtime.ledger.record_operating_receipt("queue", "demo", None, {"status": "idle"}))
        with sqlite3.connect(self.root / "factory.db") as database:
            database.execute("DROP TABLE operating_receipts")
            database.execute("PRAGMA user_version=13")
        with self.runtime() as runtime:
            self.assertEqual([], runtime.ledger.operating_receipts())
            self.assertEqual(14, runtime.ledger.connection.execute("PRAGMA user_version").fetchone()[0])

    def test_valid_then_missing_usage_is_incomplete_except_nonassistant_omp_messages(self):
        cases = (("codex", None, 1), ("claude-code", None, 1),
                 ("omp-rpc", "assistant", 1), ("omp-rpc", "toolResult", 0))
        for number, (kind, role, unknown) in enumerate(cases, 1):
            self.values["ledger_path"] = str(self.root / f"missing-{number}.db")
            with self.runtime() as runtime:
                base = fixture_runner(50)
                def runner(launch):
                    record_usage(runtime.ledger, launch, kind=kind, trailing_missing=True, trailing_role=role)
                    return base.run(launch)
                runtime.scheduler.runner = SimpleNamespace(run=runner)
                runtime.start_issue("demo", f"DEMO-{number}", admission_snapshot=candidate(number))
                runtime.step()
                self.assertEqual(unknown, usage(runtime.ledger, "demo")["unknown_runs"])

    def test_config_rejects_invalid_limits_and_policies(self):
        for key, value in (("budgets", {"project_limit": True}), ("budgets", {"execution_limit": 0}),
                           ("budgets", {"spend_limit": 10}), ("work_queue", {"enabled": "yes"}),
                           ("work_queue", {"admission_label": "TEST"})):
            with self.subTest(value=value):
                previous = self.values.get(key)
                self.values[key] = value
                with self.assertRaises(FactoryConfigError):
                    self.config()
                if previous is None:
                    self.values.pop(key)
                else:
                    self.values[key] = previous


class QueuePaginationTests(unittest.TestCase):
    def test_drain_between_pages_discards_partial_candidates(self):
        calls = []
        def transport(*_):
            calls.append("page")
            return {"data": {"issues": {"nodes": [candidate(1)],
                    "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}
        client = LinearGraphQLClient("fixture", transport=transport)
        self.assertEqual([], client.queue_issues(project_id="demo-project", status_names=["Todo"],
                                                should_continue=lambda: not calls))
        self.assertEqual(["page"], calls)

    def test_pagination_deduplicates_and_passes_cursor(self):
        calls = []
        def transport(_endpoint, _headers, body, _timeout):
            request = json.loads(body)
            calls.append(request["variables"])
            second = request["variables"]["after"] is not None
            return {"data": {"issues": {"nodes": [candidate(1), candidate(2)] if second else [candidate(1)],
                    "pageInfo": {"hasNextPage": not second, "endCursor": "cursor-one"}}}}
        issues = LinearGraphQLClient("fixture", transport=transport).queue_issues(project_id="demo-project", status_names=["Todo"])
        self.assertEqual(2, len(issues))
        self.assertEqual([None, "cursor-one"], [call["after"] for call in calls])

    def test_invalid_or_repeating_cursor_fails_closed_without_partial_page(self):
        for page in ({}, {"hasNextPage": True, "endCursor": "repeated"}):
            client = LinearGraphQLClient("fixture", transport=lambda *_: {"data": {"issues": {"nodes": [candidate(1)], "pageInfo": page}}})
            with self.assertRaises(LinearAPIError):
                client.queue_issues(project_id="demo-project", status_names=["Todo"])
