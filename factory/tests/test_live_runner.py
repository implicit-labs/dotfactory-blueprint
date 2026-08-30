import base64
import json
import os
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from dotfactory import (
    ClaudeCodeAdapter, CodexAdapter, ControlService, DurableKernel, FactoryConfig,
    LiveRunner, LiveRunnerRouter, OmpRpcAdapter, OmpRpcFrameDecoder,
    PreparedLaunch, Principal, RunnerExecutionError, RunnerNeedsAttention,
    RunnerProtocolError, RunnerProviderError, RunnerRoute, SQLiteLedger,
)
from dotfactory.ledger import StaleAttempt
from dotfactory.resources import PreparationError
from dotfactory.runner import runner_request


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "runners"


class LiveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = SQLiteLedger(self.root / "factory.db")
        self.ledger.configure_factory("live-runner-test")
        self.ledger.register_project(
            "alpha", display_name="Alpha", tracker_kind="linear",
            tracker_project_id="linear-alpha",
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def launch(self, runner="codex"):
        kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "default.dot",
            factory_defaults={
                "runner": runner, "model": "fixture-model",
                "reasoning_effort": "high",
            },
        )
        execution = kernel.begin(
            "alpha", f"IMP-{runner}", {"title": runner},
            command_id=f"begin-{runner}",
        )
        kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner=f"owner-{runner}", command_id=f"enter-{runner}",
        )
        request = runner_request(kernel, execution)
        request.config["prompt"] = "Do the task"
        request.config["timeout"] = "10s"
        preparation = self.ledger.begin_preparation(
            attempt_id=request.attempt_id, fence_token=request.fence_token,
            request_digest=f"request-{runner}",
        )
        preparation_digest = f"digest-{runner}"
        self.ledger.mark_preparation_ready(
            str(preparation["id"]), fence_token=request.fence_token,
            result_digest=preparation_digest, prepared={"runner": runner},
        )
        return kernel, execution, PreparedLaunch(
            request=request, preparation_id=str(preparation["id"]),
            preparation_digest=preparation_digest,
            workspace_path=str(self.root), branch_name=f"factory/{runner}",
            environment=(), commands=(), urls=(), allocation_ids=(),
        )

    def lines(self, name):
        return (FIXTURES / name).read_text().splitlines()

    def executable(self, body, name="fixture-runner"):
        path = self.root / name
        path.write_text(
            f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8"
        )
        path.chmod(0o700)
        return path

    def routes(self):
        return {
            "codex": RunnerRoute(
                "codex", "codex", "codex", "0.147.0", "approve-for-me",
            ),
            "claude": RunnerRoute(
                "claude", "claude-code", "claude", "2.1.251", "auto",
                ("runner-browser",),
            ),
            "omp": RunnerRoute(
                "omp", "omp-rpc", "omp", "18.0.4", "write",
                ("runner-browser", "runner-computer"), "ompcode",
            ),
        }

    def test_all_versioned_success_fixtures_produce_same_result(self):
        cases = (
            (CodexAdapter(), "codex-0.147.0-success.jsonl"),
            (ClaudeCodeAdapter(), "claude-2.1.251-success.jsonl"),
            (OmpRpcAdapter(), "omp-18.0.4-success.jsonl"),
        )
        for adapter, fixture in cases:
            with self.subTest(adapter=adapter.kind):
                receipt = adapter.parse(self.lines(fixture))
                self.assertEqual("succeeded", receipt.result.outcome)
                self.assertEqual("complete", receipt.result.preferred_label)
                self.assertEqual("test", receipt.result.evidence[0]["kind"])
                self.assertTrue(receipt.session_id)

    def test_commands_keep_prompt_and_secrets_out_of_argv(self):
        for runner, adapter in (
            ("codex", CodexAdapter()), ("claude", ClaudeCodeAdapter()),
            ("omp", OmpRpcAdapter()),
        ):
            _kernel, _execution, launch = self.launch(runner)
            route = self.routes()[runner]
            command = adapter.command(route, launch, session_id=None)
            self.assertNotIn("Do the task", command)
            self.assertIn("Do the task", adapter.stdin(launch, prompt_text="Do the task"))
            self.assertNotIn("secret", " ".join(command).lower())

    def test_prompt_requires_an_immutable_snapshot(self):
        _kernel, execution, launch = self.launch("codex")
        with self.assertRaisesRegex(RunnerProtocolError, "immutable prompt text"):
            CodexAdapter().stdin(launch, prompt_text="")

    def test_resume_command_is_provider_specific(self):
        session = "44444444-4444-4444-8444-444444444444"
        for runner, adapter, marker in (
            ("codex", CodexAdapter(), "resume"),
            ("claude", ClaudeCodeAdapter(), "--resume"),
            ("omp", OmpRpcAdapter(), "switch_session"),
        ):
            _kernel, _execution, launch = self.launch(runner)
            command = adapter.command(self.routes()[runner], launch, session_id=session)
            wire = " ".join(command)
            if runner == "omp":
                wire += adapter.input_payload(
                    launch, prompt_text="Do the task", session_id=session,
                ).decode("utf-8")
            self.assertIn(marker, wire)
            self.assertIn(session, wire)

    def test_malformed_missing_terminal_and_exit_only_success_fail_closed(self):
        adapter = CodexAdapter()
        with self.assertRaisesRegex(RunnerProtocolError, "malformed"):
            adapter.parse(["not-json"])
        with self.assertRaisesRegex(RunnerProtocolError, "terminal"):
            adapter.parse([json.dumps({"type": "turn.started"})])
        with self.assertRaisesRegex(RunnerProtocolError, "result proof"):
            adapter.parse([json.dumps({"type": "turn.completed"})])
        with self.assertRaisesRegex(RunnerProtocolError, "code 1"):
            adapter.parse(self.lines("codex-0.147.0-success.jsonl"), exit_code=1)

    def test_every_adapter_rejects_malformed_exit_failure_and_missing_evidence(self):
        for adapter, fixture in (
            (CodexAdapter(), "codex-0.147.0-success.jsonl"),
            (ClaudeCodeAdapter(), "claude-2.1.251-success.jsonl"),
            (OmpRpcAdapter(), "omp-18.0.4-success.jsonl"),
        ):
            with self.subTest(adapter=adapter.kind, case="malformed"):
                with self.assertRaisesRegex(RunnerProtocolError, "malformed"):
                    adapter.parse(["not-json"])
            with self.subTest(adapter=adapter.kind, case="exit"):
                with self.assertRaisesRegex(RunnerProtocolError, "code 7"):
                    adapter.parse(self.lines(fixture), exit_code=7)
            with self.subTest(adapter=adapter.kind, case="evidence"):
                with self.assertRaisesRegex(RunnerProtocolError, "missing evidence"):
                    adapter.parse(
                        self.lines(fixture), required_evidence=("screenshot",),
                    )

    def test_omp_ack_and_nonterminal_agent_end_are_not_completion(self):
        frames = [
            {"type": "ready", "supportedProtocolVersions": [1, 2]},
            {
                "type": "response", "command": "negotiate_protocol",
                "success": True, "data": {"protocolVersion": 2},
            },
            {"type": "response", "command": "prompt", "success": True},
            {"type": "agent_end", "isTerminal": False},
        ]
        with self.assertRaisesRegex(RunnerProtocolError, "terminal"):
            OmpRpcAdapter().parse([json.dumps(item) for item in frames])

    def test_omp_requires_successful_v2_negotiation(self):
        lines = self.lines("omp-18.0.4-success.jsonl")
        without_negotiation = [line for line in lines if "negotiate_protocol" not in line]
        with self.assertRaisesRegex(RunnerProtocolError, "negotiation failed"):
            OmpRpcAdapter().parse(without_negotiation)

    def test_omp_terminal_provider_failure_is_not_a_missing_proof_error(self):
        frames = [
            {"type": "ready", "supportedProtocolVersions": [1, 2]},
            {
                "type": "response", "command": "negotiate_protocol",
                "success": True, "data": {"protocolVersion": 2},
            },
            {
                "type": "agent_end", "isTerminal": True,
                "messages": [{
                    "role": "assistant", "stopReason": "error",
                    "errorMessage": "provider is unavailable", "content": [],
                }],
            },
        ]
        with self.assertRaisesRegex(
            RunnerProviderError, "provider is unavailable"
        ):
            OmpRpcAdapter().parse([json.dumps(frame) for frame in frames])

    def test_tool_output_cannot_supply_final_result_proof(self):
        proof = {
            "dotfactory_result": 1, "outcome": "succeeded",
            "preferred_label": "complete", "evidence": [],
        }
        frames = [
            {"type": "thread.started", "thread_id": "thread"},
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "output": json.dumps(proof)},
            },
            {"type": "turn.completed"},
        ]
        with self.assertRaisesRegex(RunnerProtocolError, "result proof"):
            CodexAdapter().parse([json.dumps(item) for item in frames])

    def test_omp_input_and_approval_are_explicit_events(self):
        lines = self.lines("omp-18.0.4-success.jsonl")
        lines.insert(2, json.dumps({
            "type": "extension_ui_request", "method": "confirm", "id": "a",
        }))
        lines.insert(3, json.dumps({
            "type": "extension_ui_request", "method": "input", "id": "b",
        }))
        kinds = [event.kind for event in OmpRpcAdapter().parse(lines).events]
        self.assertIn("approval", kinds)
        self.assertIn("input", kinds)

    def test_omp_display_only_ui_updates_do_not_request_attention(self):
        for method in (
            "notify", "setStatus", "setWidget", "setTitle", "set_editor_text",
        ):
            with self.subTest(method=method):
                event = OmpRpcAdapter().frame_event({
                    "type": "extension_ui_request", "method": method,
                })
                self.assertEqual("protocol", event.kind)

    def test_router_rejects_raw_unknown_and_stale_launches(self):
        kernel, execution, launch = self.launch("codex")
        router = LiveRunnerRouter(self.ledger, self.routes())
        with self.assertRaisesRegex(PreparationError, "PreparedLaunch"):
            router.route(launch.request)
        route, adapter = router.route(launch)
        self.assertEqual("codex", route.name)
        self.assertIsInstance(adapter, CodexAdapter)
        kernel.complete_attempt(
            execution, preferred_label="complete", outcome="external",
            evidence=[{"kind": "proof", "uri": "local://external"}],
            attempt_id=launch.request.attempt_id,
            fence_token=launch.request.fence_token, owner=launch.request.owner,
            command_id="external-complete",
        )
        with self.assertRaises(StaleAttempt):
            router.route(launch)

    def test_capability_report_is_fallback_data(self):
        router = LiveRunnerRouter(self.ledger, self.routes())
        available = router.capability_report(
            "claude", available=True, version="2.1.251",
        )
        missing = router.capability_report(
            "codex", available=False, version=None, reason="not installed",
        )
        self.assertTrue(available.supports("runner-browser"))
        self.assertFalse(missing.supports("runner-browser"))
        self.assertEqual("not installed", missing.reason)

    def test_live_supervisor_drains_both_streams_and_persists_trace_receipt(self):
        _kernel, execution, launch = self.launch("codex")
        secret = "-".join(("fixture", "secret", "value"))
        lines = self.lines("codex-0.147.0-success.jsonl")
        executable = self.executable(f"""
            import sys

            sys.stdin.read()
            sys.stderr.write("x" * 524288 + {secret!r} + "\\n")
            sys.stderr.flush()
            for line in {lines!r}:
                sys.stdout.write(line + "\\n")
                sys.stdout.flush()
        """)
        route = RunnerRoute(
            "codex", "codex", str(executable), "0.147.0", "approve-for-me",
            environment_envs=("TEST_RUNNER_SECRET",), silence_timeout_seconds=3,
            termination_grace_seconds=1,
        )
        runner = LiveRunner(
            self.ledger, routes={"codex": route},
            observed_versions={"codex": "0.147.0"},
            environment={
                "HOME": str(self.root), "PATH": os.environ["PATH"],
                "TEST_RUNNER_SECRET": secret,
            },
        )
        result = runner.run(launch)
        self.assertEqual("succeeded", result.outcome)
        stored = self.ledger.runner_run_for_attempt(launch.request.attempt_id)
        self.assertEqual("result_ready", stored["status"])
        self.assertEqual(32, len(stored["trace_id"]))
        self.assertEqual(16, len(stored["root_span_id"]))
        self.assertEqual("test", stored["result"]["evidence"][0]["kind"])
        self.assertTrue(any(event["stream"] == "stderr" for event in stored["events"]))
        durable = json.dumps(stored, sort_keys=True)
        self.assertNotIn(secret, durable)
        self.assertNotIn("dotfactory_result", durable)
        self.assertEqual(0, stored["receipt"]["exit_code"])
        runner_trace = [
            item for item in self.ledger.trace_page(execution, limit=1000)
            if item["source_kind"] == "runner_event"
        ]
        self.assertEqual(len(stored["events"]), len(runner_trace))
        self.assertNotIn(secret, json.dumps(runner_trace, sort_keys=True))

    def test_tool_result_span_is_parented_to_its_tool_call(self):
        _kernel, execution, launch = self.launch("codex")
        proof = {
            "dotfactory_result": 1, "outcome": "succeeded",
            "preferred_label": "complete",
            "evidence": [{"kind": "test", "uri": "local://trace-proof"}],
        }
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thread"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({
                "type": "item.started",
                "item": {"id": "tool-1", "type": "command_execution"},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "tool-1", "type": "command_execution"},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "message-1", "type": "agent_message",
                         "text": json.dumps(proof)},
            }),
            json.dumps({"type": "turn.completed"}),
        ]
        executable = self.executable(f"""
            import sys

            sys.stdin.read()
            for line in {lines!r}:
                sys.stdout.write(line + "\\n")
                sys.stdout.flush()
        """)
        route = RunnerRoute(
            "codex", "codex", str(executable), "0.147.0", "approve-for-me",
        )
        LiveRunner(
            self.ledger, routes={"codex": route},
            observed_versions={"codex": "0.147.0"},
            environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
        ).run(launch)
        events = self.ledger.runner_run_for_attempt(
            launch.request.attempt_id
        )["events"]
        tool_call = next(event for event in events if event["kind"] == "tool_call")
        tool_result = next(
            event for event in events if event["kind"] == "tool_result"
        )
        self.assertEqual(tool_call["span_id"], tool_result["parent_span_id"])
        trace = self.ledger.trace_page(execution, limit=1000)
        tool_spans = [
            item for item in trace
            if item["source_kind"] == "runner_event"
            and item["entity_id"] == "tool-1"
        ]
        self.assertEqual(2, len(tool_spans))
        self.assertEqual(tool_spans[0]["span_id"], tool_spans[1]["span_id"])
        self.assertEqual(tool_spans[0]["parent_span_id"], tool_spans[1]["parent_span_id"])

    def test_silence_timeout_terminates_owned_runner_and_records_error(self):
        _kernel, execution, launch = self.launch("codex")
        executable = self.executable("""
            import sys
            import time

            sys.stdin.read()
            time.sleep(30)
        """)
        route = RunnerRoute(
            "codex", "codex", str(executable), "0.147.0", "approve-for-me",
            silence_timeout_seconds=1, termination_grace_seconds=1,
        )
        runner = LiveRunner(
            self.ledger, routes={"codex": route},
            observed_versions={"codex": "0.147.0"},
            environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
        )
        with self.assertRaisesRegex(RunnerExecutionError, "silence timeout"):
            runner.run(launch)
        stored = self.ledger.runner_run_for_attempt(launch.request.attempt_id)
        self.assertEqual("failed", stored["status"])
        self.assertEqual("timeout", stored["error"]["class"])
        self.assertTrue(stored["error"]["retryable"])
        errors = self.ledger.error_page(execution)
        self.assertTrue(any(item["category"] == "timeout" for item in errors))
        with self.assertRaises(ProcessLookupError):
            os.kill(int(stored["pid"]), 0)

    def test_wall_timeout_is_independent_from_protocol_silence(self):
        _kernel, _execution, launch = self.launch("codex")
        launch.request.config["timeout"] = "1s"
        executable = self.executable("""
            import sys
            import time

            sys.stdin.read()
            time.sleep(30)
        """)
        route = RunnerRoute(
            "codex", "codex", str(executable), "0.147.0", "approve-for-me",
            silence_timeout_seconds=10, termination_grace_seconds=1,
        )
        runner = LiveRunner(
            self.ledger, routes={"codex": route},
            observed_versions={"codex": "0.147.0"},
            environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
        )
        with self.assertRaisesRegex(RunnerExecutionError, "wall timeout"):
            runner.run(launch)
        stored = self.ledger.runner_run_for_attempt(launch.request.attempt_id)
        self.assertEqual("timeout", stored["error"]["class"])

    def test_explicit_cancellation_is_distinct_from_failure(self):
        _kernel, _execution, launch = self.launch("codex")
        executable = self.executable("""
            import sys
            import time

            sys.stdin.read()
            time.sleep(30)
        """)
        route = RunnerRoute(
            "codex", "codex", str(executable), "0.147.0", "approve-for-me",
            silence_timeout_seconds=10, termination_grace_seconds=1,
        )
        runner = LiveRunner(
            self.ledger, routes={"codex": route},
            observed_versions={"codex": "0.147.0"},
            environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
            cancel_requested=lambda _runner_run_id: True,
        )
        with self.assertRaisesRegex(RunnerExecutionError, "cancellation"):
            runner.run(launch)
        stored = self.ledger.runner_run_for_attempt(launch.request.attempt_id)
        self.assertEqual("canceled", stored["status"])
        self.assertEqual("canceled", stored["error"]["class"])

    def test_omp_approval_becomes_durable_attention(self):
        _kernel, _execution, launch = self.launch("omp")
        executable = self.executable("""
            import json
            import sys
            import time

            frames = [
                {"type": "ready", "supportedProtocolVersions": [1, 2]},
                {"id": "protocol-1", "type": "response",
                 "command": "negotiate_protocol", "success": True,
                 "data": {"protocolVersion": 2}},
                {"type": "extension_ui_request", "id": "approval-1",
                 "method": "confirm", "message": "Continue?"},
            ]
            for frame in frames:
                sys.stdout.write(json.dumps(frame) + "\\n")
                sys.stdout.flush()
            time.sleep(30)
        """)
        route = RunnerRoute(
            "omp", "omp-rpc", str(executable), "18.0.4", "write",
            silence_timeout_seconds=5, termination_grace_seconds=1,
        )
        runner = LiveRunner(
            self.ledger, routes={"omp": route},
            observed_versions={"omp": "18.0.4"},
            environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
        )
        with self.assertRaises(RunnerNeedsAttention) as raised:
            runner.run(launch)
        stored = self.ledger.runner_run_for_attempt(launch.request.attempt_id)
        self.assertEqual("waiting_input", stored["status"])
        self.assertEqual(raised.exception.attention_id, stored["attention_id"])
        attention = self.ledger.attention(stored["attention_id"])
        self.assertEqual("runner-approval", attention["category"])
        self.assertEqual(["retry", "cancel"], attention["detail"]["allowed_actions"])
        self.assertNotIn("Continue?", json.dumps(stored, sort_keys=True))

    def test_authorized_attention_retry_resumes_the_recorded_omp_session(self):
        kernel, execution, launch = self.launch("omp")
        proof = {
            "dotfactory_result": 1, "outcome": "succeeded",
            "preferred_label": "complete",
            "evidence": [{"kind": "test", "uri": "local://resume-proof"}],
        }
        executable = self.executable(f"""
            import json
            import sys
            import time

            def send(frame):
                sys.stdout.write(json.dumps(frame) + "\\n")
                sys.stdout.flush()

            send({{"type": "ready", "supportedProtocolVersions": [1, 2]}})
            negotiate = json.loads(sys.stdin.readline())
            send({{"id": negotiate["id"], "type": "response",
                  "command": "negotiate_protocol", "success": True,
                  "data": {{"protocolVersion": 2}}}})
            next_frame = json.loads(sys.stdin.readline())
            resumed = next_frame.get("type") == "switch_session"
            if resumed:
                send({{"id": next_frame["id"], "type": "response",
                      "command": "switch_session", "success": True}})
                next_frame = json.loads(sys.stdin.readline())
            send({{"type": "agent_start", "sessionFile": "fixture-session.jsonl"}})
            if resumed:
                send({{"type": "agent_end", "isTerminal": True,
                      "sessionFile": "fixture-session.jsonl",
                      "messages": [{{"role": "assistant", "content": [
                          {{"type": "text", "text": json.dumps({proof!r})}}
                      ]}}]}})
            else:
                send({{"type": "extension_ui_request", "id": "approval-resume",
                      "method": "confirm", "message": "Continue?"}})
                time.sleep(30)
        """, name="resumable-omp")
        route = RunnerRoute(
            "omp", "omp-rpc", str(executable), "18.0.4", "write",
            silence_timeout_seconds=5, termination_grace_seconds=1,
        )
        runner = LiveRunner(
            self.ledger, routes={"omp": route},
            observed_versions={"omp": "18.0.4"},
            environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
        )
        with self.assertRaises(RunnerNeedsAttention) as raised:
            runner.run(launch)
        receipt = ControlService(
            self.ledger, kernel, attention_controllers={"omp": runner},
        ).execute(
            execution, command_id="resume-omp", principal=Principal(
                "toma", "operator", "test"
            ), request={
                "action": "attention", "expected_state": "Autoplanning",
                "parameters": {
                    "attention_id": raised.exception.attention_id,
                    "remedy": "retry",
                    "expected_attempt_id": launch.request.attempt_id,
                },
            },
        )
        self.assertEqual("completed", receipt["status"])
        self.assertEqual("resume_authorized", self.ledger.runner_run_for_attempt(
            launch.request.attempt_id
        )["status"])
        result = runner.run(launch)
        self.assertEqual("succeeded", result.outcome)
        stored = self.ledger.runner_run_for_attempt(launch.request.attempt_id)
        self.assertEqual("result_ready", stored["status"])
        self.assertEqual(1, stored["resume_count"])
        self.assertEqual("fixture-session.jsonl", stored["session_id"])

    def test_stale_fence_before_spawn_never_starts_process(self):
        kernel, execution, launch = self.launch("codex")
        marker = self.root / "spawned"
        executable = self.executable(f"""
            from pathlib import Path
            Path({str(marker)!r}).write_text("spawned")
        """)
        route = RunnerRoute(
            "codex", "codex", str(executable), "0.147.0", "approve-for-me",
        )

        def fence(boundary):
            if boundary == "after_runner_start_intent":
                kernel.complete_attempt(
                    execution, preferred_label="complete", outcome="external",
                    evidence=[{"kind": "proof", "uri": "local://external"}],
                    attempt_id=launch.request.attempt_id,
                    fence_token=launch.request.fence_token,
                    owner=launch.request.owner, command_id="external-fence",
                )

        runner = LiveRunner(
            self.ledger, routes={"codex": route}, fault_hook=fence,
            observed_versions={"codex": "0.147.0"},
            environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
        )
        with self.assertRaises(StaleAttempt):
            runner.run(launch)
        self.assertFalse(marker.exists())
        stored = self.ledger.runner_run_for_attempt(launch.request.attempt_id)
        self.assertEqual("superseded", stored["status"])

    def test_stale_fence_before_result_commit_cannot_publish_success(self):
        kernel, execution, launch = self.launch("codex")
        lines = self.lines("codex-0.147.0-success.jsonl")
        executable = self.executable(f"""
            import sys

            sys.stdin.read()
            for line in {lines!r}:
                sys.stdout.write(line + "\\n")
                sys.stdout.flush()
        """)
        route = RunnerRoute(
            "codex", "codex", str(executable), "0.147.0", "approve-for-me",
        )

        def fence(boundary):
            if boundary == "before_runner_result_commit":
                kernel.complete_attempt(
                    execution, preferred_label="complete", outcome="external",
                    evidence=[{"kind": "proof", "uri": "local://external"}],
                    attempt_id=launch.request.attempt_id,
                    fence_token=launch.request.fence_token,
                    owner=launch.request.owner, command_id="external-result-fence",
                )

        runner = LiveRunner(
            self.ledger, routes={"codex": route}, fault_hook=fence,
            observed_versions={"codex": "0.147.0"},
            environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
        )
        with self.assertRaises(StaleAttempt):
            runner.run(launch)
        stored = self.ledger.runner_run_for_attempt(launch.request.attempt_id)
        self.assertEqual("superseded", stored["status"])
        self.assertIsNone(stored["result"])

    def test_event_limit_records_capture_drop_before_failing(self):
        _kernel, execution, launch = self.launch("codex")
        lines = self.lines("codex-0.147.0-success.jsonl")
        executable = self.executable(f"""
            import sys

            sys.stdin.read()
            for line in {lines!r}:
                sys.stdout.write(line + "\\n")
                sys.stdout.flush()
        """)
        route = RunnerRoute(
            "codex", "codex", str(executable), "0.147.0", "approve-for-me",
            maximum_events=1,
        )
        runner = LiveRunner(
            self.ledger, routes={"codex": route},
            observed_versions={"codex": "0.147.0"},
            environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
        )
        with self.assertRaisesRegex(RunnerExecutionError, "event limit"):
            runner.run(launch)
        stored = self.ledger.runner_run_for_attempt(launch.request.attempt_id)
        self.assertEqual(1, stored["dropped_event_count"])
        self.assertTrue(any(
            event["protocol_type"] == "dotfactory.capture_dropped"
            for event in stored["events"]
        ))
        capture = [
            item for item in self.ledger.trace_page(execution, limit=1000)
            if item["phase"] == "dotfactory.capture_dropped"
        ]
        self.assertEqual(["records_dropped"], capture[0]["completeness"]["reasons"])

    def test_oversized_event_payload_is_replaced_by_capture_metadata(self):
        _kernel, _execution, launch = self.launch("codex")
        lines = self.lines("codex-0.147.0-success.jsonl")
        executable = self.executable(f"""
            import sys

            sys.stdin.read()
            sys.stderr.write("payload-" + "x" * 500 + "\\n")
            sys.stderr.flush()
            for line in {lines!r}:
                sys.stdout.write(line + "\\n")
                sys.stdout.flush()
        """)
        route = RunnerRoute(
            "codex", "codex", str(executable), "0.147.0", "approve-for-me",
            maximum_payload_bytes=128,
        )
        LiveRunner(
            self.ledger, routes={"codex": route},
            observed_versions={"codex": "0.147.0"},
            environment={"HOME": str(self.root), "PATH": os.environ["PATH"]},
        ).run(launch)
        events = self.ledger.runner_run_for_attempt(
            launch.request.attempt_id
        )["events"]
        truncated = [event for event in events if event["truncated"]]
        self.assertTrue(truncated)
        self.assertTrue(all(
            event["payload"]["capture"] == "truncated" for event in truncated
        ))

    def test_preflight_checks_executable_and_minimum_version(self):
        executable = self.executable("""
            print("codex-cli 0.147.1")
        """)
        route = RunnerRoute(
            "codex", "codex", str(executable), "0.147.0", "approve-for-me",
        )
        report = LiveRunnerRouter(
            self.ledger, {"codex": route}
        ).preflight("codex")
        self.assertTrue(report.available)
        self.assertEqual("0.147.1", report.version)

    def test_omp_v2_decoder_validates_chunk_sequences(self):
        logical = json.dumps({"type": "message_end", "text": "hello"}).encode()
        parts = (logical[:12], logical[12:])
        frames = [json.dumps({
            "type": "rpc_chunk", "chunkId": "chunk-1", "index": index,
            "count": len(parts), "byteLength": len(logical),
            "data": base64.b64encode(part).decode(),
        }) for index, part in enumerate(parts)]
        decoder = OmpRpcFrameDecoder(maximum_reassembled_frame_bytes=1024)
        self.assertEqual((), decoder.feed(frames[0]))
        self.assertEqual("message_end", decoder.feed(frames[1])[0]["type"])
        decoder.finish()

        out_of_order = json.loads(frames[1])
        out_of_order["index"] = 1
        with self.assertRaisesRegex(RunnerProtocolError, "start at zero"):
            OmpRpcFrameDecoder().feed(json.dumps(out_of_order))

    def test_schema_eight_migrates_runner_trace_tables(self):
        path = self.root / "schema-eight.db"
        ledger = SQLiteLedger(path)
        ledger.close()
        database = sqlite3.connect(path)
        database.execute("DROP TABLE runner_events")
        database.execute("DROP TABLE runner_runs")
        database.execute("PRAGMA user_version=8")
        database.commit()
        database.close()
        migrated = SQLiteLedger(path)
        self.assertEqual(10, migrated.connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0])
        tables = {
            row[0] for row in migrated.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("runner_runs", tables)
        self.assertIn("runner_events", tables)
        migrated.close()


class RunnerConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.values = json.loads((ROOT / "factory.example.json").read_text())
        self.values["ledger_path"] = str(self.root / "factory.db")
        for project in self.values["projects"].values():
            project.pop("repository_path_env", None)
            project["repository_path"] = str(self.root / project["display_name"])
            tracker = project["tracker"]
            tracker.pop("project_id_env", None)
            tracker["project_id"] = "linear-" + project["display_name"]

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        path = self.root / "factory.json"
        path.write_text(json.dumps(self.values))
        return path

    def test_schema_six_resolves_runner_registry(self):
        config = FactoryConfig.load(self.write())
        runners = config.resolve_runners()
        self.assertEqual("omp-rpc", runners["omp"]["kind"])
        self.assertEqual("dotfactory-claude-api", runners["omp"]["profile"])
        self.assertEqual(
            ("ANTHROPIC_API_KEY",), runners["omp"]["environment_envs"]
        )
        self.assertEqual("codex", config.validate_runner_name("codex"))

    def test_invalid_runner_registry_blocks_activation(self):
        for key, value in (
            ("kind", "unknown"), ("command", ""),
            ("capabilities", ["Bad Name"]),
        ):
            with self.subTest(key=key):
                original = self.values["runners"]["codex"][key]
                self.values["runners"]["codex"][key] = value
                with self.assertRaises(ValueError):
                    FactoryConfig.load(self.write())
                self.values["runners"]["codex"][key] = original

    def test_runner_limits_and_environment_names_are_validated(self):
        runner = self.values["runners"]["codex"]
        runner["environment_envs"] = ["OPENAI_API_KEY"]
        runner["silence_timeout_seconds"] = 30
        resolved = FactoryConfig.load(self.write()).resolve_runners()["codex"]
        self.assertEqual(("OPENAI_API_KEY",), resolved["environment_envs"])
        self.assertEqual(30, resolved["silence_timeout_seconds"])
        runner["environment_envs"] = ["not-an-env"]
        with self.assertRaisesRegex(ValueError, "environment variable names"):
            FactoryConfig.load(self.write())

    def test_schema_five_has_no_live_runners(self):
        self.values["schema_version"] = 5
        self.values.pop("runners")
        self.assertEqual({}, FactoryConfig.load(self.write()).resolve_runners())


if __name__ == "__main__":
    unittest.main()
