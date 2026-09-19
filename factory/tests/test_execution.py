import json
import base64
import os
import subprocess
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotfactory.cli import _demo_config
from dotfactory.instance import FactoryConfig, FactoryConfigError
from dotfactory.lifecycle import FactoryRuntime
from dotfactory.execution import ExecutionError, call, transport_command, validate_policy
from dotfactory import worker


class WorkerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = _demo_config(self.root)
        self.fixture = self.root / "codex-fixture"
        self.fixture.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
if '--version' in sys.argv:
    print('codex-cli 1.0.0')
elif sys.argv[1:3] == ['login', 'status']:
    print('Logged in using ChatGPT')
elif sys.argv[1:3] == ['mcp', 'list']:
    print('[]')
else:
    text = sys.stdin.read()
    path = Path('worker-proof.txt')
    old = path.read_text() if path.exists() else ''
    path.write_text(old + 'completed stage\\n')
    result = {'dotfactory_result': 1, 'outcome': 'succeeded', 'preferred_label': 'complete',
              'evidence': [{'kind': 'test', 'uri': 'local://worker-proof.txt'}]}
    print(json.dumps({'type': 'thread.started', 'thread_id': 'fixture-thread'}))
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(result)}}))
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))
''')
        self.fixture.chmod(0o755)
        self.policy = {
            "workers": {"cloud": {"transport": "local", "root": str(self.root / "cloud"), "billing": "subscription"},
                        "mac": {"transport": "local", "root": str(self.root / "mac"), "billing": "subscription"}},
            "stages": {state: {"workers": ["cloud"], "scope": "portable", "requires": ["tool:git"], "checks": []}
                       for state in ("Autoplanning", "Implementing", "Verifying", "Investigating", "Reworking")},
        }
        self.policy["stages"]["Verifying"]["workers"] = ["mac"]
        self.policy["stages"]["Verifying"]["checks"] = [[sys.executable, "-c", "from pathlib import Path; assert Path('worker-proof.txt').read_text().count('completed stage') == 3"]]
        values = json.loads(self.path.read_text())
        values["runners"]["codex"]["command"] = str(self.fixture)
        values["execution"] = self.policy
        self.path.write_text(json.dumps(values))

    def tearDown(self):
        self.temp.cleanup()

    def test_location_is_explicit_and_independent_of_transport(self):
        for transport in ("local", "ssh"):
            for location in ("local", "cloud"):
                config = self.policy["workers"]["cloud"]
                config.update(transport=transport, location=location,
                              target="builder@host", entrypoint="/opt/worker.py")
                validate_policy(self.policy)
        self.policy["workers"]["cloud"]["location"] = "render"
        with self.assertRaisesRegex(ValueError, "worker location"):
            validate_policy(self.policy)

    def test_local_dispatch_preserves_environment_that_passed_precheck(self):
        values = json.loads(self.path.read_text())
        values["runners"]["codex"]["environment_envs"] = ["WORKER_TEST_MARKER"]
        self.path.write_text(json.dumps(values))
        self.fixture.write_text(self.fixture.read_text().replace(
            "import json, sys", "import json, sys, os\nassert os.environ['WORKER_TEST_MARKER'] == 'present'\nassert os.environ['CODEX_HOME'] == '/tmp/worker-native-home'"))
        with patch.dict(os.environ, {"WORKER_TEST_MARKER": "present", "CODEX_HOME": "/tmp/worker-native-home"}):
            with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
                execution = runtime.start_issue("demo", "DEMO-ENV")
                runtime.run([execution], max_ticks=20)
                self.assertEqual("Review", runtime.ledger.current(execution)["current_state_id"])

    def watchdog_fixture(self, body, payload):
        directory = self.root / "watchdog"
        directory.mkdir()
        (directory / "repo").mkdir()
        (directory / "manifest.json").write_text(json.dumps({"binding": "watchdog"}))
        pidfile = self.root / "watchdog.pid"
        self.fixture.write_text("#!/usr/bin/env python3\nimport os,sys,time,subprocess\nfrom pathlib import Path\n"
                                "if '--version' in sys.argv: print('codex-cli 1.0.0')\n"
                                "elif sys.argv[1:3] == ['login','status']: print('Logged in using ChatGPT')\n"
                                "else:\n Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid()))\n " + body + "\n")
        spec = {"op": "exec", "root": str(self.root), "identity": "watchdog", "binding": "watchdog",
                "kind": "codex", "billing": "subscription", "argv": [str(self.fixture)],
                "input": base64.b64encode(payload).decode(), "timeout_seconds": 1}
        process = subprocess.Popen([sys.executable, "-I", worker.__file__], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            process.communicate(json.dumps(spec).encode(), timeout=8)
            self.assertNotEqual(process.returncode, 0)
            self.assertNotEqual(json.loads((directory / "exit.json").read_text())["exit_code"], 0)
        finally:
            if pidfile.exists():
                try:
                    os.killpg(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()

    def test_worker_deadline_covers_a_native_child_that_never_reads_input(self):
        self.watchdog_fixture("time.sleep(30)", b"x" * 1024 * 1024)

    def test_worker_deadline_covers_pipes_held_after_native_parent_exit(self):
        self.watchdog_fixture("subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])", b"")

    def test_cloud_to_mac_handoff_runs_real_subprocesses_and_survives_restart(self):
        values = json.loads(self.path.read_text())
        values["execution"]["workers"]["cloud"]["location"] = "cloud"
        values["execution"]["workers"]["mac"]["location"] = "local"
        self.path.write_text(json.dumps(values))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1", title="Worker integration")
            receipt = runtime.run([execution], max_ticks=20)
            self.assertEqual("Review", runtime.ledger.current(execution)["current_state_id"], receipt.as_dict())
            rows = runtime.ledger.connection.execute("SELECT manifest_json FROM worker_handoffs ORDER BY rowid").fetchall()
            records = [json.loads(row[0]) for row in rows]
            self.assertEqual(["cloud", "cloud", "mac"], [row["worker"] for row in records])
            self.assertTrue(all(row["status"] == "accepted" for row in records))
            handoffs = runtime.ledger.run_snapshot(execution)["worker_handoffs"]
            self.assertEqual(["cloud", "cloud", "local"], [h["location"] for h in handoffs])
            self.assertEqual(3, len({h["attempt_id"] for h in handoffs}))
            self.assertTrue(all("worker_config" not in h for h in handoffs))

            self.assertEqual(records[0]["output_sha"], records[1]["source_sha"])
            self.assertEqual(records[1]["output_sha"], records[2]["source_sha"])
            self.assertEqual(0, records[2]["checks"][0]["exit_code"])
            path = Path(runtime.ledger.workspace_for_execution(execution)["path"])
            self.assertEqual(3, (path / "worker-proof.txt").read_text().count("completed stage"))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            runtime.run([execution], max_ticks=2)
            self.assertEqual(3, runtime.ledger.connection.execute("SELECT COUNT(*) FROM worker_handoffs").fetchone()[0])

    def test_worker_verified_delivery_keeps_transport_receipts_out_of_file_evidence(self):
        values = json.loads(self.path.read_text())
        values["workflows"]["default"]["path"] = str(
            Path(__file__).resolve().parents[1] / "workflows/verified-python.dot")
        values["execution"]["stages"]["Verifying"]["checks"] = [
            [sys.executable, "-B", "-c", "from greeting import greet; assert greet() == 'hello'"]]
        self.path.write_text(json.dumps(values))
        self.fixture.write_text('''#!/usr/bin/env python3
