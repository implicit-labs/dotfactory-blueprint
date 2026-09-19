"""Exercise readiness through project/run settings, using real subprocesses."""
import copy
import io
import json
import signal
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import test_execution as fixtures
from dotfactory import FactoryConfig, FactoryRuntime, ObservationService
from dotfactory.cli import main
from dotfactory.execution import settings_view
from dotfactory.linear_agent import build_agent_projection


class CustomExecutionConfigTests(unittest.TestCase):
    setUp = fixtures.WorkerExecutionTests.setUp
    tearDown = fixtures.WorkerExecutionTests.tearDown

    def probe(self, name, exit_code=0):
        return {"name": name, "command": [sys.executable, "-c",
            "import sys; raise SystemExit(" + str(exit_code) + ")"], "timeout_seconds": 2}

    def save(self, values):
        self.path.write_text(json.dumps(values))

    def handoffs(self, runtime, execution):
        return [json.loads(row[0]) for row in runtime.ledger.connection.execute(
            "SELECT manifest_json FROM worker_handoffs WHERE execution_id=? ORDER BY rowid", (execution,))]

    def run_cli(self, issue, override_path=None):
        args = ["run", "--config", str(self.path), "--project", "demo", "--issue", issue,
                "--until-state", "Review", "--max-ticks", "20"]
        if override_path:
            args.extend(["--execution-config", str(override_path)])
        out, err = io.StringIO(), io.StringIO()
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(args)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
        return code, out.getvalue(), err.getvalue()

    def test_real_cli_clears_project_probe_and_runs_custom_worker_checks(self):
        values = json.loads(self.path.read_text())
        preview = copy.deepcopy(values["execution"]["workers"]["mac"])
        preview.update(root=str(self.root / "preview"), location="local")
        values["execution"]["workers"]["preview"] = preview
        check = [sys.executable, "-c",
            "from pathlib import Path; import sys; "
            "assert Path.cwd().parents[1] == Path(sys.argv[1]).resolve(); "
            "assert Path('worker-proof.txt').read_text().count('completed stage') == 3",
            preview["root"]]
        values["projects"]["demo"]["execution"] = {"stages": {
            "Autoplanning": {"readiness": [self.probe("project-unavailable", 17)]},
            "Verifying": {"checks": [check]},
        }}
        self.save(values)
        override = {"stages": {
            "Autoplanning": {"readiness": []},
            "Verifying": {"workers": ["preview"], "readiness": [self.probe("run-preview")]},
        }}
        path = self.root / "custom-run.json"
        path.write_text(json.dumps(override))
        code, out, err = self.run_cli("CUSTOM-CLI", path)
        self.assertEqual(0, code, out + err)
        with FactoryRuntime(FactoryConfig.load(self.path), control_only=True) as runtime:
            ex = runtime.ledger.list_runs()[0]["id"]
            self.assertEqual("Review", runtime.ledger.current(ex)["current_state_id"])
            records = self.handoffs(runtime, ex)
            self.assertEqual(["cloud", "cloud", "preview"], [h["worker"] for h in records])
            self.assertEqual([], records[0]["report"]["readiness"])
            self.assertEqual(["run-preview"], [p["name"] for p in records[2]["report"]["readiness"]])
            self.assertTrue(records[2]["report"]["readiness"][0]["passed"])
            self.assertEqual(0, records[2]["checks"][0]["exit_code"])
            self.assertEqual(check, records[2]["checks"][0]["argv"])
            view = settings_view(runtime.ledger, ex)
            self.assertEqual("project", view["provenance"]["Verifying"]["checks"])
            self.assertEqual("run", view["provenance"]["Verifying"]["workers"])
            before = {table: runtime.ledger.connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                      for table in ("attempts", "runner_runs", "worker_handoffs", "execution_policies")}
        # Changing the input file cannot mutate an existing run or allocate more work.
        path.write_text(json.dumps({"stages": {"Verifying": {"readiness": []}}}))
        code, _out, err = self.run_cli("CUSTOM-CLI", path)
        self.assertEqual(1, code)
        self.assertIn("frozen", err)
        # Omitted input on resume must retain, not clear, the recorded override.
        code, out, err = self.run_cli("CUSTOM-CLI")
        self.assertEqual(0, code, out + err)
        with FactoryRuntime(FactoryConfig.load(self.path), control_only=True) as runtime:
            self.assertEqual(view, settings_view(runtime.ledger, ex))
            after = {table: runtime.ledger.connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                     for table in before}
            self.assertEqual(before, after)
        # Another run without the override still receives the project's failing probe.
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            other = runtime.start_issue("demo", "CUSTOM-DEFAULT")
            runtime.run([other], max_ticks=3)
            self.assertIsNone(runtime.ledger.workspace_for_execution(other))
            self.assertIn("project-unavailable", json.dumps(runtime.ledger.run_snapshot(other)["attention_requests"]))

    def test_project_probes_and_stricter_run_are_isolated_at_verification(self):
        values = json.loads(self.path.read_text())
        values["projects"]["web"] = copy.deepcopy(values["projects"]["demo"])
        values["projects"]["web"]["tracker"]["project_id"] = "web-project"
        for project, worker in (("demo", "mac"), ("web", "cloud")):
            values["projects"][project]["execution"] = {"stages": {
                state: {"workers": [worker], "readiness": [self.probe(project + "-" + state.lower())]}
                for state in ("Autoplanning", "Implementing", "Verifying")}}
            values["execution"]["workers"][worker]["location"] = "local"
        self.save(values)
        # Exercise the harmless-notice path in the same worker runs.
        notice = ("Skill descriptions were shortened to fit the skills context budget. "
                  "Codex can still see every skill, but some descriptions are shorter. "
                  "Disable unused skills or plugins to leave more room for the rest.")
        frame = {"type": "item.completed", "item": {"type": "error", "message": notice}}
        self.fixture.write_text(self.fixture.read_text().replace("    text = sys.stdin.read()",
            "    print(json.dumps(" + repr(frame) + "))\n    text = sys.stdin.read()"))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            normal = runtime.start_issue("demo", "CONFIG-NORMAL")
            web = runtime.start_issue("web", "CONFIG-WEB")
            strict = runtime.start_issue("demo", "CONFIG-STRICT", execution_override={"stages": {
                "Verifying": {"readiness": [self.probe("demo-verifying"), self.probe("extra-fixture", 23)]}}})
            runtime.run([normal, web, strict], max_ticks=40)
            for ex, project, selected_worker in ((normal, "demo", "mac"), (web, "web", "cloud")):
                self.assertEqual("Review", runtime.ledger.current(ex)["current_state_id"])
                records = self.handoffs(runtime, ex)
                self.assertEqual(3, len(records))
                for record in records:
                    self.assertEqual(selected_worker, record["worker"])
                    self.assertEqual("accepted", record["status"])
                    self.assertEqual([project + "-" + record["state"].lower()],
                                     [probe["name"] for probe in record["report"]["readiness"]])
                snapshot = runtime.ledger.run_snapshot(ex)
                projection = ObservationService(runtime.ledger, runtime.kernels[project]).execution_projection(ex)
                _urls, activities = build_agent_projection(snapshot, projection, runtime.ledger.run_history(ex),
                                                           marker_url="https://runs.example/" + project)
                self.assertEqual("elicitation", activities[-1]["content"]["type"])
                self.assertEqual(3, sum(a["content"].get("body", "").startswith("💻 Local") for a in activities))
                self.assertEqual([], projection["error_groups"])
            self.assertEqual("Verifying", runtime.ledger.current(strict)["current_state_id"])
            self.assertIn("extra-fixture", json.dumps(runtime.ledger.run_snapshot(strict)["attention_requests"]))
            self.assertEqual(["Autoplanning", "Implementing"], [h["state"] for h in self.handoffs(runtime, strict)])
            self.assertEqual(2, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM runner_runs WHERE execution_id=?", (strict,)).fetchone()[0])
            self.assertEqual(8, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM runner_events WHERE kind='warning'").fetchone()[0])

    def test_custom_project_verification_command_failure_prevents_review(self):
        values = json.loads(self.path.read_text())
        failing = [sys.executable, "-c", "raise SystemExit(29)"]
        values["projects"]["demo"]["execution"] = {"stages": {
            "Verifying": {"checks": [failing], "check_timeout_seconds": 7}}}
        self.save(values)
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            ex = runtime.start_issue("demo", "CUSTOM-CHECK-FAILURE")
            # Stop immediately after verification fails; do not spend recovery attempts.
            for _ in range(6):
                runtime.step()
                rows = self.handoffs(runtime, ex)
                if any(h["state"] == "Verifying" for h in rows):
                    break
            self.assertNotEqual("Review", runtime.ledger.current(ex)["current_state_id"])
            self.assertNotIn("Review", [s["state_id"] for s in runtime.ledger.run_history(ex)["state_runs"]])
            record = next(h for h in self.handoffs(runtime, ex) if h["state"] == "Verifying")
            self.assertNotEqual("accepted", record["status"])
            self.assertEqual([failing], record["rule"]["checks"])
            self.assertEqual(7, record["rule"]["check_timeout_seconds"])
            self.assertEqual("Investigating", runtime.ledger.current(ex)["current_state_id"])
            projection = ObservationService(runtime.ledger, runtime.kernels["demo"]).execution_projection(ex)
            self.assertIn("required verification command failed", json.dumps(projection["error_groups"]))


if __name__ == "__main__":
    unittest.main()
