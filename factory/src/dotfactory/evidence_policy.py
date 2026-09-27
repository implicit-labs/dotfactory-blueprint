"""Frozen policy for local review bundles; hosted projections are a separate lane."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ledger import LedgerError


FIELDS = {"destination", "mode", "retention_seconds", "redact_fields"}
MODES = {"disabled", "summary", "full"}
DEFAULT = {"destination": "operator-local", "mode": "full",
           "retention_seconds": None, "redact_fields": []}
BUILTIN = {"kind": "local_review", "enabled": True, "root": None}
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


def local_path(value):
    path = Path(value).absolute()
    # macOS system aliases are not user-controlled export indirections.
    for alias in (Path("/tmp"), Path("/var")):
        if (alias == path or alias in path.parents) and alias.is_symlink():
            target = Path("/private") / alias.name
            if alias.resolve() == target:
                path = target / path.relative_to(alias)
    return path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def validate_layer(layer):
    if not isinstance(layer, dict) or set(layer) - FIELDS:
        raise ValueError("evidence_policy accepts destination, mode, retention_seconds, redact_fields")
    if "destination" in layer and (not isinstance(layer["destination"], str)
                                  or not NAME.fullmatch(layer["destination"])):
        raise ValueError("evidence destination must be a registered name")
    if "mode" in layer and (not isinstance(layer["mode"], str) or layer["mode"] not in MODES):
        raise ValueError("evidence mode must be disabled, summary, or full")
    seconds = layer.get("retention_seconds")
    if seconds is not None and (type(seconds) is not int or not 1 <= seconds <= 315360000):
        raise ValueError("retention_seconds must be null or 1..315360000")
    fields = layer.get("redact_fields", [])
    if (not isinstance(fields, list) or any(not isinstance(v, str) or not re.fullmatch(
            r"[a-zA-Z_][a-zA-Z0-9_]{0,63}", v) for v in fields)):
        raise ValueError("redact_fields must be a list of JSON field names")


def registry(values):
    section = values.get("evidence_policy", {})
    if not isinstance(section, dict) or set(section) - {"destinations", "defaults", "constraints"}:
        raise ValueError("instance evidence_policy accepts destinations, defaults, constraints")
    destinations = section.get("destinations", {})
    if not isinstance(destinations, dict):
        raise ValueError("evidence destinations must be an object")
    result = {"operator-local": copy.deepcopy(BUILTIN)}
    for name, profile in destinations.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or name == "operator-local":
            raise ValueError("evidence destination name is invalid or reserved")
        if (not isinstance(profile, dict) or not {"kind", "enabled", "root"}.issubset(profile)
                or set(profile) - {"kind", "enabled", "root", "projects"}):
            raise ValueError("local evidence destination requires kind, enabled, root; optional projects; no credentials")
        if profile["kind"] != "local_review" or type(profile["enabled"]) is not bool:
            raise ValueError("only local_review evidence destinations are supported")
        projects = profile.get("projects", list(values["projects"]))
        if not isinstance(projects, list) or any(not isinstance(p, str) or p not in values["projects"] for p in projects):
            raise ValueError("evidence destination projects must name registered projects")
        root = profile["root"]
        if not isinstance(root, str) or not Path(root).is_absolute() or ".." in Path(root).parts:
            raise ValueError("local evidence root must be an absolute path without traversal")
        result[name] = {**profile, "root": str(local_path(root))}
    validate_layer(section.get("defaults", {}))
    constraints = section.get("constraints", {})
    if not isinstance(constraints, dict) or set(constraints) - {
            "allowed_destinations", "allow_full", "require_export", "max_retention_seconds", "redact_fields"}:
        raise ValueError("unknown evidence policy constraint")
    for key in ("allow_full", "require_export"):
        if key in constraints and type(constraints[key]) is not bool:
            raise ValueError(f"evidence constraint {key} must be boolean")
    allowed = constraints.get("allowed_destinations", list(result))
    if not isinstance(allowed, list) or any(not isinstance(v, str) or v not in result for v in allowed):
        raise ValueError("allowed_destinations must name registered evidence destinations")
    validate_layer({"retention_seconds": constraints.get("max_retention_seconds"),
                    "redact_fields": constraints.get("redact_fields", [])})
    return result


def constrain(effective, constraints, provenance, conflicts):
    def replace(key, value):
        if effective[key] != value:
            conflicts.append({"field": key, "replaced_origin": provenance[key],
                              "winner_origin": "instance:constraint"})
            effective[key], provenance[key] = value, "instance:constraint"
    if not constraints.get("allow_full", True) and effective["mode"] == "full":
        replace("mode", "summary")
    if constraints.get("require_export") and effective["mode"] == "disabled":
        raise ValueError("mandatory evidence export cannot be disabled")
    maximum = constraints.get("max_retention_seconds")
    if maximum is not None and (effective["retention_seconds"] is None or effective["retention_seconds"] > maximum):
        replace("retention_seconds", maximum)
    replace("redact_fields", sorted(set(effective["redact_fields"]) | set(constraints.get("redact_fields", []))))


def resolve(values, project_key, override=None, *, legacy=False):
    profiles = registry(values)
    if project_key not in values["projects"]:
        raise ValueError("unknown evidence policy project")
    section = values.get("evidence_policy", {})
    effective = copy.deepcopy(DEFAULT)
    provenance = {key: "legacy-v0" if legacy else "builtin" for key in effective}
    conflicts = []
    run = {} if override is None else override
    layers = () if legacy else (
        ("instance", section.get("defaults", {})),
        ("project", values["projects"][project_key].get("evidence_policy", {})),
        ("run", run),
    )
    for origin, layer in layers:
        validate_layer(layer)
        for key, value in layer.items():
            if effective[key] != value:
                conflicts.append({"field": key, "replaced_origin": provenance[key], "winner_origin": origin})
            effective[key], provenance[key] = copy.deepcopy(value), origin
    constraints = {} if legacy else section.get("constraints", {})
    constrain(effective, constraints, provenance, conflicts)
    name = effective["destination"]
    if name not in profiles or name not in constraints.get("allowed_destinations", profiles):
        raise ValueError("evidence destination is unregistered or not allowed")
    if project_key not in profiles[name].get("projects", values["projects"]):
        raise ValueError("evidence destination is not approved for this project")
    result = {"schema_version": 1, "scope": "local_review", "project_key": project_key,
              "effective": effective, "provenance": provenance, "conflicts": conflicts,
              "destination_digest": digest(profiles[name]), "run_override": copy.deepcopy(run),
              "migration": "legacy-v0" if legacy else "admission-v1"}
    result["digest"] = digest(result)
    return result


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS evidence_policies (execution_id TEXT PRIMARY KEY "
               "REFERENCES workflow_executions(id),policy_json TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS evidence_exports (id TEXT PRIMARY KEY,execution_id TEXT NOT NULL "
               "REFERENCES workflow_executions(id),path TEXT UNIQUE NOT NULL,manifest_json TEXT NOT NULL,"
               "status TEXT NOT NULL,expires_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS evidence_policy_migrations (project_key TEXT PRIMARY KEY,completed_at TEXT NOT NULL)")


def record_admission(db, execution_id, project_key, snapshot):
    initialize(db)
    validate_snapshot(snapshot, project_key)
    db.execute("INSERT INTO evidence_policies VALUES(?,?)", (execution_id, encoded(snapshot).decode()))


def validate_snapshot(snapshot, project_key):
    if (snapshot.get("schema_version") != 1 or snapshot.get("scope") != "local_review"
            or snapshot.get("project_key") != project_key
            or snapshot.get("digest") != digest({k: v for k, v in snapshot.items() if k != "digest"})):
        raise LedgerError("frozen evidence policy is invalid; restore its original snapshot")
    validate_layer(snapshot["effective"])
    return snapshot


def settings_view(ledger, execution_id):
    if not ledger.connection.execute("SELECT 1 FROM sqlite_master WHERE name='evidence_policies'").fetchone():
        return None
    row = ledger.connection.execute("SELECT policy_json FROM evidence_policies WHERE execution_id=?", (execution_id,)).fetchone()
    if not row:
        return None
    return validate_snapshot(json.loads(row[0]), ledger.current(execution_id)["project_key"])


def configure(ledger, values, projects):
    ledger.evidence_configuration = copy.deepcopy(values)
    with ledger.transaction() as db:
        initialize(db)
        legacy_projects = {key for key in projects if not db.execute(
            "SELECT 1 FROM evidence_policy_migrations WHERE project_key=?", (key,)).fetchone()}
        for row in db.execute("SELECT we.id,wi.project_key FROM workflow_executions we "
                              "JOIN work_items wi ON wi.id=we.work_item_id "
                              "LEFT JOIN evidence_policies ep ON ep.execution_id=we.id WHERE ep.execution_id IS NULL").fetchall():
            if row[1] in legacy_projects:
                record_admission(db, row[0], row[1], resolve(values, row[1], legacy=True))
        for key in legacy_projects:
            db.execute("INSERT INTO evidence_policy_migrations VALUES(?,?)", (key, ledger.clock()))


def authorization(ledger, execution_id, output):
    snapshot = settings_view(ledger, execution_id)
    if snapshot is None:
        raise LedgerError("review export requires a migrated evidence policy; open the run through FactoryRuntime")
    values = getattr(ledger, "evidence_configuration", None)
    if values is None:
        raise LedgerError("review export requires its owning configuration; open the run through FactoryRuntime")
    profiles = registry(values)
    effective = copy.deepcopy(snapshot["effective"])
    constraints = values.get("evidence_policy", {}).get("constraints", {})
    constrain(effective, constraints, dict(snapshot["provenance"]), [])
    name = effective["destination"]
    profile = profiles.get(name)
    if (not profile or name not in constraints.get("allowed_destinations", profiles)
            or snapshot["project_key"] not in profile.get("projects", values["projects"])
            or not profile["enabled"] or digest(profile) != snapshot["destination_digest"]):
        raise LedgerError("evidence destination unavailable or changed; restore the frozen profile")
    if effective["mode"] == "disabled":
        raise LedgerError("review export is disabled by frozen evidence policy")
    destination = local_path(output)
    if ".." in destination.parts:
        raise LedgerError("evidence output must not traverse parent directories")
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise LedgerError("evidence output must not traverse symlinks")
    if profile["root"]:
        root = local_path(profile["root"])
        if root not in destination.parents:
            raise LedgerError("evidence output must be inside the frozen destination root")
    return snapshot, effective, destination


def expiration(ledger, seconds):
    if seconds is None:
        return None
    now = datetime.fromisoformat(ledger.clock().replace("Z", "+00:00"))
    return (now + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()
