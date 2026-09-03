import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dotfactory.dot import DotSyntaxError, parse_dot
from dotfactory.workflow import WorkflowError, compile_dot, load_workflow


ROOT = Path(__file__).resolve().parents[1]


class DotParserTests(unittest.TestCase):
    def test_parses_comments_defaults_nodes_and_edges(self):
        graph = parse_dot(
            """
            // concise graph
            digraph Example {
              node [type="agent", runner="codex"]
              start [shape=Mdiamond]
              build [model="gpt"]
              done [shape=Msquare]
              start -> build
              build -> done [on="complete"]
            }
            """
        )
        self.assertEqual("Example", graph.graph_id)
        self.assertEqual("agent", graph.node_defaults["type"])
        self.assertEqual("gpt", graph.nodes["build"].values["model"])
        self.assertEqual("complete", graph.edges[1].attributes.values["on"])
        workflow = compile_dot(graph)
        build = {item["id"]: item for item in workflow.states}["build"]
        self.assertEqual("agent", build["node_type"])
        self.assertEqual("workflow", build["config_sources"]["runner"])
        self.assertEqual("node", build["config_sources"]["model"])

    def test_reports_source_location(self):
        with self.assertRaisesRegex(DotSyntaxError, r"flow.dot:4:1: expected"):
            parse_dot("digraph X {\n start [shape=Mdiamond]\n start ->\n}", source="flow.dot")

    def test_rejects_chained_edges(self):
        with self.assertRaisesRegex(DotSyntaxError, "chained edges are not supported"):
            parse_dot("digraph X { start -> build -> done }")