import json, subprocess, sys
from pathlib import Path
if '--version' in sys.argv:
    print('codex-cli 1.0.0')
elif sys.argv[1:3] == ['login', 'status']:
    print('Logged in using ChatGPT')
elif sys.argv[1:3] == ['mcp', 'list']:
    print('[]')
else:
    sys.stdin.read()
    root = Path('.factory')
    if not root.exists():
        root.mkdir()
        (root / 'plan.md').write_text('Return hello and run the pinned verifier.')
        (root / 'verification.json').write_text(json.dumps({
            'schema_version': 1, 'criteria': [{'id': 'greeting',
            'requirement': 'Return hello', 'kind': 'automated',
            'files': ['.factory/verify.py']}]}))
        (root / 'verify.py').write_text("import sys\\nsys.path.insert(0, sys.argv[1])\\nfrom greeting import greet\\nassert greet() == 'hello'\\n")
        evidence = '.factory/plan.md'
    elif not (root / 'delivery.json').exists():
        Path('greeting.py').write_text("def greet():\\n    return 'hello'\\n")
        (root / 'delivery.json').write_text('{"summary":"Return hello","limitations":[]}')
        evidence = '.factory/delivery.json'
    else:
        evidence = '.factory/delivery.json'
    for args in [('config','user.name','Fixture'), ('config','user.email','test@example.invalid'),
                 ('add','.'), ('commit','--allow-empty','-m','verified stage')]:
        subprocess.run(['git',*args],check=True,capture_output=True)
    result = {'dotfactory_result': 1, 'outcome': 'Committed verified stage',
              'preferred_label': 'complete', 'evidence': [{'kind':'file','uri':evidence}]}
    print(json.dumps({'type':'thread.started','thread_id':'verified-fixture'}))
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(result)}}))
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
''')
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution = runtime.start_issue("demo", "VERIFIED-WORKER")
            runtime.run([execution], until_state="Review", max_ticks=20)
            self.assertEqual("Review", runtime.ledger.current(execution)["current_state_id"])
            records = [json.loads(row[0]) for row in runtime.ledger.connection.execute(
                "SELECT manifest_json FROM worker_handoffs ORDER BY rowid")]
            self.assertEqual(["cloud", "cloud", "mac"], [r["worker"] for r in records])
            self.assertTrue(all(r["status"] == "accepted" for r in records))
            self.assertEqual(records[0]["output_sha"], records[1]["source_sha"])
            self.assertEqual(records[1]["output_sha"], records[2]["source_sha"])
            checks = [json.loads(row[0]) for row in runtime.ledger.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='delivery_checked'")]
            self.assertEqual(["plan-result-v2", "implementation-result-v2", "python-verification-v2"],
                             [r["contract"] for r in checks])
            self.assertTrue(all(r["passed"] for r in checks))
            results = [json.loads(row[0]) for row in runtime.ledger.connection.execute(
                "SELECT result_json FROM runner_runs")]
            self.assertTrue(all(e["kind"] != "worker_handoff" for r in results for e in r["evidence"]))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            runtime.run([execution], until_state="Review", max_ticks=2)
            self.assertEqual(3, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM worker_handoffs").fetchone()[0])

    def test_failed_readiness_blocks_before_allocation_and_native_launch(self):
        values = json.loads(self.path.read_text())
        values["execution"]["stages"]["Autoplanning"]["readiness"] = [{
            "name": "python-version", "command": [sys.executable, "-c",
                "import sys; raise SystemExit(sys.version_info < (99, 0))"]}]
        self.path.write_text(json.dumps(values))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-READINESS")
            runtime.run([execution], max_ticks=3)
            self.assertIsNone(runtime.ledger.workspace_for_execution(execution))
            attention = runtime.ledger.run_snapshot(execution)["attention_requests"]
            self.assertIn("readiness:python-version:nonzero-exit", json.dumps(attention))
            self.assertFalse((self.root / "cloud").exists())
            self.assertEqual(0, runtime.ledger.connection.execute("SELECT COUNT(*) FROM runner_runs").fetchone()[0])

    def test_skill_notice_reaches_trace_without_creating_error_fact(self):
        notice = ("Skill descriptions were shortened to fit the skills context budget. "
                  "Codex can still see every skill, but some descriptions are shorter. "
                  "Disable unused skills or plugins to leave more room for the rest.")
        self.fixture.write_text(self.fixture.read_text().replace("    text = sys.stdin.read()",
            "    print(json.dumps(" + repr({"type": "item.completed", "item": {
                "type": "error", "message": notice}}) + "))\n    text = sys.stdin.read()"))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-NOTICE")
            runtime.run([execution], max_ticks=20)
            self.assertEqual("Review", runtime.ledger.current(execution)["current_state_id"])
            self.assertEqual(0, runtime.ledger.connection.execute("SELECT COUNT(*) FROM error_facts").fetchone()[0])
            rows = runtime.ledger.connection.execute("SELECT payload_json FROM runner_events WHERE kind='warning'").fetchall()
            self.assertEqual(3, len(rows))
            self.assertTrue(all(json.loads(row[0])["excerpt"] == notice for row in rows))

    def test_old_worker_cannot_silently_skip_requested_readiness(self):
        values = json.loads(self.path.read_text())
        values["execution"]["stages"]["Autoplanning"]["readiness"] = [{
            "name": "python-version", "command": [sys.executable, "-c", "pass"]}]
        self.path.write_text(json.dumps(values))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-OLD-WORKER")
            with patch("dotfactory.execution.call", return_value={"available": True,
                    "missing": [], "version": "codex-cli 1.0.0"}):
                runtime.run([execution], max_ticks=3)
            self.assertIsNone(runtime.ledger.workspace_for_execution(execution))
            self.assertIn("did not report requested readiness", json.dumps(
                runtime.ledger.run_snapshot(execution)["attention_requests"]))
            self.assertFalse((self.root / "cloud").exists())

    def test_probe_reports_success_without_retaining_output(self):
        report = call(self.policy["workers"]["cloud"], {"op": "probe", "kind": "codex",
            "command": str(self.fixture), "billing": "subscription", "requires": [],
            "readiness": [{"name": "python-version", "command": [sys.executable, "-c",
                "print('private-fixture-output'); import sys; assert sys.version_info >= (3, 9)"]}]})
        self.assertTrue(report["available"])
        self.assertEqual([{"name": "python-version", "passed": True, "exit_code": 0, "reason": "passed"}], report["readiness"])
        self.assertNotIn("private-fixture-output", json.dumps(report))

    def test_readiness_validation_and_bounded_failures(self):
        valid = {"name": "test", "command": [sys.executable, "-c", "pass"]}
        for probes in (None, [valid] * 9, [valid, valid],
                       [dict(valid, name="bad name")], [dict(valid, timeout_seconds=True)],
                       [dict(valid, timeout_seconds=31)], [dict(valid, command="echo")],
                       [dict(valid, name=str(i), timeout_seconds=30) for i in range(3)]):
            with self.subTest(probes=probes), self.assertRaises(ValueError):
                worker.validate_readiness(probes)
        results = worker.readiness([
            {"name": "missing", "command": ["/nonexistent-readiness-tool"]},
            {"name": "timeout", "command": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout_seconds": 1},
        ], dict(os.environ))
        self.assertEqual(["unavailable", "timeout"], [r["reason"] for r in results])

    def test_ineligible_stage_never_runs_or_allocates_workspace(self):
        values = json.loads(self.path.read_text())
        values["execution"]["stages"]["Autoplanning"]["requires"] = ["os:unavailable"]
        self.path.write_text(json.dumps(values))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.run([execution], max_ticks=3)
            self.assertIsNone(runtime.ledger.workspace_for_execution(execution))
            self.assertTrue(runtime.ledger.run_snapshot(execution)["attention_requests"])
            self.assertFalse((self.root / "cloud").exists())

    def test_failed_verification_never_reaches_review(self):
        values = json.loads(self.path.read_text())
        values["execution"]["stages"]["Verifying"]["checks"] = [[sys.executable, "-c", "raise SystemExit(1)"]]
        self.path.write_text(json.dumps(values))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution = runtime.start_issue("demo", "DEMO-1")
            runtime.run([execution], max_ticks=5)
            self.assertNotEqual("Review", runtime.ledger.current(execution)["current_state_id"])
            records = [json.loads(row[0]) for row in runtime.ledger.connection.execute("SELECT manifest_json FROM worker_handoffs")]
            self.assertFalse(any(row["state"] == "Verifying" and row["status"] == "accepted" for row in records))

    def prepared(self, runtime):
        execution = runtime.start_issue("demo", "DEMO-1")
        runtime._claim_pickups()
        from dotfactory.runner import runner_request
        request = runner_request(runtime.kernels["demo"], execution)
        result = runtime.projects["demo"].preparation.prepare(request)
        self.assertEqual("ready", result.disposition, result.error)
        return execution, result.launch

    def test_interrupted_preparation_reuses_exact_allocation(self):
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution, launch = self.prepared(runtime)
            manifest = runtime.execution.record(launch.request)
            original_binding = manifest["binding"]
            manifest.pop("remote_workspace")
            manifest["status"] = "preparing"
            runtime.execution._save(launch.request, manifest)
            runtime.execution.prepare(launch)
            recovered = runtime.execution.record(launch.request)
            self.assertEqual("prepared", recovered["status"])
            self.assertEqual(original_binding, recovered["binding"])
            self.assertEqual(1, len(list((self.root / "cloud").iterdir())))

    def test_workspace_conflict_preserves_both_copies(self):
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution, launch = self.prepared(runtime)
            manifest = runtime.execution.record(launch.request)
            directory = Path(manifest["remote_workspace"]).parent
            (directory / "repo" / "remote.txt").write_text("remote work")
            worker.write_json(directory / "exit.json", {"exit_code": 0})
            (Path(launch.workspace_path) / "local.txt").write_text("local work")
            with self.assertRaisesRegex(ExecutionError, "changed during remote"):
                runtime.execution.accept(launch)
            self.assertEqual("local work", (Path(launch.workspace_path) / "local.txt").read_text())
            self.assertEqual("remote work", (directory / "repo" / "remote.txt").read_text())

    def test_import_replays_after_git_merge_without_duplicate_execution(self):
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution, launch = self.prepared(runtime)
            manifest = runtime.execution.record(launch.request)
            directory = Path(manifest["remote_workspace"]).parent
            (directory / "repo" / "remote.txt").write_text("remote work")
            worker.write_json(directory / "exit.json", {"exit_code": 0})
            first = runtime.execution.accept(launch)
            manifest = runtime.execution.record(launch.request)
            manifest["status"] = "importing"
            runtime.execution._save(launch.request, manifest)
            self.assertEqual(first, runtime.execution.accept(launch))

    def test_stale_attempt_cannot_accept_worker_output(self):
        from dotfactory.ledger import StaleAttempt
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution, launch = self.prepared(runtime)
            with patch.object(runtime.ledger, "assert_attempt_active", side_effect=StaleAttempt("old attempt")):
                with self.assertRaises(StaleAttempt):
                    runtime.execution.accept(launch)
            self.assertEqual("prepared", runtime.execution.record(launch.request)["status"])

    def test_prepared_runner_uses_snapshotted_adapter_after_config_change(self):
        from dataclasses import replace
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution, launch = self.prepared(runtime)
            runner = runtime.scheduler.runner
            runner.router.routes["codex"] = replace(runner.router.routes["codex"], kind="claude-code")
            result = runner.run(launch)
            self.assertEqual("complete", result.preferred_label)
            self.assertEqual("accepted", runtime.execution.record(launch.request)["status"])

    def test_policy_is_snapshotted_before_future_stage_placement(self):
        values = json.loads(self.path.read_text())
        frozen = [{"name": "version", "command": [sys.executable, "-c", "pass"]}]
        values["execution"]["stages"]["Verifying"]["readiness"] = frozen
        self.path.write_text(json.dumps(values))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution, launch = self.prepared(runtime)
            runtime.execution.policy["stages"]["Verifying"]["workers"] = ["cloud"]
            runtime.execution.policy["stages"]["Verifying"]["readiness"] = []
            stored = runtime.execution._policy(launch.request)
            self.assertEqual(["mac"], stored["policy"]["stages"]["Verifying"]["workers"])
            self.assertEqual(frozen, stored["policy"]["stages"]["Verifying"]["readiness"])

    def test_fallback_never_changes_billing_method(self):
        self.policy["workers"]["mac"]["billing"] = "api"
        self.policy["stages"]["Implementing"]["workers"] = ["cloud", "mac"]
        with self.assertRaisesRegex(ValueError, "billing"):
            validate_policy(self.policy)

    def test_worker_allocation_binding_mismatch_is_rejected(self):
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution, launch = self.prepared(runtime)
            manifest = runtime.execution.record(launch.request)
            spec = runtime.execution.spec(manifest, "renew")
            spec["binding"] = "different"
            with self.assertRaisesRegex(ExecutionError, "binding mismatch"):
                call(manifest["worker_config"], spec)


    def test_subscription_does_not_inherit_api_keys(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-api-value", "ANTHROPIC_API_KEY": "private-api-value"}):
            env = worker.environment("subscription", "codex", ["OPENAI_API_KEY"])
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertEqual("private-api-value", worker.environment("api", "codex")["OPENAI_API_KEY"])

    def test_verification_excludes_ambient_and_api_credentials(self):
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution, launch = self.prepared(runtime)
            manifest = runtime.execution.record(launch.request)
            directory = Path(manifest["remote_workspace"]).parent
            worker.write_json(directory / "exit.json", {"exit_code": 0})
            manifest["rule"]["checks"] = [[sys.executable, "-c",
                "import os; assert 'UNDECLARED_PASSWORD' not in os.environ; assert 'OPENAI_API_KEY' not in os.environ"]]
            runtime.execution._save(launch.request, manifest)
            with patch.dict(os.environ, {"UNDECLARED_PASSWORD": "sentinel", "OPENAI_API_KEY": "sentinel"}):
                evidence = runtime.execution.accept(launch)
            self.assertEqual(0, evidence["checks"][0]["exit_code"])

    def test_missing_or_ambiguous_scope_rejected(self):
        self.policy["stages"]["Implementing"].pop("scope")
        with self.assertRaisesRegex(ValueError, "scope"):
            validate_policy(self.policy)

    def test_unknown_capability_fails_closed(self):
        self.policy["stages"]["Implementing"]["requires"] = ["physical-airpods"]
        with self.assertRaisesRegex(ValueError, "requirements"):
            validate_policy(self.policy)

    def test_transport_quotes_remote_paths_and_rejects_options(self):
        config = {"transport": "ssh", "target": "agent:environment:identity@ssh.railway.com",
                  "entrypoint": "/app/worker space/worker.py", "root": "/app/work", "billing": "subscription"}
        self.assertIn("'/app/worker space/worker.py'", transport_command(config)[-1])
        self.policy["workers"]["cloud"] = dict(config, target="-oProxyCommand=bad")
        with self.assertRaises(ValueError):
            validate_policy(self.policy)

    def test_worker_bad_auth_is_not_available(self):
        self.fixture.write_text("#!/bin/sh\ncase \"$1\" in --version) echo '1.0.0';; *) echo 'not logged in'; exit 1;; esac\n")
        report = call(self.policy["workers"]["cloud"], {"op": "probe", "kind": "codex", "command": str(self.fixture), "billing": "subscription", "requires": []})
        self.assertFalse(report["available"])
        self.assertEqual(["auth:chatgpt"], report["missing"])


if __name__ == "__main__":
    unittest.main()
