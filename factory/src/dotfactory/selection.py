"""Registered project/run workflow and stage-profile selection before admission."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import replace

from .workflow import WorkflowDefinition, _state_definition_v1, load_workflow


PROFILE_FIELDS = frozenset({
    "runner", "model", "reasoning_effort", "skills", "capabilities",
    "resources", "timeout", "max_retries",
})
LIST_FIELDS = frozenset({"skills", "capabilities", "resources"})
EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
DURATION = re.compile(r"[1-9][0-9]*(?:ms|s|m|h)\Z")


def _name(value, label):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(f"{label} must be a registered lowercase name")
    return value


def validate_profiles(values):
    """Validate the instance registry; names are the project/run safety ceiling."""
    profiles = values.get("selection_profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("selection_profiles must be an object")
    runners = values.get("runners") or {}
    for name, fields in profiles.items():
        _name(name, "selection profile")
        if not isinstance(fields, dict) or not fields or set(fields) - PROFILE_FIELDS:
            raise ValueError(f"selection profile {name} has no fields or contains unknown fields")
        for key, value in fields.items():
            if key == "runner":
                if not isinstance(value, str) or value not in runners:
                    raise ValueError(f"selection profile {name} references unknown runner {value}")
            elif key in ("model",):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"selection profile {name}.{key} must be nonempty text")
            elif key == "reasoning_effort":
                if not isinstance(value, str) or value not in EFFORTS:
                    raise ValueError(f"selection profile {name}.reasoning_effort is unsupported")
            elif key in LIST_FIELDS:
                if not isinstance(value, list) or any(
                    not isinstance(item, str) or not item or not NAME.fullmatch(item)
                    for item in value
                ) or len(value) != len(set(value)):
                    raise ValueError(f"selection profile {name}.{key} must be unique names")
            elif key == "timeout":
                if not isinstance(value, str) or not DURATION.fullmatch(value):
                    raise ValueError(f"selection profile {name}.timeout must be a positive duration")
            elif key == "max_retries":
                if type(value) is not int or value < 0:
                    raise ValueError(f"selection profile {name}.max_retries must be nonnegative")
    return profiles


def validate_layer(layer, profiles, workflows, label):
    allowed = {"workflow", "profile", "stage_profiles"}
    if not isinstance(layer, dict) or set(layer) - allowed:
        raise ValueError(f"{label} accepts only workflow, profile, and stage_profiles")
    if "workflow" in layer and layer["workflow"] is not None:
        name = _name(layer["workflow"], f"{label}.workflow")
        if name not in workflows:
            raise ValueError(f"{label}.workflow references unknown workflow {name}")
    if "profile" in layer and layer["profile"] is not None:
        name = _name(layer["profile"], f"{label}.profile")
        if name not in profiles:
            raise ValueError(f"{label}.profile references unknown selection profile {name}")
    stages = layer.get("stage_profiles", {})
    if not isinstance(stages, dict):
        raise ValueError(f"{label}.stage_profiles must be an object")
    for state, profile in stages.items():
        if not isinstance(state, str) or not state:
            raise ValueError(f"{label}.stage_profiles requires stage names")
        if profile is not None:
            name = _name(profile, f"{label}.stage_profiles.{state}")
            if name not in profiles:
                raise ValueError(f"{label}.stage_profiles.{state} references unknown selection profile {name}")


def split_override(override):
    """Keep worker-placement overrides separate from graph/profile selection."""
    if override is None:
        return None, {}
    if not isinstance(override, dict) or set(override) - {"stages", "workflow", "profile", "stage_profiles", "evidence_policy", "projection_policy"}:
        raise ValueError("execution override accepts stages, workflow, profile, stage_profiles, evidence_policy, projection_policy")
    placement = {"stages": copy.deepcopy(override["stages"])} if "stages" in override else {}
    selection = {key: copy.deepcopy(value) for key, value in override.items() if key not in {"stages", "evidence_policy", "projection_policy"}}
    return placement, selection


def resolve(config, project_key, override=None):
    """Return a frozen graph variant and explainable selection, without probing hosts."""
    if project_key not in config.values["projects"]:
        raise ValueError(f"unknown project: {project_key}")
    profiles = validate_profiles(config.values)
    placement, run = split_override(override)
    workflows = config.values["workflows"] if config.values["schema_version"] >= 3 else {}
    project = config.values["projects"][project_key]
    project_layer = {key: project[key] for key in ("profile", "stage_profiles") if key in project}
    validate_layer(project_layer, profiles, workflows, "project selection")
    validate_layer(run, profiles, workflows, "run selection")
    workflow_name = run.get("workflow", project.get("workflow", config.values.get("default_workflow")))
    if workflow_name is None:
        workflow_name = config.values["default_workflow"]
    selected = config.resolve_workflow(project_key, name=workflow_name)
    definition = load_workflow(selected["path"], profile_paths=selected["profile_paths"],
                               factory_defaults=selected["defaults"])
    states = copy.deepcopy(list(definition.states))
    work = {state["id"] for state in states if state["kind"] == "work"}
    for label, layer in (("project", project_layer), ("run", run)):
        unknown = set(layer.get("stage_profiles", {})) - work
        if unknown:
            raise ValueError(f"{label}.stage_profiles names a non-work or unknown stage: {sorted(unknown)[0]}")
    runner_registry = config.resolve_runners()
    stage_report = {}
    for state in states:
        if state["kind"] != "work":
            continue
        name = state["id"]
        execution = state.setdefault("execution", {})
        sources = state.setdefault("config_sources", {})
        applied = []
        conflicts = []
        if project_layer.get("profile"):
            applied.append(("project", project_layer["profile"]))
        if name in project_layer.get("stage_profiles", {}):
            chosen = project_layer["stage_profiles"][name]
            applied = [] if chosen is None else [*applied, ("project:stage", chosen)]
        if "profile" in run:
            applied = [] if run["profile"] is None else [*applied, ("run", run["profile"])]
        if name in run.get("stage_profiles", {}):
            chosen = run["stage_profiles"][name]
            applied = [] if chosen is None else [*applied, ("run:stage", chosen)]
        for label, profile_name in applied:
            for field, value in profiles[profile_name].items():
                winner = f"{label}:{profile_name}"
                if field in execution and execution[field] != value:
                    conflicts.append({"field": field,
                                      "replaced_origin": sources.get(field, "workflow"),
                                      "winner_origin": winner})
                execution[field] = copy.deepcopy(value)
                sources[field] = winner
        runner = execution.get("runner")
        if config.values["schema_version"] >= 6 and runner is not None and runner not in runner_registry:
            raise ValueError(f"{name} references unregistered runner {runner}")
        if runner in runner_registry:
            for field, route_key in (("model", "default_model"),
                                     ("reasoning_effort", "default_reasoning_effort")):
                default = runner_registry[runner].get(route_key)
                if field not in execution and default is not None:
                    execution[field] = default
                    sources[field] = f"runner:{runner}"
        missing = (set(execution.get("capabilities", [])) - set(runner_registry[runner]["capabilities"])) if runner in runner_registry else set()
        if missing:
            raise ValueError(f"{name} requests capabilities unavailable on runner {runner}: {', '.join(sorted(missing))}")
        config.validate_resource_names(project_key, execution.get("resources", []))
        state["execution"] = execution
        state["config_sources"] = sources
        state["state_definition"] = _state_definition_v1(
            state, list(definition.transitions) + list(definition.global_transitions)
        )
        stage_report[name] = {"profiles": [profile_name for _label, profile_name in applied],
                              "effective": {key: copy.deepcopy(execution[key]) for key in PROFILE_FIELDS if key in execution},
                              "provenance": {key: sources[key] for key in PROFILE_FIELDS if key in sources},
                              "conflicts": conflicts}
    normalized = copy.deepcopy(definition.normalized)
    normalized["states"] = states
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    resolved = replace(definition, states=tuple(states), normalized=normalized,
                       digest=hashlib.sha256(canonical.encode()).hexdigest())
    result = {"workflow": {"name": selected["name"],
                           "origin": "run" if "workflow" in run else "project" if "workflow" in project else "instance",
                           "digest": resolved.digest},
              "stages": stage_report,
              "run_override": run}
    result["digest"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return resolved, result, placement
