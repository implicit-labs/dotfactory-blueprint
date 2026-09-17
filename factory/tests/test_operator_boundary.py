import json
import io
import socket
from contextlib import redirect_stdout
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from dotfactory.cli import _demo_config, _run_exit_code, main
from dotfactory.control import Principal
from dotfactory.instance import FactoryConfig
from dotfactory.lifecycle import FactoryRuntime, LifecycleError, fixture_runner
from dotfactory.linear_api import LinearAPIError
from dotfactory.live_runner import LiveRunner, RunnerRoute, RunnerCanceled
from dotfactory.operator import send, socket_path
from dotfactory.runner import runner_request
from dotfactory.ledger import StaleAttempt


class OperatorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-op-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.config = FactoryConfig.load(_demo_config(self.root))

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_stop_before_next_dispatch_and_restart_at_target(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            receipt = runtime.run([execution], max_ticks=1, until_state="Ready")
            self.assertEqual("target_state", receipt.shutdown_reason)
            self.assertEqual(0, _run_exit_code(runtime, receipt))
            self.assertEqual(1, len(runtime.ledger.run_history(execution)["attempts"]))
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            receipt = runtime.run([execution], until_state="Ready")
            self.assertEqual("target_state", receipt.shutdown_reason)
            self.assertEqual(1, len(runtime.ledger.run_history(execution)["attempts"]))
            runtime.run([execution], until_state="Review")
            with self.assertRaisesRegex(LifecycleError, "already passed"):
                runtime.run([execution], until_state="Ready")
            with self.assertRaisesRegex(LifecycleError, "unknown"):
                runtime.run([execution], until_state="Unknown")
            receipt = runtime.run([execution], until_state="Done")
            self.assertEqual("target_not_reached", receipt.shutdown_reason)
            self.assertEqual(1, _run_exit_code(runtime, receipt))
            runtime.control_service("demo").execute(
                execution, command_id="terminal", principal=Principal("test", "approver", "test"),
                request={"action": "cancel", "expected_state": "Review", "confirmed": True},
            )
            receipt = runtime.run([execution], until_state="Canceled")
            self.assertEqual("target_state", receipt.shutdown_reason)
            with self.assertRaisesRegex(LifecycleError, "unreachable"):
                runtime.run([execution], until_state="Done")

    def call(self, runtime, message):
        responses = []
        def client():
            try:
                responses.append(send(runtime.operator_server.path, message))
            except Exception as error:
                responses.append(error)
        thread = threading.Thread(target=client)
        thread.start()
        deadline = time.monotonic() + 3
        while thread.is_alive() and time.monotonic() < deadline:
            runtime.operator_server.pump()
            time.sleep(0.005)
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        if isinstance(responses[0], Exception):
            raise responses[0]
        return responses[0]

    def test_socket_permissions_scope_idempotency_and_cleanup(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.run([execution], until_state="Review")
            runtime.enable_operator()
            endpoint = runtime.operator_server.path
            self.assertEqual(0o600, endpoint.stat().st_mode & 0o777)
            message = {"operation": "command", "project": "demo", "execution": execution,
                       "command_id": "socket-cancel", "request": {
                           "action": "cancel", "expected_state": "Review", "confirmed": True}}
            denied = self.call(runtime, {**message, "command_id": "unconfirmed", "request": {
                "action": "cancel", "expected_state": "Review"}})
            self.assertEqual("denied", denied["data"]["status"])
            stale = self.call(runtime, {**message, "command_id": "stale", "request": {
                "action": "cancel", "expected_state": "Implementing", "confirmed": True}})
            self.assertFalse(stale["ok"])
            self.assertEqual("Review", runtime.ledger.current(execution)["current_state_id"])
            first = self.call(runtime, message)
            second = self.call(runtime, message)
            self.assertTrue(first["ok"], first)
            self.assertEqual("completed", first["data"]["status"])
            self.assertEqual(first["data"]["command_id"], second["data"]["command_id"])
            foreign = self.call(runtime, {"operation": "status", "project": "foreign"})
            self.assertFalse(foreign["ok"])
            self.assertTrue(self.call(runtime, {"operation": "drain", "project": "demo"})["ok"])
            self.assertTrue(runtime.drain_requested)
        self.assertFalse(endpoint.exists())

    def test_attention_recovers_after_a_stale_owned_socket(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            attention = runtime.ledger.open_attention(
                execution_id=execution, attempt_id=None, preparation_id=None,
                dedupe_key="offline-remedy", category="test", provider="git-worktree",
                detail={"allowed_actions": ["retry"]},
            )
            endpoint = socket_path(runtime.ledger.path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(endpoint))
            endpoint.chmod(0o600)
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["attention", "--config", str(self.config.path), "--project", "demo",
                         "--execution", execution, "--attention-id", attention["id"],
                         "--expected-state", "Todo", "--remedy", "retry",
                         "--command-id", "stale-socket-recovery"])
        self.assertEqual(0, code)
        self.assertEqual("completed", json.loads(output.getvalue())["status"])

    def test_drain_does_not_claim_the_next_pickup(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.run([execution], until_state="Ready")
            runtime.drain_requested = True
            receipt = runtime.run([execution], watch=True)
            self.assertEqual("drained", receipt.shutdown_reason)
            self.assertEqual("Ready", runtime.ledger.current(execution)["current_state_id"])
            self.assertEqual(1, len(runtime.ledger.run_history(execution)["attempts"]))

    def test_stop_at_work_state_does_not_dispatch_its_attempt(self):
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            receipt = runtime.run([execution], until_state="Autoplanning")
            self.assertEqual("target_state", receipt.shutdown_reason)
            self.assertEqual(0, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM runner_runs").fetchone()[0])
            self.assertEqual(0, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM preparations").fetchone()[0])

    def test_socket_cancels_owned_active_child_without_second_ledger_writer(self):
        executable = self.root / "slow-runner"
        pid_file = self.root / "pid"
        executable.write_text(
            f"#!{sys.executable}\nimport os,time\n"
            f"open({str(pid_file)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n")
        executable.chmod(0o700)
        read_started = threading.Event()
        read_finished = threading.Event()
        release_read = threading.Event()
        class SlowTracker:
            def issue(self, _identifier):
                read_started.set()
                release_read.wait(5)
                read_finished.set()
                raise LinearAPIError("TIMEOUT", "fixture outage", retryable=True)
        with FactoryRuntime(self.config, runner=fixture_runner()) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime._claim_pickups()
            request = runner_request(runtime.kernels["demo"], execution)
            launch = runtime.projects["demo"].preparation.prepare(request).launch
            runtime.enable_operator()
            runtime.linear_workers["demo"] = SimpleNamespace(client=SlowTracker())
            route = RunnerRoute("codex", "codex", str(executable), "0.147.0", "approve-for-me",
                                silence_timeout_seconds=5, termination_grace_seconds=1)
            runner = LiveRunner(runtime.ledger, routes={"codex": route},
                                observed_versions={"codex": "0.147.0"},
                                environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
                                cancel_requested=runtime._runner_cancel_requested)
            responses = []
            def client():
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                read_started.wait(3)
                responses.append(send(runtime.operator_server.path, {
                    "operation": "command", "project": "demo", "execution": execution,
                    "command_id": "active-cancel", "request": {
                        "action": "cancel", "expected_state": "Autoplanning", "confirmed": True}}))
            thread = threading.Thread(target=client)
            thread.start()
            try:
                with self.assertRaises((StaleAttempt, RunnerCanceled)):
                    runner.run(launch)
                self.assertTrue(read_started.is_set())
                self.assertFalse(read_finished.is_set(), "tracker read blocked local cancellation")
            finally:
                release_read.set()
                thread.join(timeout=6)
            self.assertFalse(thread.is_alive())
            self.assertTrue(responses[0]["ok"], responses)
            self.assertEqual("Canceled", runtime.ledger.current(execution)["current_state_id"])
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
            run = runtime.ledger.runner_run_for_attempt(request.attempt_id)
            self.assertEqual("canceled", run["status"])


if __name__ == "__main__":
    unittest.main()
