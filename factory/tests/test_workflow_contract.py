import unittest
from collections import defaultdict, deque
from pathlib import Path

from dotfactory.dot import parse_dot
from dotfactory.workflow import compile_dot, load_workflow


ROOT = Path(__file__).resolve().parents[1]


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = load_workflow(
            ROOT / "workflows" / "default.dot"
        ).as_contract()
        cls.states = {state["id"]: state for state in cls.contract["states"]}

    def test_state_taxonomy_is_complete(self):
        expected_work = {
            "Autoplanning",
            "Planning",
            "Implementing",
            "Verifying",
            "Investigating",
            "Reworking",
        }
        actual_work = {sid for sid, state in self.states.items() if state["kind"] == "work"}
        self.assertEqual(expected_work, actual_work)
        self.assertEqual(
            ["Todo", "Ready", "Review"],
            self.contract["scope"]["main_checkpoints"],
        )
        self.assertEqual({"checkpoint", "work"}, {state["kind"] for state in self.states.values()})

    def test_work_and_checkpoint_invariants(self):
        required_attempt_fields = self.contract["semantics"]["work"]["required_attempt_fields"]
        self.assertEqual(
            ["owner", "attempt_id", "started_at", "heartbeat_at"],
            required_attempt_fields,
        )
        for state in self.states.values():
            if state["kind"] == "work":
                self.assertIn("work_role", state)
                self.assertNotIn("checkpoint_role", state)
            else:
                self.assertIn("checkpoint_role", state)
                self.assertNotIn("work_role", state)

    def test_edges_name_actor_and_signal(self):
        allowed_actors = {"human", "agent"}
        allowed_signals = {
            "linear_status_change",
            "listener_claim",
            "agent_handoff",
            "structured_comment",
            "recovery_requested",
            "control_command",
        }
        ids = set()
        for edge in self.contract["transitions"] + self.contract["global_transitions"]:
            self.assertNotIn(edge["id"], ids)
            ids.add(edge["id"])
            self.assertTrue(edge["evocations"])
            for evocation in edge["evocations"]:
                self.assertIn(evocation["actor"], allowed_actors)
                self.assertIn(evocation["signal"], allowed_signals)
            if not edge["from"].startswith("@"):
                self.assertIn(edge["from"], self.states)
            self.assertIn(edge["to"], self.states)

    def test_default_preserves_current_linear_projection(self):
        for state in self.states.values():
            self.assertEqual(state["linear_status"], state["id"])

    def test_human_and_automatic_planning_are_distinct(self):
        edges = {
            (edge["from"], edge["to"]): edge
            for edge in self.contract["transitions"]
        }
        agent_routes = {
            ("Todo", "Autoplanning"),
            ("Autoplanning", "Ready"),
            ("Autoplanning", "Investigating"),
            ("Investigating", "Autoplanning"),
        }
        human_routes = {
            ("Todo", "Planning"),
            ("Planning", "Ready"),
            ("Planning", "Investigating"),
            ("Investigating", "Planning"),
        }
        for route in agent_routes:
            self.assertEqual(
                {"agent"}, {item["actor"] for item in edges[route]["evocations"]}
            )
        for route in human_routes:
            self.assertEqual(
                {"human"}, {item["actor"] for item in edges[route]["evocations"]}
            )

    def test_review_exit_is_human_evoked(self):
        review_edges = [edge for edge in self.contract["transitions"] if edge["from"] == "Review"]
        self.assertEqual({"Done", "Reworking"}, {edge["to"] for edge in review_edges})
        for edge in review_edges:
            self.assertEqual({"human"}, {item["actor"] for item in edge["evocations"]})
        by_target = {edge["to"]: edge for edge in review_edges}
        self.assertTrue(by_target["Done"]["requires_feedback"])
        self.assertEqual("approval", by_target["Done"]["feedback_kind"])
        self.assertEqual("approve", by_target["Done"]["action"])
        self.assertTrue(by_target["Reworking"]["requires_feedback"])
        self.assertEqual(
            "changes_requested", by_target["Reworking"]["feedback_kind"]
        )

    def test_recovery_returns_or_blocks(self):
        edges = self.contract["transitions"]
        work_states = {sid for sid, state in self.states.items() if state["kind"] == "work"}
        recovery_sources = {
            edge["from"]
            for edge in edges
            if edge["to"] == "Investigating" and edge["from"] != "Blocked"
        }
        self.assertEqual(work_states - {"Investigating"}, recovery_sources)
        recovery_targets = {
            edge["to"] for edge in edges if edge["from"] == "Investigating"
        }
        self.assertEqual((work_states - {"Investigating"}) | {"Blocked"}, recovery_targets)
        self.assertIn(("Blocked", "Investigating"), {
            (edge["from"], edge["to"]) for edge in edges
        })

    def test_main_graph_reaches_done(self):
        adjacency = defaultdict(set)
        for edge in self.contract["transitions"]:
            if (
                edge["from"].startswith("@")
                or "Investigating" in {edge["from"], edge["to"]}
                or "Blocked" in {edge["from"], edge["to"]}
            ):
                continue
            adjacency[edge["from"]].add(edge["to"])
        seen = {"Todo"}
        queue = deque(["Todo"])
        while queue:
            source = queue.popleft()
            for target in adjacency[source] - seen:
                seen.add(target)
                queue.append(target)
        self.assertIn("Done", seen)
        self.assertIn("Reworking", seen)

    def test_internal_state_ids_may_differ_from_linear_projection(self):
        simple = compile_dot(parse_dot(
            "digraph X { start [shape=Mdiamond] "
            "build [type=agent, linear_status=\"In Progress\"] "
            "done [shape=Msquare, linear_status=Done] "
            "start -> build build -> done }"
        ))
        states = {state["id"]: state for state in simple.states}
        self.assertEqual("In Progress", states["build"]["linear_status"])
        self.assertNotEqual("build", states["build"]["linear_status"])

    def test_adr_records_every_status_type(self):
        adr = (
            ROOT.parent
            / "docs"
            / "decisions"
            / "0006-separate-checkpoints-work-and-edge-authority.md"
        ).read_text(encoding="utf-8")
        for state in self.states.values():
            row_prefix = "| {} | {} |".format(
                state["linear_status"],
                state["kind"],
            )
            self.assertIn(row_prefix, adr)


if __name__ == "__main__":
    unittest.main()
