"""Frozen egress choices for the instance's existing Linear/Logfire registrations."""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime

from .evidence_policy import digest, encoded
from .ledger import LedgerError


DESTINATIONS = {"linear", "logfire", "dataset"}
FIELDS = {"destinations", "redaction", "redact_fields", "max_delivery_age_seconds"}


class ProjectionPolicyBlocked(LedgerError):
    pass


def validate_layer(layer):
    if not isinstance(layer, dict) or set(layer) - FIELDS:
        raise ValueError("projection_policy accepts destinations, redaction, redact_fields, max_delivery_age_seconds; remote deletion is unsupported")
    if "destinations" in layer and (not isinstance(layer["destinations"], list) or any(
            not isinstance(v, str) or v not in DESTINATIONS for v in layer["destinations"])):
        raise ValueError("projection destinations must name linear, logfire, or dataset")
    if "redaction" in layer and layer["redaction"] not in ("standard", "metadata"):
        raise ValueError("projection redaction must be standard or metadata")
    value = layer.get("max_delivery_age_seconds")
    if value is not None and (type(value) is not int or not 1 <= value <= 315360000):
        raise ValueError("max_delivery_age_seconds must be null or 1..315360000")
    from .evidence_policy import validate_layer as evidence_layer
    evidence_layer({"redact_fields": layer.get("redact_fields", [])})


def registrations(values, project, environment):
    projections = values.get("projections", {})
    linear = projections.get("linear", {})
    logfire = projections.get("logfire", {})
    tracker = values["projects"][project]["tracker"]
    identity = {}
    for key in ("project_id", "team_id"):
        identity[key] = tracker.get(key) or environment.get(tracker.get(key + "_env", ""))
    return {
        "linear": {"enabled": bool(linear.get("enabled")), "identity": {
            "endpoint": linear.get("endpoint", "https://api.linear.app/graphql"),
            "tracker": identity, "token_ref": linear.get("token_env", "LINEAR_API_KEY"),
            "agent_token_ref": linear.get("agent_token_env", "LINEAR_DOTFACTORY_AGENT_TOKEN"),
            "agent_sessions": bool(linear.get("agent_sessions_enabled")),
            "session_url_template": linear.get("agent_session_url_template")}},
        "logfire": {"enabled": bool(logfire.get("enabled")), "identity": {
            "endpoint": environment.get(logfire.get("endpoint_env", "OTEL_EXPORTER_OTLP_ENDPOINT"), "https://logfire-us.pydantic.dev").rstrip("/"),
            "endpoint_ref": logfire.get("endpoint_env", "OTEL_EXPORTER_OTLP_ENDPOINT"),
            "credential_ref": logfire.get("headers_env", "OTEL_EXPORTER_OTLP_HEADERS"),
            "project": logfire.get("project"), "region": logfire.get("region", "us")}},
        "dataset": {"enabled": bool(logfire.get("dataset_enabled")), "identity": {
            "dataset_name": logfire.get("dataset_name", "dotfactory-executions"),
            "credential_ref": logfire.get("dataset_api_key_env"),
            "project": logfire.get("project"), "region": logfire.get("region", "us")}},
    }


def constraints(values):
    section = values.get("projection_policy", {})
    if not isinstance(section, dict) or set(section) - {"defaults", "constraints"}:
        raise ValueError("instance projection_policy accepts defaults and constraints")
    validate_layer(section.get("defaults", {}))
    rule = section.get("constraints", {})
    if not isinstance(rule, dict) or set(rule) - {"required_destinations", "redaction", "redact_fields", "max_delivery_age_seconds"}:
        raise ValueError("unknown projection constraint; remote deletion is unsupported")
    validate_layer({("destinations" if k == "required_destinations" else k): v for k, v in rule.items()})
    return rule


def constrain(effective, rule, provenance, conflicts):
    def change(key, value):
        if effective[key] != value:
            conflicts.append({"field": key, "replaced_origin": provenance[key], "winner_origin": "instance:constraint"})
            effective[key], provenance[key] = value, "instance:constraint"
    change("destinations", sorted(set(effective["destinations"]) | set(rule.get("required_destinations", []))))
    change("redact_fields", sorted(set(effective["redact_fields"]) | set(rule.get("redact_fields", []))))
    if rule.get("redaction") == "metadata":
        change("redaction", "metadata")
    age = rule.get("max_delivery_age_seconds")
    if age is not None and (effective["max_delivery_age_seconds"] is None or effective["max_delivery_age_seconds"] > age):
        change("max_delivery_age_seconds", age)


