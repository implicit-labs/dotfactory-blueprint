import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from dotfactory import (
    DatasetContractError, DurableKernel, HostedDatasetPublisher,
    HostedDatasetSettings, ObservationService, SQLiteLedger, dataset_bundle,
    execution_dataset_case, incident_rows, score_execution_case,
    write_dataset_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeDatasets:
    def __init__(self):
        self.created = 0
        self.cases = []
        self.closed = False

    def get_dataset(self, _name):
        raise type("NotFoundError", (RuntimeError,), {})()

    def create_dataset(self, **_values):
        self.created += 1

    def add_cases(self, _name, *, cases):
        self.cases = list(cases)

    def list_cases(self, _name):
        return [{"name": item["name"]} for item in self.cases]

    def close(self):
        self.closed = True


class ExecutionDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.temp.name) / "factory.db")
        self.ledger.configure_factory("dataset-test")
        self.ledger.register_project(
            "dotfactory", display_name="dotfactory", tracker_kind="linear",
            tracker_project_id="project-dotfactory",
        )
        self.kernel = DurableKernel(
            self.ledger, ROOT / "workflows" / "default.dot"
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def canceled_execution(self, title="dataset"):
        execution = self.kernel.begin(
            "dotfactory", "TASK-571", {"title": title, "source": "fixture"},
            command_id="begin-dataset:" + title,
        )
        self.kernel.transition(
            execution, "Canceled", actor="human", signal="linear_status_change",
            outcome="canceled",
            evidence=[{"kind": "decision", "uri": "https://example.test/fixture"}],
            command_id="cancel-dataset:" + title,
        )
        return execution

    def test_local_bundle_is_byte_identical_and_content_addressed(self):
        execution = self.canceled_execution()
        projection = ObservationService(
            self.ledger, self.kernel
        ).execution_projection(execution)
        case = execution_dataset_case(self.ledger, execution, projection)
        first = dataset_bundle([case])
        second = dataset_bundle([case])
        self.assertEqual(first, second)
        manifest = json.loads(first[1])
        self.assertEqual(1, manifest["case_count"])
        output = write_dataset_bundle(Path(self.temp.name) / "output", [case])
        self.assertTrue(Path(output["dataset"]).is_file())
        self.assertTrue(Path(output["manifest"]).is_file())

    def test_case_uses_allowlists_and_separates_conformance(self):
        execution = self.canceled_execution("safe")
        projection = ObservationService(
            self.ledger, self.kernel
        ).execution_projection(execution)
        case = execution_dataset_case(self.ledger, execution, projection)
        encoded = json.dumps(case, sort_keys=True)
        self.assertNotIn(str(Path(self.temp.name)), encoded)
        self.assertIn("archive", case["metadata"]["completeness"])
        score = score_execution_case(case, projection["waterfall"])
        self.assertTrue(score["passed"])
        self.assertEqual([], incident_rows(case))

    def test_runtime_scorers_name_each_required_failure_mode(self):
        execution = self.canceled_execution("scorers")
        projection = ObservationService(
            self.ledger, self.kernel
        ).execution_projection(execution)
        case = execution_dataset_case(self.ledger, execution, projection)
        waterfall = projection["waterfall"]

        fixtures = {}
        orphan = deepcopy(waterfall)
        next(
            item for item in orphan["items"]
            if item.get("kind") == "span" and item.get("parent_span_id")
        )["parent_span_id"] = None
        fixtures["orphan_spans"] = (case, orphan)
        invalid = deepcopy(waterfall)
        invalid["items"][-1]["parent_span_id"] = "missing"
        fixtures["invalid_parents"] = (case, invalid)
        missing_terminal = deepcopy(waterfall)
        missing_terminal["open_span_count"] = 1
        fixtures["missing_terminal"] = (case, missing_terminal)
        false_success = deepcopy(case)
        false_success["metadata"]["errors"] = [{
            "code": "FAILED", "category": "runner"
        }]
        fixtures["false_success"] = (false_success, waterfall)
        bad_transition = deepcopy(case)
        bad_transition["metadata"]["errors"] = [{
            "code": "INVALID_TRANSITION", "category": "transition"
        }]
        fixtures["bad_transition"] = (bad_transition, waterfall)
        evidence_gap = deepcopy(case)
        evidence_gap["metadata"]["evidence"] = []
        fixtures["evidence_gap"] = (evidence_gap, waterfall)
        capture_loss = deepcopy(waterfall)
        capture_loss["completeness"] = {
            "complete": False, "reasons": ["dropped_events"]
        }
        fixtures["capture_loss"] = (case, capture_loss)
        clock_anomaly = deepcopy(waterfall)
        clock_anomaly["items"][0]["started_at"] = "2026-01-02T00:00:00Z"
        clock_anomaly["items"][0]["ended_at"] = "2026-01-01T00:00:00Z"
        fixtures["clock_anomaly"] = (case, clock_anomaly)

        for expected, (fixture_case, fixture_waterfall) in fixtures.items():
            with self.subTest(expected):
                score = score_execution_case(fixture_case, fixture_waterfall)
                self.assertFalse(score["checks"][expected])
                self.assertFalse(score["passed"])

    def test_private_path_in_authoritative_input_is_rejected(self):
        execution = self.canceled_execution("/private/example/skill")
        projection = ObservationService(
            self.ledger, self.kernel
        ).execution_projection(execution)
        with self.assertRaisesRegex(DatasetContractError, "private path"):
            execution_dataset_case(self.ledger, execution, projection)

    def test_hosted_publication_is_optional_and_read_back(self):
        execution = self.canceled_execution("hosted")
        projection = ObservationService(
            self.ledger, self.kernel
        ).execution_projection(execution)
        case = execution_dataset_case(self.ledger, execution, projection)
        remote = FakeDatasets()
        settings = HostedDatasetSettings("project-api-key", "example/dotfactory")
        result = HostedDatasetPublisher(
            settings,
            client_factory=lambda _key: remote,
            ledger=self.ledger,
        ).publish([case], command_id="publish-hosted")
        self.assertEqual([case["case_id"]], result["case_ids"])
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, remote.created)
        self.assertTrue(remote.closed)
        receipts = self.ledger.connection.execute(
            "SELECT status,detail_json FROM projection_receipts WHERE destination=?",
            (settings.destination,),
        ).fetchall()
        self.assertEqual(["accepted"], [row["status"] for row in receipts])
        self.assertNotIn("project-api-key", receipts[0]["detail_json"])
        replay = HostedDatasetPublisher(
            settings,
            client_factory=lambda _key: self.fail("completed replay performed I/O"),
            ledger=self.ledger,
        ).publish([case], command_id="publish-hosted")
        self.assertEqual(result["projection_attempt_id"], replay[
            "projection_attempt_id"
        ])

    def test_hosted_failure_is_receipted_without_changing_run_state(self):
        execution = self.canceled_execution("hosted-failure")
        projection = ObservationService(
            self.ledger, self.kernel
        ).execution_projection(execution)
        case = execution_dataset_case(self.ledger, execution, projection)

        def unavailable(_key):
            raise RuntimeError("credential value must never be stored")
        settings = HostedDatasetSettings("project-api-key", "example/dotfactory")

        with self.assertRaisesRegex(RuntimeError, "credential value"):
            HostedDatasetPublisher(
                settings,
                client_factory=unavailable, ledger=self.ledger,
            ).publish([case], command_id="publish-hosted-failure")
        receipt = self.ledger.connection.execute(
            "SELECT status,detail_json FROM projection_receipts WHERE destination=?",
            (settings.destination,),
        ).fetchone()
        self.assertEqual("rejected", receipt["status"])
        detail = json.loads(receipt["detail_json"])
        self.assertEqual("project_api_key", detail["credential_kind"])
        self.assertEqual(
            ["project:read_datasets", "project:write_datasets"],
            detail["required_scopes"],
        )
        stored = json.dumps([
            dict(row) for row in self.ledger.connection.execute(
                "SELECT * FROM projection_rejections"
            )
        ])
        self.assertNotIn("credential value", stored)
        self.assertEqual("completed", self.ledger.current(execution)["status"])


if __name__ == "__main__":
    unittest.main()
