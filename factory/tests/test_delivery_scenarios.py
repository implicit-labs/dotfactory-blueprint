import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory import (  # noqa: E402
    DurableKernel, RunnerResult, SQLiteLedger, run_prepared_attempt,
)
from dotfactory.linear_api import (  # noqa: E402
    LinearConvergenceWorker, LinearGraphQLClient,
)
from dotfactory.observability import stable_span_id, stable_trace_id  # noqa: E402
from dotfactory.projections import readable_error_groups  # noqa: E402
from dotfactory.resources import (  # noqa: E402
    CapabilityPlan, PreparationEngine, ProviderActivation,
)
from dotfactory.runner import runner_request  # noqa: E402
from dotfactory.workspace import WorkspaceHandle  # noqa: E402


STATUS_TYPE = {
    "Done": "completed", "Canceled": "canceled", "Duplicate": "duplicate",
    "Todo": "unstarted", "Ready": "unstarted",
}


class ScenarioLinearTransport:
    def __init__(self, statuses):
        self.status_by_id = {f"status-{name.lower()}": name for name in statuses}
        self.remote_status = "Todo"
        self.revision = 1

    def issue(self):
        return {
            "id": "issue-demo-1", "identifier": "DEMO-1",
            "updatedAt": f"remote-{self.revision}",
            "state": {
                "id": f"status-{self.remote_status.lower()}",
                "name": self.remote_status,
            },
            "team": {"id": "team-demo"},
            "project": {"id": "project-dotfactory"},
        }

    def __call__(self, _endpoint, _headers, body, _timeout):
        request = json.loads(body)
        operation = request["operationName"]
        if operation == "FactoryLinearPreflight":
            return {"data": {
                "team": {"id": "team-demo", "states": {"nodes": [
                    {
                        "id": status_id, "name": name,
                        "type": STATUS_TYPE.get(name, "started"),
                    }
                    for status_id, name in self.status_by_id.items()
                ]}},
                "project": {"id": "project-dotfactory"},
            }}
        if operation == "FactoryViewer":
            return {"data": {"viewer": {"id": "factory-demo-actor"}}}
        if operation == "FactoryIssue":
            return {"data": {"issue": self.issue()}}
        if operation == "FactoryIssueStatusUpdate":
            self.remote_status = self.status_by_id[request["variables"]["statusId"]]
            self.revision += 1
            return {"data": {"issueUpdate": {
                "success": True, "issue": self.issue(),
            }}}
        raise AssertionError(f"unexpected Linear operation: {operation}")


class ScenarioWorkspaceProvider:
    def __init__(self, root):
        self.root = root

    def materialize(
        self, *, repository_path, root, remote, base_ref, issue_identifier,
        execution_number,
    ):
        path = self.root / f"{issue_identifier}-{execution_number}"
        path.mkdir(parents=True, exist_ok=False)
        return WorkspaceHandle(
            repository_path=str(Path(repository_path).resolve()),
            git_common_dir=str(self.root / "common.git"), remote=remote,
            base_ref=base_ref, base_sha="a" * 40,
            branch_name=f"factory/{issue_identifier.lower()}-{execution_number}",
            path=str(path),
        )

    def reconcile(self, handle):
        if not Path(handle.path).is_dir():
            raise RuntimeError("scenario workspace is missing")
        return handle

    def cleanup(self, handle):
        Path(handle.path).rmdir()


class ScenarioResourceProvider:
    def __init__(self):
        self.activations = []
        self.cleaned = []

    def plan(self, *, capability, config, workspace):
        return CapabilityPlan(
            provider="scenario", capability=capability, scope="attempt",
            resource_id=f"scenario:{capability}:DEMO-1", target=capability,
            config=config,
        )

    def activate(self, plan, *, workspace, owner_token):
        activation = ProviderActivation(
            resource_id=plan.resource_id,
            environment=(("LOCAL_WEB_URL", "https://demo-1.localhost"),),
            urls=("https://demo-1.localhost",), handle=owner_token,
        )
        self.activations.append(activation)
        return activation

    def reconcile(self, allocation, *, workspace, owner_token):
        return next(
            item for item in self.activations
            if item.resource_id == allocation["resource_id"]
        )

    def cleanup(self, activation, *, owner_token):
        if activation.handle != owner_token:
            raise RuntimeError("scenario resource ownership mismatch")
        self.cleaned.append(activation.resource_id)
        return {"cleaned": activation.resource_id}


