"""Project/run profile resolution and immutable admission contracts."""
import json
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import test_execution as fixtures
from dotfactory import FactoryConfig, FactoryRuntime, LifecycleError
from dotfactory.cli import main
from dotfactory.configuration import preview
from dotfactory.doctor import inspect as doctor
from dotfactory.execution import settings_view
from dotfactory.linear_api import LinearConvergenceWorker
from dotfactory.linear_reconciliation import LinearStatusBindingV1
from dotfactory.selection import resolve


class SelectionTests(unittest.TestCase):
    setUp = fixtures.WorkerExecutionTests.setUp
    tearDown = fixtures.WorkerExecutionTests.tearDown

    def configure(self, *, profiles=None, project=None, runners=None, workflows=None):
        values = json.loads(self.path.read_text())
        values["selection_profiles"] = profiles or {}
        values["projects"]["demo"].update(project or {})
        values["runners"].update(runners or {})
        values["workflows"].update(workflows or {})
        self.path.write_text(json.dumps(values))
        return FactoryConfig.load(self.path)

    def test_project_and_run_profiles_replace_lists_and_freeze_provenance(self):
        config = self.configure(
            profiles={
                "project-default": {"model": "project-model", "skills": ["project-skill"]},
                "verify-default": {"model": "verify-model", "skills": ["verify-skill"]},
                "run-default": {"model": "run-model", "reasoning_effort": "high"},
                "empty-skills": {"skills": []},
            },
            project={"profile": "project-default", "stage_profiles": {"Verifying": "verify-default"}},
        )
        override = {"profile": "run-default", "stage_profiles": {"Verifying": "empty-skills"}}
        graph, report, placement = resolve(config, "demo", override)
        self.assertEqual({}, placement)
        planning = next(state for state in graph.states if state["id"] == "Autoplanning")
        verifying = next(state for state in graph.states if state["id"] == "Verifying")
        self.assertEqual("run-model", planning["execution"]["model"])
        self.assertEqual("high", planning["execution"]["reasoning_effort"])
        self.assertEqual(["project-skill"], planning["execution"]["skills"])
        self.assertEqual([], verifying["execution"]["skills"])
        self.assertEqual("run-model", verifying["execution"]["model"])
        self.assertEqual("run:stage:empty-skills", verifying["config_sources"]["skills"])
        self.assertEqual([], verifying["state_definition"]["skills"])
        self.assertIn({"field": "model", "replaced_origin": "project:stage:verify-default",
                       "winner_origin": "run:run-default"},
                      report["stages"]["Verifying"]["conflicts"])
        self.assertEqual(report["digest"], preview(config, "demo", override)["selection"]["digest"])
        cleared, _, _ = resolve(config, "demo", {"profile": None})
        cleared_implementing = next(state for state in cleared.states if state["id"] == "Implementing")
        self.assertEqual("gpt-5.6-sol", cleared_implementing["execution"]["model"])
        self.assertEqual([], cleared_implementing["execution"].get("skills", []))
        stage_cleared, _, _ = resolve(config, "demo", {"stage_profiles": {"Verifying": None}})
        cleared_verifying = next(state for state in stage_cleared.states if state["id"] == "Verifying")
        self.assertEqual("gpt-5.6-sol", cleared_verifying["execution"]["model"])
        with FactoryRuntime(config) as runtime:
            execution = runtime.start_issue("demo", "PROFILE-1", execution_override=override)
            frozen = runtime.ledger.workflow_snapshot(execution)
            self.assertEqual(graph.digest, frozen["digest"])
            self.assertEqual(report, settings_view(runtime.ledger, execution)["selection"])
            self.assertEqual(report, runtime.ledger.run_snapshot(execution)["intent"]["selection"])

    def test_registered_workflow_selection_survives_restart_and_config_change(self):
        original = Path(__file__).resolve().parents[1] / "workflows" / "default.dot"
        alternate = self.root / "alternate.dot"
        (self.root / "prompts").mkdir()
        (self.root / "prompts" / "work.md").write_text(
            (original.parent / "prompts" / "work.md").read_text()
        )
        alternate.write_text(original.read_text().replace("dotfactory-default", "dotfactory-alternate")
                             .replace('prompt="prompts/work.md"', 'prompt="prompts/work.md", timeout="45m"'))
        config = self.configure(workflows={"alternate": {"path": str(alternate), "profile_paths": [],
                                                         "defaults": {"runner": "codex", "resources": []}}})
        override = {"workflow": "alternate"}
        graph, report, _ = resolve(config, "demo", override)
        self.assertEqual("alternate", report["workflow"]["name"])
        with FactoryRuntime(config) as runtime:
            execution = runtime.start_issue("demo", "PROFILE-WORKFLOW", execution_override=override)
            self.assertEqual(graph.digest, runtime.ledger.workflow_snapshot(execution)["digest"])
            self.assertEqual("alternate", runtime.ledger.run_snapshot(execution)["intent"]["selection"]["workflow"]["name"])
        alternate.write_text(alternate.read_text().replace('timeout="45m"', 'timeout="60m"'))
        with FactoryRuntime(FactoryConfig.load(self.path)) as restarted:
            selected, states, _ = restarted.kernels["demo"].graph_for_execution(execution)
            self.assertEqual(graph.digest, selected["workflow_digest"])
            self.assertEqual("45m", states["Implementing"]["execution"]["timeout"])
            self.assertEqual(execution, restarted.start_issue("demo", "PROFILE-WORKFLOW"))
            with self.assertRaisesRegex(LifecycleError, "frozen"):
                restarted.start_issue("demo", "PROFILE-WORKFLOW", execution_override={"workflow": "default"})

    def test_invalid_selection_rejected_before_ledger_write(self):
        config = self.configure(profiles={"known": {"runner": "codex", "resources": []}})
        bad = (
            {"profile": "missing"},
            {"workflow": "missing"},
            {"stage_profiles": {"Review": "known"}},
            {"stage_profiles": {"Verifying": "missing"}},
        )
        with FactoryRuntime(config) as runtime:
            for number, override in enumerate(bad):
                with self.subTest(override=override), self.assertRaises(ValueError):
                    runtime.start_issue("demo", f"INVALID-{number}", execution_override=override)
            count = runtime.ledger.connection.execute("SELECT COUNT(*) FROM workflow_executions").fetchone()[0]
            self.assertEqual(0, count)

    def test_registry_ceiling_and_default_sol_medium(self):
        config = self.configure()
        self.assertEqual("gpt-5.6-sol", config.resolve_runners()["codex"]["default_model"])
        self.assertEqual("medium", config.resolve_runners()["codex"]["default_reasoning_effort"])
        values = json.loads(self.path.read_text())
        values["selection_profiles"] = {"impossible": {"runner": "unregistered"}}
        self.path.write_text(json.dumps(values))
        with self.assertRaisesRegex(ValueError, "unknown runner"):
            FactoryConfig.load(self.path)

    def test_selected_runner_reaches_real_fixture_dispatch(self):
        values = json.loads(self.path.read_text())
        alternate = dict(values["runners"]["codex"])
        config = self.configure(
            profiles={"alternate-runner": {"runner": "codex-alt", "model": "gpt-5.6-sol",
                                          "reasoning_effort": "medium"}},
            runners={"codex-alt": alternate},
        )
        with FactoryRuntime(config) as runtime:
            execution = runtime.start_issue("demo", "PROFILE-DISPATCH",
                                            execution_override={"profile": "alternate-runner"})
            runtime.run([execution], max_ticks=20)
            self.assertEqual("Review", runtime.ledger.current(execution)["current_state_id"])
            runners = {row[0] for row in runtime.ledger.connection.execute(
                "SELECT runner_key FROM runner_runs WHERE execution_id=?", (execution,)
            )}
            self.assertEqual({"codex-alt"}, runners)

    def test_project_profile_isolation_and_incompatible_capability(self):
        values = json.loads(self.path.read_text())
        other = json.loads(json.dumps(values["projects"]["demo"]))
        other["display_name"] = "Other project"
        other["tracker"]["project_id"] = "other-project"
        values["projects"]["other"] = other
        values["selection_profiles"] = {
            "fast": {"model": "project-model"},
            "browser": {"capabilities": ["browser"]},
        }
        values["projects"]["demo"]["profile"] = "fast"
        self.path.write_text(json.dumps(values))
        config = FactoryConfig.load(self.path)
        self.assertEqual("project-model", preview(config, "demo")["selection"]["stages"]["Implementing"]["effective"]["model"])
        self.assertEqual("gpt-5.6-sol", preview(config, "other")["selection"]["stages"]["Implementing"]["effective"]["model"])
        self.assertEqual("runner:codex", preview(config, "other")["selection"]["stages"]["Implementing"]["provenance"]["model"])
        with FactoryRuntime(config) as runtime:
            with self.assertRaisesRegex(ValueError, "capabilities unavailable"):
                runtime.start_issue("demo", "INCOMPATIBLE", execution_override={"profile": "browser"})
            self.assertEqual(0, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM workflow_executions").fetchone()[0])

    def test_selected_graph_reuses_matching_linear_bindings_and_preflights_new_status(self):
        config = self.configure(profiles={"faster": {"reasoning_effort": "low"}})

        class Client:
            calls = 0

            def preflight(self, *, project_key, workflow_digest, team_id, project_id,
                          required_status_names):
                self.calls += 1
                return [LinearStatusBindingV1(
                    project_key=project_key, workflow_digest=workflow_digest,
                    team_id=team_id, status_id="s-" + name,
                    status_name=name, status_type="unstarted",
                ) for name in required_status_names]

        with FactoryRuntime(config) as runtime:
            kernel = runtime.kernels["demo"]
            states = kernel.states
            base = kernel.definition.digest
            runtime.ledger.bind_linear_statuses("demo", base, "demo-team", [
                LinearStatusBindingV1(
                    project_key="demo", workflow_digest=base, team_id="demo-team",
                    status_id="s-" + name, status_name=name, status_type="unstarted",
                ) for name in states
            ])
            client = Client()
            worker = LinearConvergenceWorker(runtime.ledger, kernel, client, self_actor_id="factory")
            selected, _, _ = resolve(config, "demo", {"profile": "faster"})
            copied = worker.ensure_selected_bindings(
                project_key="demo", workflow_digest=selected.digest,
                states={state["id"]: state for state in selected.states},
                team_id="demo-team", project_id="demo-project",
            )
            self.assertEqual(0, client.calls)
            self.assertTrue(copied)
            self.assertEqual(selected.digest, copied[0]["workflow_digest"])
            altered = {**states, "New": {"linear_status": "New"}}
            worker.ensure_selected_bindings(
                project_key="demo", workflow_digest="a" * 64,
                states=altered, team_id="demo-team", project_id="demo-project",
            )
            self.assertEqual(1, client.calls)

    def test_preview_and_doctor_report_the_same_selected_profile_without_writes(self):
        self.configure(profiles={"sol-medium": {"runner": "codex", "model": "gpt-5.6-sol",
                                                  "reasoning_effort": "medium"}})
        override = {"profile": "sol-medium"}
        override_path = self.root / "override.json"
        override_path.write_text(json.dumps(override))
        before = set(self.root.rglob("*"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(["config-preview", "--config", str(self.path),
                                      "--project", "demo", "--execution-config", str(override_path)]))
        command = json.loads(output.getvalue())
        self.assertEqual(command, doctor(str(self.path), project="demo",
                                         execution_override=override)["execution_settings"]["demo"])
        self.assertEqual("gpt-5.6-sol", command["selection"]["stages"]["Verifying"]["effective"]["model"])
        self.assertEqual(before, set(self.root.rglob("*")))

    def test_selected_missing_skill_blocks_before_model_dispatch(self):
        values = json.loads(self.path.read_text())
        empty_skills = self.root / "empty-skills"
        empty_skills.mkdir()
        values["runners"]["codex"]["skill_directory"] = str(empty_skills)
        values["selection_profiles"] = {"missing-tool": {"skills": ["not-installed-here"]}}
        self.path.write_text(json.dumps(values))
        with FactoryRuntime(FactoryConfig.load(self.path)) as runtime:
            execution = runtime.start_issue(
                "demo", "MISSING-TOOL",
                execution_override={"stage_profiles": {"Autoplanning": "missing-tool"}},
            )
            runtime.run([execution], until_state="Investigating", max_ticks=12)
            self.assertEqual("Investigating", runtime.ledger.current(execution)["current_state_id"])
            self.assertEqual(0, runtime.ledger.connection.execute(
                "SELECT COUNT(*) FROM runner_runs WHERE execution_id=?", (execution,)
            ).fetchone()[0])
            self.assertIn("not-installed-here", json.dumps(runtime.ledger.run_snapshot(execution)))


if __name__ == "__main__":
    unittest.main()
