"""Offline send-boundary proofs. Fake providers cannot prove live delivery."""
import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import test_telemetry_delivery as telemetry_fixture
import test_verified_delivery as runtime_fixture
from test_datasets import FakeDatasets
from test_linear_agent import FakeAgentAPI
from test_linear_evidence import FakeComments
from dotfactory import FactoryRuntime, LifecycleError, ObservationService
from dotfactory.datasets import (DatasetContractError, HostedDatasetPublisher,
                                HostedDatasetSettings, execution_dataset_case)
from dotfactory.linear_agent import LinearAgentSessionWorker
from dotfactory.linear_api import LinearConvergenceWorker
from dotfactory.linear_evidence import LinearEvidenceWorker
from dotfactory.projection_health import projection_health
from dotfactory.projection_policy import (authorize, configure, linear_body,
                                        record_admission, resolve, settings_view)
from dotfactory.telemetry import TelemetryProjectionError


def values():
    return {"projects": {"dotfactory": {"tracker": {"project_id": "project-test"}}},
            "projections": {"linear": {"enabled": True},
                            "logfire": {"enabled": True, "dataset_enabled": True, "project": "example/dotfactory",
                                        "dataset_api_key_env": "DATASET_KEY"}}}


class ProjectionPolicyTests(unittest.TestCase):
    setUp = telemetry_fixture.DurableTelemetryTests.setUp
    tearDown = telemetry_fixture.DurableTelemetryTests.tearDown
    worker = telemetry_fixture.DurableTelemetryTests.worker
    append = telemetry_fixture.DurableTelemetryTests.append

    def policy(self, override=None):
        self.values = values()
        # Existing run deliberately represents migration; fresh runs get admission-v1.
        configure(self.ledger, self.values, {"DATASET_KEY": "never-copy-this"}, ["dotfactory"])
        if override is not None:
            self.execution = self.kernel.begin("dotfactory", "NEW", {}, command_id="new",
                projection_settings=resolve(self.values, "dotfactory", override, environment={}))
        return self.execution

    def advance(self, seconds):
        now = datetime.fromisoformat(self.ledger.clock().replace("Z", "+00:00"))
        self.ledger.clock = lambda: (now + timedelta(seconds=seconds)).isoformat()

    def test_layers_constraints_and_credential_isolation(self):
        config = values()
        config["projection_policy"] = {"defaults": {"destinations": ["linear"]},
            "constraints": {"required_destinations": ["logfire"], "redaction": "metadata",
                            "max_delivery_age_seconds": 60, "redact_fields": ["intent"]}}
        config["projects"]["dotfactory"]["projection_policy"] = {"destinations": ["dataset"]}
        result = resolve(config, "dotfactory", {"destinations": [], "redaction": "standard",
            "max_delivery_age_seconds": None, "redact_fields": []}, environment={"DATASET_KEY": "secret-value"})
        self.assertEqual({"destinations": ["logfire"], "redaction": "metadata",
            "max_delivery_age_seconds": 60, "redact_fields": ["intent"]}, result["effective"])
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertTrue(result["conflicts"])
        self.policy()
        self.assertNotIn("never-copy-this", json.dumps(self.ledger.projection_policy_environment))

    def test_invalid_or_unsupported_policy_and_native_planning(self):
        for bad in ({"remote_retention_seconds": 1}, {"destinations": ["https://other.invalid"]},
                    {"max_delivery_age_seconds": True}, {"redaction": "none"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve(values(), "dotfactory", bad)
        config = values()
        config["projections"]["linear"]["enabled"] = False
        with self.assertRaisesRegex(ValueError, "instance-disabled"):
            resolve(config, "dotfactory", {"destinations": ["linear"]})
        config = values()
        config["projects"]["dotfactory"]["linear_planning"] = {"enabled": True}
        with self.assertRaisesRegex(ValueError, "native Linear planning"):
            resolve(config, "dotfactory", {"destinations": []})

    def test_project_binding_and_current_constraints_cannot_enable_opted_out_run(self):
        run = self.policy({"destinations": []})
        current = self.ledger.projection_policy_values
        current["projection_policy"] = {"constraints": {"required_destinations": ["linear"]}}
        self.assertEqual("opted_out", authorize(self.ledger, run, "linear")["reason"])
        # Migration captured existing registrations without applying newly selected defaults.
        legacy = self.ledger.connection.execute("SELECT id FROM workflow_executions WHERE id<>?", (run,)).fetchone()[0]
        self.assertEqual("legacy-v0", settings_view(self.ledger, legacy)["migration"])
        current["projects"]["dotfactory"]["tracker"]["project_id"] = "wrong-project"
        self.assertEqual("destination_changed", authorize(self.ledger, legacy, "linear")["reason"])

    def test_mixed_runs_skip_without_success_receipt_or_stalling(self):
        enabled = self.execution
        disabled = self.policy({"destinations": []})
        calls = []
        result = self.worker(lambda _e, _h, body, _t: calls.append(body) or {}).drain()
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, result["policy_skipped_count"])
        self.assertEqual(1, result["accepted_count"])
        outgoing = [telemetry_fixture.attributes(s)["dotfactory.execution.id"]
                    for body in calls for s in telemetry_fixture.spans(body)]
        self.assertIn(enabled, outgoing)
        self.assertNotIn(disabled, outgoing)
        self.assertIsNone(self.worker(lambda *_: self.fail("watermark did not advance")).drain())
        self.ledger.projection_configuration = {"logfire": True, "linear": True, "logfire_destination": self.settings.destination}
        health = next(c for c in projection_health(self.ledger, disabled)["channels"] if c["projection_type"] == "trace")
        self.assertEqual(0, health["confirmed"])
        self.assertEqual(1, health["policy_skipped"])
        self.assertEqual("opted_out", health["policy"]["reason"])

    def test_queued_retry_blocks_destination_change_then_reuses_exact_bytes(self):
        self.policy()
        calls = []
        def unknown(_e, _h, body, _t):
            calls.append(body)
            raise TelemetryProjectionError("UNKNOWN", "unknown", retryable=True, ambiguous=True)
        self.worker(unknown).publish(command_id="retry")
        current = self.ledger.projection_policy_values["projections"]["logfire"]
        current["headers_env"] = "OTHER_CREDENTIAL"
        worker = self.worker(lambda _e, _h, body, _t: calls.append(body) or {})
        blocked = worker.publish(command_id="retry")
        self.assertEqual("policy_blocked", blocked["delivery"]["status"])
        self.assertEqual(1, len(calls))
        del current["headers_env"]
        self.assertEqual("completed", worker.publish(command_id="retry")["status"])
        self.assertEqual(calls[0], calls[1])

    def test_expired_queued_payload_is_not_rewritten_or_sent(self):
        self.policy({"max_delivery_age_seconds": 1})
        def unknown(*_):
            raise TelemetryProjectionError("UNKNOWN", "unknown", retryable=True, ambiguous=True)
        self.worker(unknown).publish(command_id="expires")
        self.advance(2)
        result = self.worker(lambda *_: self.fail("expired payload sent")).publish(command_id="expires")
        self.assertEqual("delivery_expired", result["delivery"]["outcome"]["reason"])
        self.assertEqual("source_time_invalid", authorize(self.ledger, self.execution, "logfire", source_at="bad")["reason"])

    def test_redaction_tightening_blocks_saved_payload(self):
        self.policy()
        def unknown(*_):
            raise TelemetryProjectionError("UNKNOWN", "unknown", retryable=True, ambiguous=True)
        self.worker(unknown).publish(command_id="redact")
        self.ledger.projection_policy_values["projection_policy"] = {"constraints": {"redaction": "metadata"}}
        result = self.worker(lambda *_: self.fail("old unredacted payload sent")).publish(command_id="redact")
        self.assertEqual("queued_policy_changed", result["delivery"]["outcome"]["reason"])

    def test_metadata_spans_keep_trace_shape_without_source_fields(self):
        run = self.policy({"redaction": "metadata"})
        calls = []
        self.worker(lambda _e, _h, body, _t: calls.append(body) or {}).drain()
        own = [s for body in calls for s in telemetry_fixture.spans(body)
               if telemetry_fixture.attributes(s)["dotfactory.execution.id"] == run]
        self.assertTrue(own)
        for span in own:
            self.assertEqual("dotfactory.observation", span["name"])
            self.assertNotIn("NEW", json.dumps(span))
            self.assertIn("traceId", span)

    def test_linear_status_comment_and_agent_send_boundaries(self):
        run = self.policy({"destinations": []})
        client = FakeComments()
        item = self.ledger.stage_linear_evidence(run, issue_id="issue", body="private", digest=hashlib.sha256(b"private").hexdigest())
        self.assertEqual("policy_blocked", LinearEvidenceWorker(self.ledger, client).drain_one(item))
        self.assertEqual(0, client.creates)
        worker = LinearConvergenceWorker(self.ledger, self.kernel, client)
        self.assertEqual("policy_blocked", worker.drain_one({"id": "mutation", "execution_id": run})["status"])
        agent = FakeAgentAPI()
        result = LinearAgentSessionWorker(self.ledger, agent).sync(run, issue_id="issue",
            marker_url="https://example.invalid/run", external_urls=[], activities=[])
        self.assertEqual("policy_blocked", result)
        self.assertEqual(0, agent.creates)

    def test_queued_linear_prose_blocks_after_redaction_tightens(self):
        run = self.policy()
        client = FakeComments()
        item = self.ledger.stage_linear_evidence(run, issue_id="issue", body="private", digest=hashlib.sha256(b"private").hexdigest())
        self.ledger.projection_policy_values["projection_policy"] = {"constraints": {"redact_fields": ["intent"]}}
        self.assertNotIn("private", linear_body(self.ledger, run, "private"))
        self.assertEqual("policy_blocked", LinearEvidenceWorker(self.ledger, client).drain_one(item))
        self.assertEqual(0, client.creates)

    def test_dataset_destination_content_and_optout_gate_before_client(self):
        run = self.policy()
        projection = ObservationService(self.ledger, self.kernel).execution_projection(run)
        case = execution_dataset_case(self.ledger, run, projection)
        remote = FakeDatasets()
        publisher = HostedDatasetPublisher(HostedDatasetSettings("key", project="example/dotfactory"), client_factory=lambda _: remote, ledger=self.ledger)
        changed = copy.deepcopy(case)
        changed["inputs"]["intent"]["title"] = "changed"
        with self.assertRaisesRegex(DatasetContractError, "content"):
            publisher.publish([changed], command_id="changed")
        self.assertEqual(0, remote.created)
        self.ledger.projection_policy_values["projections"]["logfire"]["dataset_name"] = "other"
        with self.assertRaisesRegex(DatasetContractError, "destination_changed"):
            publisher.publish([case], command_id="wrong-destination")
        del self.ledger.projection_policy_values["projections"]["logfire"]["dataset_name"]
        self.assertEqual("completed", publisher.publish([case], command_id="allowed")["status"])
        self.assertEqual(1, remote.created)

    def test_dataset_optout_and_metadata_mode(self):
        run = self.policy({"destinations": [], "redaction": "metadata"})
        case = execution_dataset_case(self.ledger, run,
            ObservationService(self.ledger, self.kernel).execution_projection(run))
        self.assertEqual({"intent": {"withheld": True}}, case["inputs"])
        publisher = HostedDatasetPublisher(HostedDatasetSettings("key", project="example/dotfactory"),
            client_factory=lambda _: self.fail("opted out dataset created client"), ledger=self.ledger)
        with self.assertRaisesRegex(DatasetContractError, "opted_out"):
            publisher.publish([case], command_id="optout")

    def test_reopened_managed_dataset_ledger_cannot_bypass_owner_configuration(self):
        run = self.policy()
        case = execution_dataset_case(self.ledger, run,
            ObservationService(self.ledger, self.kernel).execution_projection(run))
        from dotfactory import SQLiteLedger
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        publisher = HostedDatasetPublisher(HostedDatasetSettings("key", project="example/dotfactory"),
            client_factory=lambda _: self.fail("managed ledger sent without owner configuration"), ledger=self.ledger)
        with self.assertRaisesRegex(DatasetContractError, "registration_unavailable"):
            publisher.publish([case], command_id="reopened")

    def test_queued_agent_session_and_activity_are_gated(self):
        run = self.policy()
        api = FakeAgentAPI()
        worker = LinearAgentSessionWorker(self.ledger, api)
        item = worker._stage_session(run, issue_id="issue", marker_url="https://example.invalid/run",
                                    external_urls=[{"url": "https://example.invalid/run", "label": "Run"}])
        self.ledger.projection_policy_values["projections"]["linear"]["enabled"] = False
        self.assertEqual("policy_blocked", worker._drain_session(item))
        self.assertEqual("policy_blocked", worker._drain_activity({"execution_id": run}, "session"))
        self.assertEqual(0, api.creates + api.activity_creates)

    def test_metadata_comment_can_be_delivered_without_prose(self):
        run = self.policy({"redaction": "metadata"})
        client = FakeComments()
        body = linear_body(self.ledger, run, "private intent")
        item = self.ledger.stage_linear_evidence(run, issue_id="issue", body=body,
            digest=hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual("confirmed", LinearEvidenceWorker(self.ledger, client).drain_one(item))
        self.assertEqual(1, client.creates)
        self.assertNotIn("private intent", json.dumps(client.comments))

    def test_actual_transport_mismatch_and_missing_policy_fail_closed(self):
        run = self.policy()
        self.assertEqual("transport_destination_mismatch", authorize(self.ledger, run, "linear", endpoint="https://other.invalid")["reason"])
        self.assertEqual("transport_destination_mismatch", authorize(self.ledger, run, "logfire", project="other/project")["reason"])
        self.assertEqual("transport_destination_mismatch", authorize(self.ledger, run, "dataset", region="eu")["reason"])
        self.ledger.connection.execute("DELETE FROM projection_policies WHERE execution_id=?", (run,))
        self.assertEqual("policy_missing", authorize(self.ledger, run, "linear")["reason"])

    def test_reopened_managed_ledger_with_missing_snapshot_never_becomes_unmanaged(self):
        run = self.policy()
        self.ledger.connection.execute("DELETE FROM projection_policies WHERE execution_id=?", (run,))
        from dotfactory import SQLiteLedger
        self.ledger.close()
        self.ledger = SQLiteLedger(self.path)
        self.assertEqual("policy_missing", authorize(self.ledger, run, "linear")["reason"])
        result = self.worker(lambda *_: self.fail("missing managed snapshot allowed telemetry")).drain()
        self.assertEqual("policy_missing", result["delivery"]["outcome"]["reason"])
        configure(self.ledger, self.values, {}, ["dotfactory"])
        self.assertEqual("policy_missing", authorize(self.ledger, run, "linear")["reason"])

    def test_public_transport_project_mismatch_blocks_before_send(self):
        from dataclasses import replace
        from dotfactory.telemetry import LogfireProjectionWorker
        self.policy()
        worker = LogfireProjectionWorker(self.ledger, replace(self.settings, project="other/project"),
            transport=lambda *_: self.fail("wrong-project transport sent data"))
        result = worker.drain()
        self.assertEqual("transport_destination_mismatch", result["delivery"]["outcome"]["reason"])

    def test_public_policy_skip_health_is_destination_scoped(self):
        self.policy({"destinations": []})
        self.worker(lambda *_: {}).drain()
        self.ledger.projection_configuration = {"logfire": True, "logfire_destination": "logfire:other/project:us:otel-v2"}
        health = next(c for c in projection_health(self.ledger, self.execution)["channels"] if c["projection_type"] == "trace")
        self.assertEqual(0, health["policy_skipped"])

    def test_all_opted_out_records_advance_without_false_success(self):
        config = values()
        with self.ledger.transaction() as db:
            record_admission(db, self.execution, "dotfactory", resolve(config, "dotfactory", {"destinations": []}))
        configure(self.ledger, config, {}, ["dotfactory"])
        result = self.worker(lambda *_: self.fail("optout sent data")).drain()
        self.assertEqual("completed", result["status"])
        self.assertEqual(0, result["accepted_count"])
        self.assertEqual(1, result["policy_skipped_count"])

    def test_legacy_queued_payload_requires_redaction_check_after_migration(self):
        def unknown(*_):
            raise TelemetryProjectionError("UNKNOWN", "unknown", retryable=True, ambiguous=True)
        self.worker(unknown).publish(command_id="legacy")
        # Simulate a pre-policy outbox, then migrate and tighten instance constraints.
        self.ledger.connection.execute("DELETE FROM otlp_policy_bindings")
        self.policy()
        self.ledger.projection_policy_values["projection_policy"] = {"constraints": {"redaction": "metadata"}}
        result = self.worker(lambda *_: self.fail("legacy payload sent unredacted")).publish(command_id="legacy")
        self.assertEqual("queued_redaction_changed", result["delivery"]["outcome"]["reason"])


class ProjectionAdmissionTests(unittest.TestCase):
    setUp = runtime_fixture.VerifiedDeliveryTests.setUp
    tearDown = runtime_fixture.VerifiedDeliveryTests.tearDown

    def test_runtime_atomic_admission_replay_and_restart(self):
        with FactoryRuntime(self.config, runner=runtime_fixture.EditingRunner()) as runtime:
            run = runtime.start_issue("demo", "POLICY", execution_override={"projection_policy": {"destinations": []}})
            frozen = settings_view(runtime.ledger, run)
            self.assertEqual("admission-v1", frozen["migration"])
            direct = runtime.kernels["demo"].begin("demo", "DIRECT", {}, command_id="direct")
            self.assertIsNotNone(settings_view(runtime.ledger, direct))
            before = runtime.ledger.connection.execute("SELECT COUNT(*) FROM workflow_executions").fetchone()[0]
            def fail(*args):
                record_admission(*args)
                raise RuntimeError("rollback")
            with patch("dotfactory.projection_policy.record_admission", side_effect=fail), self.assertRaisesRegex(RuntimeError, "rollback"):
                runtime.start_issue("demo", "ROLLBACK")
            self.assertEqual(before, runtime.ledger.connection.execute("SELECT COUNT(*) FROM workflow_executions").fetchone()[0])
        with FactoryRuntime(self.config, runner=runtime_fixture.EditingRunner()) as runtime:
            self.assertEqual(frozen, settings_view(runtime.ledger, run))
            self.assertEqual(run, runtime.start_issue("demo", "POLICY"))
            with self.assertRaisesRegex(LifecycleError, "frozen"):
                runtime.start_issue("demo", "POLICY", execution_override={"projection_policy": {"redaction": "metadata"}})


if __name__ == "__main__":
    unittest.main()