class WorkflowCompilationTests(unittest.TestCase):
    def test_three_step_graph_resolves_profile_and_digest(self):
        graph = parse_dot(
            "digraph X { start [shape=Mdiamond] "
            "build [type=agent, profile=builder] done [shape=Msquare] "
            "start -> build build -> done }"
        )
        workflow = compile_dot(
            graph, profile_paths=[ROOT / "workflows" / "profiles.example.json"]
        )
        states = {item["id"]: item for item in workflow.states}
        self.assertEqual("build", workflow.scope["entry_state"])
        self.assertEqual(["done"], workflow.scope["terminal_states"])
        self.assertEqual("gpt-5.6-sol", states["build"]["execution"]["model"])
        self.assertEqual(3, states["build"]["execution"]["max_retries"])
        self.assertEqual("profile:builder", states["build"]["config_sources"]["model"])
        self.assertRegex(workflow.digest, r"^[0-9a-f]{64}$")
        repeated = compile_dot(
            graph, profile_paths=[ROOT / "workflows" / "profiles.example.json"]
        )
        self.assertEqual(workflow.digest, repeated.digest)

    def test_compiler_emits_versioned_state_definitions(self):
        workflow = compile_dot(parse_dot(
            "digraph X { graph [schema_version=2, conventions=linear, "
            "linear_statuses=node_ids] start [shape=Mdiamond] "
            "build [type=agent, runner=codex, max_retries=2] "
            "done [shape=Msquare] start -> build "
            "build -> build [on=retry] build -> done [on=complete] }"
        ))
        build = {item["id"]: item for item in workflow.states}["build"]
        definition = build["state_definition"]
        self.assertEqual(1, workflow.normalized["state_definition_version"])
        self.assertEqual(1, definition["version"])
        self.assertEqual("codex", definition["runner_policy"]["runner"])
        self.assertEqual(2, definition["retry_policy"]["max_retries"])
        self.assertEqual(1, len(definition["retry_policy"]["retry_edge_ids"]))

    def test_compiler_rejects_ambiguous_human_reverse_status_route(self):
        graph = parse_dot(
            "digraph X { graph [schema_version=2, conventions=linear] "
            "start [shape=Mdiamond] current [type=checkpoint, linear_status=Current] "
            "one [type=checkpoint, linear_status=Shared] "
            "two [type=checkpoint, linear_status=Shared] done [shape=Msquare] "
            "start -> current current -> one [on=enter, authority=human] "
            "current -> two [on=enter, authority=human] one -> done two -> done }"
        )
        with self.assertRaisesRegex(WorkflowError, "ambiguous human Linear reverse route"):
            compile_dot(graph)

    def test_node_override_wins_over_profile(self):
        graph = parse_dot(
            "digraph X { start [shape=Mdiamond] "
            "build [type=agent, profile=builder, model=local] done [shape=Msquare] "
            "start -> build build -> done }"
        )
        workflow = compile_dot(
            graph, profile_paths=[ROOT / "workflows" / "profiles.example.json"]
        )
        build = {item["id"]: item for item in workflow.states}["build"]
        self.assertEqual("local", build["execution"]["model"])
        self.assertEqual("node", build["config_sources"]["model"])

    def test_readable_default_preserves_existing_executable_policy(self):
        legacy = load_workflow(ROOT / "workflow.json")
        workflow = load_workflow(ROOT / "workflows" / "default.dot")
        self.assertEqual(
            {item["id"] for item in legacy.states},
            {item["id"] for item in workflow.states},
        )
        legacy_edges = {
            (item["from"], item["to"]): item
            for item in legacy.transitions + legacy.global_transitions
        }
        workflow_edges = {
            (item["from"], item["to"]): item
            for item in workflow.transitions + workflow.global_transitions
        }
        self.assertEqual(set(legacy_edges), set(workflow_edges))
        for route in legacy_edges:
            expected = {
                key: value for key, value in legacy_edges[route].items()
                if key not in {"id", "meaning", "on"}
            }
            actual = {
                key: value for key, value in workflow_edges[route].items()
                if key not in {"id", "meaning", "on"}
            }
            self.assertEqual(expected, actual, route)

    def test_default_source_stays_concise(self):
        source = (ROOT / "workflows" / "default.dot").read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 55)
        self.assertNotIn("id=", source)
        self.assertNotIn("meaning=", source)
        self.assertNotIn("evocations=", source)
        self.assertIn("Investigating -> @resume [on=retry]", source)

    def test_schema_two_linear_conventions_expand_policy(self):
        workflow = compile_dot(parse_dot(
            "digraph X { graph [schema_version=2, conventions=linear, "
            "linear_statuses=node_ids] start [shape=Mdiamond] "
            "build [type=work] recover [type=work] done [shape=Msquare] "
            "start -> build [on=enter] build -> done [on=complete] "
            "build -> recover [on=failed, authority=\"human,agent\"] "
            "recover -> @resume [on=retry] recover -> done [on=complete] }"
        ))
        states = {item["id"]: item for item in workflow.states}
        self.assertEqual("build", states["build"]["linear_status"])
        edges = {(item["from"], item["to"]): item for item in workflow.transitions}
        failed = edges[("build", "recover")]
        self.assertEqual(
            [
                {"actor": "human", "signal": "linear_status_change"},
                {"actor": "agent", "signal": "recovery_requested"},
                {"actor": "human", "signal": "control_command"},
            ],
            failed["evocations"],
        )
        retry = edges[("recover", "build")]
        self.assertEqual("resume_state == build", retry["condition"])
        self.assertEqual("retry", retry["action"])
        self.assertTrue(retry["confirmation"])

    def test_resume_shorthand_requires_linear_convention_and_failure_edge(self):
        without_convention = parse_dot(
            "digraph X { start [shape=Mdiamond] build [type=work] "
            "done [shape=Msquare] start -> build build -> @resume [on=retry] "
            "build -> done }"
        )
        with self.assertRaisesRegex(WorkflowError, "@resume requires graph convention linear"):
            compile_dot(without_convention)
        without_failure = parse_dot(
            "digraph X { graph [schema_version=2, conventions=linear] "
            "start [shape=Mdiamond] build [type=work] done [shape=Msquare] "
            "start -> build build -> @resume [on=retry] build -> done }"
        )
        with self.assertRaisesRegex(WorkflowError, "incoming on=failed edge"):
            compile_dot(without_failure)

    def test_resume_shorthand_rejects_ambiguous_failure_source(self):
        ambiguous = parse_dot(
            "digraph X { graph [schema_version=2, conventions=linear] "
            "start [shape=Mdiamond] build [type=work] recover [type=work] "
            "done [shape=Msquare] start -> build "
            "build -> recover [on=failed] build -> recover [on=failed] "
            "recover -> @resume [on=retry] recover -> done [on=complete] }"
        )
        with self.assertRaisesRegex(WorkflowError, "@resume is ambiguous"):
            compile_dot(ambiguous)

    def test_rejects_unknown_attribute_with_remedy(self):
        graph = parse_dot(
            "digraph X { start [shape=Mdiamond] build [type=agent, modle=x] "
            "done [shape=Msquare] start -> build build -> done }",
            source="bad.dot",
        )
        with self.assertRaisesRegex(WorkflowError, "unknown attribute 'modle'"):
            compile_dot(graph)

    def test_rejects_replaced_authoring_vocabulary(self):
        old_profile = parse_dot(
            "digraph X { start [shape=Mdiamond] build [type=agent, use=builder] "
            "done [shape=Msquare] start -> build build -> done }"
        )
        with self.assertRaisesRegex(WorkflowError, "unknown attribute 'use'"):
            compile_dot(old_profile)
        old_edge = parse_dot(
            "digraph X { start [shape=Mdiamond] done [shape=Msquare] "
            "start -> done [on_label=complete] }"
        )
        with self.assertRaisesRegex(WorkflowError, "unknown attribute 'on_label'"):
            compile_dot(old_edge)

    def test_rejects_unreachable_node(self):
        graph = parse_dot(
            "digraph X { start [shape=Mdiamond] build [type=agent] orphan [type=human] "
            "done [shape=Msquare] start -> build build -> done orphan -> done }"
        )
        with self.assertRaisesRegex(WorkflowError, "unreachable workflow node: orphan"):
            compile_dot(graph)

    def test_rejects_unknown_profile(self):
        graph = parse_dot(
            "digraph X { start [shape=Mdiamond] build [type=agent, profile=missing] "
            "done [shape=Msquare] start -> build build -> done }"
        )
        with self.assertRaisesRegex(WorkflowError, "unknown profile missing"):
            compile_dot(graph)

    def test_rejects_unsupported_schema_version(self):
        graph = parse_dot(
            "digraph X { graph [schema_version=99] start [shape=Mdiamond] "
            "done [shape=Msquare] start -> done }"
        )
        with self.assertRaisesRegex(WorkflowError, "supported: 1, 2"):
            compile_dot(graph)

    def test_rejects_missing_prompt_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-prompt.dot"
            path.write_text(
                "digraph X { start [shape=Mdiamond] "
                "build [type=agent, prompt=missing.md] done [shape=Msquare] "
                "start -> build build -> done }",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(WorkflowError, "prompt does not exist"):
                load_workflow(path)

    def test_prompt_file_content_is_snapshotted_not_its_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work.md").write_text("Plan the requested change.\n", encoding="utf-8")
            path = root / "workflow.dot"
            path.write_text(
                "digraph X { start [shape=Mdiamond] "
                "build [type=agent, prompt=work.md] done [shape=Msquare] "
                "start -> build build -> done }",
                encoding="utf-8",
            )
            workflow = load_workflow(path)
            build = next(item for item in workflow.states if item["id"] == "build")
            self.assertEqual("Plan the requested change.\n", build["execution"]["prompt"])
            self.assertNotIn("work.md", build["execution"]["prompt"])

    def test_generic_renderer_uses_resolved_three_step_graph_and_digest(self):
        workflow = load_workflow(ROOT / "workflows" / "three-step-advanced.dot")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "three-step.md"
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "render_workflow.py"),
                    "--workflow", str(ROOT / "workflows" / "three-step-advanced.dot"),
                    "--output", str(output),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            rendered = output.read_text(encoding="utf-8")
        self.assertIn(workflow.digest, rendered)
        self.assertIn("| build | build | agent | work | — |", rendered)
        self.assertIn("model=gpt-5.6-sol", rendered)
        self.assertIn("[node]", rendered)
        self.assertIn("| review.done | review | done |", rendered)

    def test_starter_mermaid_keeps_unlabeled_edges_visually_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "three-step.md"
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "render_workflow.py"),
                    "--workflow", str(ROOT / "workflows" / "three-step.dot"),
                    "--output", str(output),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            rendered = output.read_text(encoding="utf-8")
        diagram = rendered.split("```mermaid", 1)[1].split("```", 1)[0]
        self.assertIn("START --> N0", diagram)
        self.assertIn("N0 --> N1", diagram)
        self.assertNotIn("start.build", diagram)
        self.assertNotIn("build.review", diagram)

    def test_minimal_human_edges_expand_to_standard_review_policy(self):
        workflow = load_workflow(ROOT / "workflows" / "three-step.dot")
        edges = {(edge["from"], edge["to"]): edge for edge in workflow.transitions}
        approval = edges[("review", "done")]
        self.assertEqual("approve", approval["action"])
        self.assertEqual("approver", approval["required_role"])
        self.assertTrue(approval["requires_feedback"])
        self.assertEqual("approval", approval["feedback_kind"])
        revision = edges[("review", "build")]
        self.assertTrue(revision["requires_feedback"])
        self.assertEqual("changes_requested", revision["feedback_kind"])


if __name__ == "__main__":
    unittest.main()