def resolve(values, project, override=None, *, environment=None, legacy=False):
    environment = os.environ if environment is None else environment
    profiles = registrations(values, project, environment)
    rule = constraints(values)
    effective = {"destinations": sorted(k for k, v in profiles.items() if v["enabled"]),
                 "redaction": "standard", "redact_fields": [], "max_delivery_age_seconds": None}
    sources = {k: "legacy-v0" if legacy else "instance:registration" for k in effective}
    conflicts = []
    run = {} if override is None else override
    layers = () if legacy else (("instance", values.get("projection_policy", {}).get("defaults", {})),
        ("project", values["projects"][project].get("projection_policy", {})), ("run", run))
    for origin, layer in layers:
        validate_layer(layer)
        for key, value in layer.items():
            if effective[key] != value:
                conflicts.append({"field": key, "replaced_origin": sources[key], "winner_origin": origin})
            effective[key], sources[key] = copy.deepcopy(value), origin
    if not legacy:
        constrain(effective, rule, sources, conflicts)
    if any(not profiles[k]["enabled"] for k in effective["destinations"]):
        raise ValueError("projection policy cannot enable an instance-disabled destination")
    if not legacy and values["projects"][project].get("linear_planning", {}).get("enabled"):
        if "linear" not in effective["destinations"] or effective["redaction"] == "metadata" or effective["redact_fields"]:
            raise ValueError("native Linear planning requires enabled standard Linear projection without extra field redaction")
    snapshot = {"schema_version": 1, "scope": "hosted_projection", "project_key": project,
                "effective": effective, "provenance": sources, "conflicts": conflicts,
                "bindings": {key: {"reference": "projections." + ("logfire" if key == "dataset" else key),
                                   "digest": digest(profiles[key]["identity"])} for key in effective["destinations"]},
                "run_override": copy.deepcopy(run), "migration": "legacy-v0" if legacy else "admission-v1"}
    snapshot["digest"] = digest(snapshot)
    return snapshot


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS projection_policies (execution_id TEXT PRIMARY KEY REFERENCES workflow_executions(id),policy_json TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS projection_policy_receipts (execution_id TEXT NOT NULL,destination TEXT NOT NULL,reason TEXT NOT NULL,policy_digest TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(execution_id,destination))")
    db.execute("CREATE TABLE IF NOT EXISTS projection_policy_migrations (project_key TEXT PRIMARY KEY,completed_at TEXT NOT NULL)")


def record_admission(db, execution_id, project, snapshot):
    initialize(db)
    validate_snapshot(snapshot, project)
    db.execute("INSERT INTO projection_policies VALUES(?,?)", (execution_id, encoded(snapshot).decode()))


def validate_snapshot(value, project):
    if (value.get("schema_version") != 1 or value.get("scope") != "hosted_projection"
            or value.get("project_key") != project
            or value.get("digest") != digest({k: v for k, v in value.items() if k != "digest"})):
        raise LedgerError("frozen projection policy is invalid; restore the original snapshot")
    validate_layer(value["effective"])
    return value


def settings_view(ledger, execution_id):
    if not ledger.connection.execute("SELECT 1 FROM sqlite_master WHERE name='projection_policies'").fetchone():
        return None
    row = ledger.connection.execute("SELECT policy_json FROM projection_policies WHERE execution_id=?", (execution_id,)).fetchone()
    return validate_snapshot(json.loads(row[0]), ledger.current(execution_id)["project_key"]) if row else None


def configure(ledger, values, environment, projects):
    ledger.projection_policy_values = copy.deepcopy(values)
    # Keep only non-secret values needed for identity checks; never retain a credential copy here.
    refs = set()
    for key in projects:
        tracker = values["projects"][key]["tracker"]
        refs.update(tracker.get(k) for k in ("project_id_env", "team_id_env"))
    refs.add(values.get("projections", {}).get("logfire", {}).get("endpoint_env", "OTEL_EXPORTER_OTLP_ENDPOINT"))
    ledger.projection_policy_environment = {key: environment[key] for key in refs if key and key in environment}
    with ledger.transaction() as db:
        initialize(db)
        legacy_projects = {key for key in projects if not db.execute(
            "SELECT 1 FROM projection_policy_migrations WHERE project_key=?", (key,)).fetchone()}
        for row in db.execute("SELECT we.id,wi.project_key FROM workflow_executions we JOIN work_items wi ON wi.id=we.work_item_id "
                              "LEFT JOIN projection_policies pp ON pp.execution_id=we.id WHERE pp.execution_id IS NULL").fetchall():
            if row[1] in legacy_projects:
                record_admission(db, row[0], row[1], resolve(values, row[1], environment=environment, legacy=True))
        # A later missing row is corruption, not permission to repeat legacy adoption.
        for key in legacy_projects:
            db.execute("INSERT INTO projection_policy_migrations VALUES(?,?)", (key, ledger.clock()))


def authorize(ledger, execution_id, destination, *, endpoint=None, dataset_name=None, project=None, region=None, source_at=None, record=True):
    snapshot = settings_view(ledger, execution_id)
    # Uncomposed library users retain the old contract. An owning runtime always installs policy.
    managed = hasattr(ledger, "projection_policy_values") or ledger.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projection_policies'").fetchone()
    if snapshot is None and not managed:
        return {"allowed": True, "reason": "unmanaged", "delivery_digest": "unmanaged", "effective": None}
    reason = "allowed"
    if snapshot is None:
        reason = "policy_missing"
        effective = None
    else:
        effective = copy.deepcopy(snapshot["effective"])
        values = getattr(ledger, "projection_policy_values", None)
        if values is None or snapshot["project_key"] not in values["projects"]:
            reason = "registration_unavailable"
        else:
            rule = constraints(values)
            # New mandatory destinations cannot grant previously absent send authority.
            bounded = {**rule, "required_destinations": []}
            constrain(effective, bounded, dict(snapshot["provenance"]), [])
            profile = registrations(values, snapshot["project_key"], ledger.projection_policy_environment)[destination]
            if destination not in snapshot["effective"]["destinations"]:
                reason = "opted_out"
            elif not profile["enabled"]:
                reason = "destination_disabled"
            elif digest(profile["identity"]) != snapshot["bindings"][destination]["digest"]:
                reason = "destination_changed"
            elif endpoint is not None and endpoint.rstrip("/") != profile["identity"].get("endpoint", "").rstrip("/"):
                reason = "transport_destination_mismatch"
            elif dataset_name is not None and dataset_name != profile["identity"].get("dataset_name"):
                reason = "transport_destination_mismatch"
            elif project is not None and project != profile["identity"].get("project"):
                reason = "transport_destination_mismatch"
            elif region is not None and region != profile["identity"].get("region"):
                reason = "transport_destination_mismatch"
            elif effective["max_delivery_age_seconds"] is not None and source_at is not None:
                try:
                    now = datetime.fromisoformat(ledger.clock().replace("Z", "+00:00"))
                    age = (now - datetime.fromisoformat(source_at.replace("Z", "+00:00"))).total_seconds()
                    if age > effective["max_delivery_age_seconds"]:
                        reason = "delivery_expired"
                except (ValueError, TypeError, AttributeError):
                    reason = "source_time_invalid"
    value = {"allowed": reason == "allowed", "reason": reason, "effective": effective,
             "policy_digest": snapshot["digest"] if snapshot else "missing"}
    value["delivery_digest"] = digest({"policy": value["policy_digest"], "effective": effective})
    if record:
        with ledger.transaction() as db:
            db.execute("INSERT INTO projection_policy_receipts VALUES(?,?,?,?,?) ON CONFLICT(execution_id,destination) "
                       "DO UPDATE SET reason=excluded.reason,policy_digest=excluded.policy_digest,recorded_at=excluded.recorded_at",
                       (execution_id, destination, reason, value["policy_digest"], ledger.clock()))
    return value


def sanitize(ledger, execution_id, destination, value):
    decision = authorize(ledger, execution_id, destination, record=False)
    if decision["effective"] is None:
        return value
    from .evidence_bundle import _redact
    return _redact(value, set(decision["effective"]["redact_fields"]))


def linear_body(ledger, execution_id, body):
    decision = authorize(ledger, execution_id, "linear", record=False)
    if decision["effective"] and (decision["effective"]["redaction"] == "metadata" or decision["effective"]["redact_fields"]):
        # Rendered prose has lost field boundaries; never guess which fragment was private.
        return "Factory run `" + execution_id + "`: evidence withheld by metadata-only policy. Inspect the local run status."
    return sanitize(ledger, execution_id, "linear", body)


def span_policy(ledger, execution_id, span):
    decision = authorize(ledger, execution_id, "logfire", record=False)
    if decision["effective"] and (decision["effective"]["redaction"] == "metadata" or decision["effective"]["redact_fields"]):
        span = copy.deepcopy(span)
        span["name"] = "dotfactory.observation"
        allowed = {"dotfactory.mapping.version", "dotfactory.execution.id", "dotfactory.record.id",
                   "dotfactory.record.seq", "dotfactory.structural", "dotfactory.derived",
                   "dotfactory.duration.known", "dotfactory.capture.complete", "dotfactory.ownership.complete"}
        span["attributes"] = [item for item in span["attributes"] if item["key"] in allowed]
    # Field rules cannot remove OTLP envelope keys or trace/ownership identities.
    # Extra fields conservatively select the metadata allowlist above instead.
    if decision["effective"]:
        from .evidence_bundle import _redact
        return _redact(span, set())
    return span


def dataset_case_policy(ledger, execution_id, case):
    decision = authorize(ledger, execution_id, "dataset", record=False)
    if decision["effective"] and (decision["effective"]["redaction"] == "metadata" or decision["effective"]["redact_fields"]):
        case = copy.deepcopy(case)
        case["inputs"] = {"intent": {"withheld": True}}
        case["metadata"] = {k: v for k, v in case["metadata"].items()
                            if k in {"execution_id", "projection_policy_digest", "source_observed_at"}}
    if decision["effective"]:
        from .evidence_bundle import _redact
        return _redact(case, set())
    return case