class ScenarioRunner:
    def __init__(self, ledger, *, fail):
        self.ledger = ledger
        self.fail = fail

    def run(self, launch):
        request = launch.request
        execution_trace_id = stable_trace_id(request.execution_id)
        provider_trace_id = hashlib.sha256(
            f"provider:{request.attempt_id}".encode("utf-8")
        ).hexdigest()[:32]
        root_span_id = stable_span_id("scenario-runner", request.attempt_id)
        command = ["scenario-runner", "DEMO-1"]
        run = self.ledger.plan_runner_run(
            execution_id=request.execution_id, attempt_id=request.attempt_id,
            preparation_id=launch.preparation_id,
            preparation_digest=launch.preparation_digest,
            fence_token=request.fence_token, runner_key="codex",
            adapter_kind="scenario", adapter_version="1.0.0", protocol_version=1,
            execution_trace_id=execution_trace_id, trace_id=provider_trace_id,
            root_span_id=root_span_id, parent_trace_id=execution_trace_id,
            command=command,
            command_digest=hashlib.sha256(json.dumps(command).encode()).hexdigest(),
            prompt_digest=hashlib.sha256(b"DEMO-1").hexdigest(),
            host_id="scenario-host", boot_id="scenario-boot",
        )
        run_id = str(run["id"])
        self.ledger.mark_runner_starting(run_id, fence_token=request.fence_token)
        self.ledger.mark_runner_running(
            run_id, fence_token=request.fence_token, pid=4242,
            process_group_id=4242,
        )
        now = self.ledger.clock()
        tool_span = stable_span_id("scenario-tool", f"{run_id}:tool-1")
        self.ledger.append_runner_event(
            run_id, fence_token=request.fence_token, kind="tool_call",
            protocol_type="scenario.tool_call", stream="stdout",
            payload={"id": "tool-1", "name": "inspect_workspace"},
            span_id=tool_span, parent_span_id=root_span_id,
            source_occurred_at=now, observed_at=now, origin="scenario-runner",
            trust_class="untrusted-provider", session_id="scenario-session",
        )
        self.ledger.append_runner_event(
            run_id, fence_token=request.fence_token, kind="tool_result",
            protocol_type="scenario.tool_result", stream="stdout",
            payload={"id": "tool-1", "result": "ok"},
            span_id=stable_span_id("scenario-result", f"{run_id}:tool-1"),
            parent_span_id=tool_span, source_occurred_at=now, observed_at=now,
            origin="scenario-runner", trust_class="untrusted-provider",
            session_id="scenario-session",
        )
        if self.fail:
            error = {
                "code": "SCENARIO_RUNNER_FAILED", "category": "runner",
                "message": "The scenario runner failed after workspace inspection.",
                "retryable": False,
                "safe_remedy": "Inspect tool-1, correct the task, then retry.",
                "fingerprint": "f" * 64, "fingerprint_version": 1,
            }
            self.ledger.append_runner_event(
                run_id, fence_token=request.fence_token, kind="error",
                protocol_type="scenario.error", stream="stderr", payload=error,
                span_id=stable_span_id("scenario-error", run_id),
                parent_span_id=root_span_id, source_occurred_at=now,
                observed_at=now, origin="scenario-runner",
                trust_class="untrusted-provider", session_id="scenario-session",
            )
            self.ledger.finish_runner_run(
                run_id, fence_token=request.fence_token, status="failed", error=error,
            )
            return RunnerResult(
                "failed", "failed",
                ({"kind": "trace", "uri": "local://DEMO-1/error"},),
            )
        result = {
            "outcome": "succeeded", "preferred_label": "complete",
            "evidence": [{"kind": "plan", "uri": "local://DEMO-1/plan"}],
        }
        self.ledger.record_runner_result(
            run_id, fence_token=request.fence_token, result=result,
            receipt={"exit_code": 0, "events": 2},
            session_id="scenario-session",
        )
        return RunnerResult(
            "succeeded", "complete",
            ({"kind": "plan", "uri": "local://DEMO-1/plan"},),
        )


class DeliveryScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = SQLiteLedger(self.root / "factory.db")
        self.ledger.configure_factory("scenario-factory")
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="project-dotfactory",
        )
        self.kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "default.dot",
            factory_defaults={"runner": "codex", "resources": ["local-web"]},
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def run_scenario(self, *, fail):
        statuses = sorted({
            state["linear_status"] for state in self.kernel.states.values()
            if state.get("linear_status")
        })
        remote = ScenarioLinearTransport(statuses)
        worker = LinearConvergenceWorker(
            self.ledger, self.kernel,
            LinearGraphQLClient("scenario-auth", transport=remote),
        )
        execution = self.kernel.begin(
            "dotfactory", "DEMO-1", {"title": "Scenario"},
            command_id="scenario-begin",
        )
        worker.preflight(
            execution_id=execution, team_id="team-demo",
            project_id="project-dotfactory",
        )
        worker.drain()
        self.kernel.transition(
            execution, "Autoplanning", actor="agent", signal="listener_claim",
            owner="scenario-planner", command_id="scenario-claim",
        )
        worker.drain()
        resource = ScenarioResourceProvider()
        engine = PreparationEngine(
            self.ledger,
            workspace_provider=ScenarioWorkspaceProvider(self.root / "worktrees"),
            providers={"scenario": resource}, owner_token="scenario-owner",
        )
        prepared = engine.prepare(
            runner_request(self.kernel, execution),
            project={"repository_path": str(self.root / "repository")},
            preparation_config={
                "workspace": {
                    "root": str(self.root / "worktrees"), "remote": "origin",
                    "base_ref": "main", "retention": "until_terminal",
                },
                "providers": {"scenario": {"kind": "fixture"}},
                "capabilities": {
                    "local-web": {
                        "provider": "scenario", "scope": "attempt",
                        "mode": "exclusive", "config": {},
                    },
                },
            },
        )
        self.assertEqual("ready", prepared.disposition)
        transition = run_prepared_attempt(
            self.kernel, engine, prepared.launch,
            ScenarioRunner(self.ledger, fail=fail),
            command_id="scenario-complete",
        )
        worker.drain()
        return execution, remote, engine, resource, prepared.launch, transition

    def assert_trace_tree(self, execution):
        trace = self.ledger.trace_page(execution, limit=1000)
        spans = {item["span_id"] for item in trace}
        self.assertTrue(all(
            item["trace_id"] == stable_trace_id(execution) for item in trace
        ))
        self.assertTrue(all(
            item["parent_span_id"] is None or item["parent_span_id"] in spans
            for item in trace
        ))
        roots = {item["span_id"] for item in trace if item["parent_span_id"] is None}
        self.assertEqual({stable_span_id("execution", execution)}, roots)

    def test_happy_path_converges_traces_and_cleans(self):
        execution, remote, engine, resource, launch, transition = self.run_scenario(
            fail=False
        )
        self.assertEqual("Ready", transition["to_state"])
        self.assertEqual("Ready", remote.remote_status)
        self.assertEqual("Ready", self.ledger.current(execution)["observed_linear_status"])
        self.assertEqual(["scenario:local-web:DEMO-1"], resource.cleaned)
        self.assertEqual("ready", engine.cleanup_workspace(execution).disposition)
        self.assertFalse(Path(launch.workspace_path).exists())
        self.assertEqual([], readable_error_groups(self.ledger.error_page(execution)))
        self.assert_trace_tree(execution)

    def test_runner_failure_converges_to_investigating_and_groups_errors(self):
        execution, remote, _engine, resource, launch, transition = self.run_scenario(
            fail=True
        )
        self.assertEqual("Investigating", transition["to_state"])
        self.assertEqual("Investigating", remote.remote_status)
        self.assertEqual(
            "Investigating", self.ledger.current(execution)["observed_linear_status"]
        )
        self.assertEqual(["scenario:local-web:DEMO-1"], resource.cleaned)
        self.assertTrue(Path(launch.workspace_path).exists())
        raw_errors = self.ledger.error_page(execution)
        groups = readable_error_groups(raw_errors)
        self.assertEqual(2, len(raw_errors))
        self.assertEqual(1, len(groups))
        self.assertEqual(2, groups[0]["occurrence_count"])
        self.assertEqual("SCENARIO_RUNNER_FAILED", groups[0]["code"])
        self.assert_trace_tree(execution)


if __name__ == "__main__":
    unittest.main()
