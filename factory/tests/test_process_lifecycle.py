"""Real CLI/HTTP process lifecycle with a deterministic provider protocol fixture."""
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from dotfactory.cli import _demo_config


class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-process-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.children = []
        self.orphan_groups = []
        self.config = _demo_config(self.root)
        self.mode = self.root / "mode"
        self.mode.write_text("complete")
        runner = self.root / "controlled-runner"
        runner.write_text(f'''#!{sys.executable}
import json, os, pathlib, re, subprocess, sys, time
root = pathlib.Path({str(self.root)!r})
if "--version" in sys.argv:
    print("codex 0.147.0")
    sys.exit(0)
prompt = sys.stdin.read()
if (root / "mode").read_text() == "hold":
    child = subprocess.Popen([sys.executable, "-c", "import pathlib,time; p=pathlib.Path(" + repr(str(root / "heartbeat")) + "); exec('while True: p.write_text(str(time.time())); time.sleep(0.05)')"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (root / "descendant-pid").write_text(str(child.pid))
(root / "effects").open("a").write("one-dispatch\\n")
(root / "runner-pid").write_text(str(os.getpid()))
print(json.dumps({{"type":"thread.started","thread_id":"process-fixture"}}), flush=True)
if (root / "mode").read_text() == "hold":
    print(json.dumps({{"type":"error","message":"CONTROLLED_INTERRUPTION: side effect recorded before crash"}}), flush=True)
deadline = time.monotonic() + 25
while (root / "mode").read_text() == "hold":
    if time.monotonic() > deadline:
        sys.exit(8)
    time.sleep(0.05)
if (root / "mode").read_text() == "fail-once":
    (root / "mode").write_text("complete")
    sys.exit(7)
labels = re.findall(r"preferred_label must be exactly one of: ([^.]+)", prompt)[-1].split(", ")
label = "retry" if "retry" in labels else "complete"
proof = {{"dotfactory_result":1,"outcome":"Controlled process completed", "preferred_label":label,
          "evidence":[{{"kind":"fixture","uri":"README.md"}}]}}
print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":json.dumps(proof)}}}}), flush=True)
print(json.dumps({{"type":"turn.completed"}}), flush=True)
''')
        runner.chmod(0o700)
        values = json.loads(self.config.read_text())
        values["runners"]["codex"]["command"] = str(runner)
        values["preparation"]["workspace"].update(root=str(self.root / "workspaces"), retention="explicit")
        self.config.write_text(json.dumps(values))
        self.task = self.root / "task.md"
        self.task.write_text("Controlled disposable lifecycle test; no network.")
        self.env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                        PYTHONDONTWRITEBYTECODE="1")
        self.base = [sys.executable, "-m", "dotfactory"]

    def tearDown(self):
        for group in self.orphan_groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in reversed(self.children):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
        self.temp.cleanup()

    def start(self, arguments, env=None):
        process = subprocess.Popen(self.base + arguments, env=env or self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        return process

    def worker(self):
        return self.start(["run", "--config", str(self.config), "--project", "demo",
                           "--issue", "PROCESS-1", "--description-file", str(self.task),
                           "--until-state", "Review", "--max-ticks", "8"])

    def finish(self, process):
        out, err = process.communicate(timeout=30)
        self.assertTrue(out, err)
        return json.loads(out)

    def query(self, sql):
        with sqlite3.connect("file:" + str(self.root / "factory.db") + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql)]

    def await_runner(self, process):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (self.root / "runner-pid").exists():
                return int((self.root / "runner-pid").read_text())
            self.assertIsNone(process.poll(), "worker exited before controlled runner started")
            time.sleep(0.05)
        self.fail("controlled runner did not start")

    def assert_stopped(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual([], self.query("SELECT id FROM runner_runs WHERE status='running'"))
        child_pid = (self.root / "descendant-pid").read_text()
        state = subprocess.run(["ps", "-p", child_pid, "-o", "stat="], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not state or state.startswith("Z"), "runner descendant remains active: " + state)

    def test_sigterm_stops_runner_and_restart_reuses_execution(self):
        self.mode.write_text("hold")
        first = self.worker()
        pid = self.await_runner(first)
        execution = self.query("SELECT id FROM workflow_executions")[0]["id"]
        first.send_signal(signal.SIGTERM)
        stopped = self.finish(first)
        self.assertEqual("signal", stopped["shutdown_reason"])
        self.assert_stopped(pid)
        self.mode.write_text("complete")
        restarted = self.worker()
        result = self.finish(restarted)
        self.assertEqual("target_state", result["shutdown_reason"])
        self.assertEqual(0, restarted.returncode)
        self.assertEqual([{"id": execution, "current_state_id": "Review"}],
                         self.query("SELECT id,current_state_id FROM workflow_executions"))

    def test_http_cancel_is_idempotent_and_stops_owned_runner(self):
        self.mode.write_text("hold")
        worker = self.worker()
        pid = self.await_runner(worker)
        execution = self.query("SELECT id FROM workflow_executions")[0]["id"]
        token = secrets.token_urlsafe(32)
        gateway = self.start(["serve", "--config", str(self.config), "--port", "0", "--role", "operator"],
                             dict(self.env, DOTFACTORY_API_TOKEN=token))
        url = json.loads(gateway.stdout.readline())["url"]
        payload = json.dumps({"action": "cancel", "expected_state": "Autoplanning", "confirmed": True,
                              "parameters": {"reason": "Disposable lifecycle test"}}).encode()
        def cancel():
            request = urllib.request.Request(url + "/v1/runs/" + execution + "/commands", data=payload,
                       headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                "Idempotency-Key": "process-cancel"})
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)
        first = cancel()
        self.finish(worker)
        self.assertEqual(first, cancel())
        self.assert_stopped(pid)
        self.assertEqual("Canceled", self.query("SELECT current_state_id FROM workflow_executions")[0]["current_state_id"])
        self.assertEqual(1, len(self.query("SELECT id FROM transition_decisions WHERE to_state='Canceled'")))
        # Explicit retention preserves the worktree rather than silently removing it.
        self.assertTrue(any((self.root / "workspaces").iterdir()))

    def test_provider_failure_dispatches_investigation_then_recovers(self):
        self.mode.write_text("fail-once")
        worker = self.worker()
        receipt = self.finish(worker)
        self.assertEqual("target_state", receipt["shutdown_reason"])
        self.assertEqual(0, worker.returncode)
        edges = self.query("SELECT from_state,to_state FROM transition_decisions ORDER BY event_seq")
        self.assertIn({"from_state": "Autoplanning", "to_state": "Investigating"}, edges)
        self.assertIn({"from_state": "Investigating", "to_state": "Autoplanning"}, edges)
        self.assertEqual("Review", edges[-1]["to_state"])
        self.assertEqual([], self.query("SELECT id FROM runner_runs WHERE status='running'"))

    def test_sigkill_recovery_cancels_without_replaying_uncertain_side_effect(self):
        values = json.loads(self.config.read_text())
        values["preparation"]["workspace"]["retention"] = "until_terminal"
        values["scheduler"]["poll_interval_ms"] = 100
        self.config.write_text(json.dumps(values))
        self.mode.write_text("hold")
        worker = self.worker()
        pid = self.await_runner(worker)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.query("SELECT error_id FROM error_facts WHERE message LIKE 'CONTROLLED_INTERRUPTION:%'"):
                break
            time.sleep(0.05)
        original_errors = self.query("SELECT error_id,message FROM error_facts")
        self.assertTrue(any("CONTROLLED_INTERRUPTION" in row["message"] for row in original_errors))
        original_run = self.query("SELECT id FROM runner_runs")[0]["id"]
        execution = self.query("SELECT id FROM workflow_executions")[0]["id"]
        # Kill only the coordinator first: the fixture-owned child survives.
        self.orphan_groups.append(pid)
        worker.kill()
        worker.communicate(timeout=5)
        restarted = self.start(["run", "--config", str(self.config), "--project", "demo",
                                "--issue", "PROCESS-1", "--watch", "--max-ticks", "100"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.query("SELECT id FROM attention_requests WHERE status='open'"):
                break
            time.sleep(0.05)
        self.assertEqual("ambiguous-dispatch", self.query(
            "SELECT category FROM attention_requests WHERE status='open'")[0]["category"])
        self.assertEqual("one-dispatch\n", (self.root / "effects").read_text())
        token = secrets.token_urlsafe(32)
        gateway = self.start(["serve", "--config", str(self.config), "--port", "0", "--role", "operator"],
                             dict(self.env, DOTFACTORY_API_TOKEN=token))
        url = json.loads(gateway.stdout.readline())["url"]
        payload = json.dumps({"action": "cancel", "expected_state": "Autoplanning", "confirmed": True}).encode()
        def cancel():
            request = urllib.request.Request(url + "/v1/runs/" + execution + "/commands", data=payload,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                         "Idempotency-Key": "resolve-interrupted-run"})
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)
        first = cancel()
        self.assertEqual(first, cancel())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.query("SELECT status FROM execution_workspaces") == [{"status": "quarantined"}]:
                break
            time.sleep(0.05)
        self.assertEqual([{"status": "quarantined"}], self.query("SELECT status FROM execution_workspaces"))
        os.kill(pid, 0)  # cancellation did not claim ownership of the orphan
        self.assertTrue(any((self.root / "workspaces").iterdir()))
        release_id = self.query("SELECT id FROM attention_requests WHERE status='open' AND category='uncertain-runner-exit'")[0]["id"]
        # Only the test knows the original process group; inspect/stop before release.
        os.killpg(pid, signal.SIGKILL)
        self.orphan_groups.remove(pid)
        release = subprocess.run(self.base + ["attention", "--config", str(self.config), "--project", "demo",
            "--execution", execution, "--attention-id", release_id, "--expected-state", "Canceled",
            "--remedy", "release", "--confirm", "--command-id", "release-inspected-workspace"],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(0, release.returncode, release.stdout + release.stderr)
        drain = subprocess.run(self.base + ["operator", "drain", "--config", str(self.config),
                               "--project", "demo"], env=self.env, capture_output=True, text=True)
        self.assertEqual(0, drain.returncode, drain.stderr)
        self.finish(restarted)
        self.assertEqual("one-dispatch\n", (self.root / "effects").read_text())
        self.assertEqual([{"id": original_run, "status": "canceled"}],
                         self.query("SELECT id,status FROM runner_runs"))
        self.assertEqual([], self.query("SELECT id FROM attention_requests WHERE status='open'"))
        self.assertEqual([{"status": "cleaned"}], self.query("SELECT status FROM execution_workspaces"))
        self.assertEqual([{"status": "superseded"}], self.query("SELECT status FROM scheduler_dispatches"))
        self.assertEqual(1, len(self.query("SELECT id FROM transition_decisions WHERE to_state='Canceled'")))
        self.assertTrue(all(row in self.query("SELECT error_id,message FROM error_facts") for row in original_errors))
        request = urllib.request.Request(url + "/v1/runs/" + execution + "/waterfall",
                                         headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=15) as response:
            waterfall = json.load(response)
        self.assertEqual(0, waterfall["data"]["open_span_count"])
