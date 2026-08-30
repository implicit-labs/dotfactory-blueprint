"""Configuration contract for one factory and its project registry."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
PROJECT_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SECRET_WORDS = ("authorization", "password", "secret", "token", "api_key")
RESOURCE_SCOPES = {"attempt", "execution"}
RESOURCE_MODES = {"exclusive", "namespaced", "capacity", "prerequisite"}


class FactoryConfigError(ValueError):
    pass


def _reject_embedded_secrets(value: Any, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key.endswith("_env"):
                if not isinstance(item, str) or not ENV_NAME.fullmatch(item):
                    raise FactoryConfigError(f"{child} must name an environment variable")
            elif any(word in key.lower() for word in SECRET_WORDS):
                raise FactoryConfigError(f"{child} must be an *_env reference")
            _reject_embedded_secrets(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_embedded_secrets(item, f"{path}[{index}]")


def _require_string(values: dict[str, Any], key: str) -> None:
    if not isinstance(values.get(key), str) or not values[key].strip():
        raise FactoryConfigError(f"config.{key} must be a non-empty string")


def _validate_workflows(values: dict[str, Any]) -> None:
    version = values["schema_version"]
    if version == 2:
        _require_string(values, "workflow_path")
        return
    workflows = values.get("workflows")
    if not isinstance(workflows, dict) or not workflows:
        raise FactoryConfigError("config.workflows must contain at least one workflow")
    _require_string(values, "default_workflow")
    if values["default_workflow"] not in workflows:
        raise FactoryConfigError("config.default_workflow must name a configured workflow")
    for key, workflow in workflows.items():
        path = f"config.workflows.{key}"
        if not isinstance(key, str) or not PROJECT_KEY.fullmatch(key):
            raise FactoryConfigError(f"{path} must use a lowercase hyphenated key")
        if not isinstance(workflow, dict):
            raise FactoryConfigError(f"{path} must be an object")
        if not isinstance(workflow.get("path"), str) or not workflow["path"].strip():
            raise FactoryConfigError(f"{path}.path must be a non-empty string")
        profile_paths = workflow.get("profile_paths", [])
        if not isinstance(profile_paths, list) or any(
            not isinstance(item, str) or not item.strip() for item in profile_paths
        ):
            raise FactoryConfigError(f"{path}.profile_paths must be an array of paths")
        defaults = workflow.get("defaults", {})
        if not isinstance(defaults, dict):
            raise FactoryConfigError(f"{path}.defaults must be an object")


def _validate_preparation(values: dict[str, Any]) -> None:
    if values["schema_version"] < 4:
        return
    preparation = values.get("preparation")
    if not isinstance(preparation, dict):
        raise FactoryConfigError("config.preparation must be an object")
    workspace = preparation.get("workspace")
    if not isinstance(workspace, dict):
        raise FactoryConfigError("config.preparation.workspace must be an object")
    roots = [key for key in ("root", "root_env") if workspace.get(key)]
    if len(roots) > 1:
        raise FactoryConfigError(
            "config.preparation.workspace accepts at most one of root or root_env"
        )
    if "root_env" in workspace and not ENV_NAME.fullmatch(str(workspace["root_env"])):
        raise FactoryConfigError(
            "config.preparation.workspace.root_env must name an environment variable"
        )
    for key in ("remote", "base_ref"):
        if not isinstance(workspace.get(key), str) or not workspace[key].strip():
            raise FactoryConfigError(
                f"config.preparation.workspace.{key} must be a non-empty string"
            )
    if workspace.get("retention") not in ("until_terminal", "explicit"):
        raise FactoryConfigError(
            "config.preparation.workspace.retention must be until_terminal or explicit"
        )
    retry = preparation.get("retry", {})
    if not isinstance(retry, dict):
        raise FactoryConfigError("config.preparation.retry must be an object")
    retry_defaults = {
        "initial_seconds": 5, "maximum_seconds": 60, "deadline_seconds": 900,
    }
    unknown_retry = set(retry) - set(retry_defaults)
    if unknown_retry:
        raise FactoryConfigError("config.preparation.retry contains unknown fields")
    resolved_retry = {**retry_defaults, **retry}
    for key, value in resolved_retry.items():
        if not isinstance(value, int) or value < 1:
            raise FactoryConfigError(
                f"config.preparation.retry.{key} must be a positive integer"
            )
    if resolved_retry["initial_seconds"] > resolved_retry["maximum_seconds"]:
        raise FactoryConfigError(
            "config.preparation.retry.initial_seconds cannot exceed maximum_seconds"
        )
    if resolved_retry["maximum_seconds"] > resolved_retry["deadline_seconds"]:
        raise FactoryConfigError(
            "config.preparation.retry.maximum_seconds cannot exceed deadline_seconds"
        )
    providers = preparation.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise FactoryConfigError(
            "config.preparation.providers must contain at least one provider"
        )
    for name, provider in providers.items():
        path = f"config.preparation.providers.{name}"
        if not isinstance(name, str) or not PROJECT_KEY.fullmatch(name):
            raise FactoryConfigError(f"{path} must use a lowercase hyphenated key")
        if not isinstance(provider, dict):
            raise FactoryConfigError(f"{path} must be an object")
        for key in ("kind", "command"):
            if not isinstance(provider.get(key), str) or not provider[key].strip():
                raise FactoryConfigError(f"{path}.{key} must be a non-empty string")
        if provider["kind"] == "portless":
            if not isinstance(provider.get("version"), str) or not provider["version"].strip():
                raise FactoryConfigError(f"{path}.version must be a non-empty string")
            if not isinstance(provider.get("node_minimum"), int) or provider["node_minimum"] < 1:
                raise FactoryConfigError(f"{path}.node_minimum must be a positive integer")
            if (
                not isinstance(provider.get("preflight_command"), str)
                or not provider["preflight_command"].strip()
            ):
                raise FactoryConfigError(
                    f"{path}.preflight_command must be a non-empty string"
                )
    capabilities = preparation.get("capabilities", {})
    if not isinstance(capabilities, dict):
        raise FactoryConfigError("config.preparation.capabilities must be an object")
    for name, capability in capabilities.items():
        path = f"config.preparation.capabilities.{name}"
        if not isinstance(name, str) or not PROJECT_KEY.fullmatch(name):
            raise FactoryConfigError(f"{path} must use a lowercase hyphenated key")
        if not isinstance(capability, dict):
            raise FactoryConfigError(f"{path} must be an object")
        if capability.get("provider") not in providers:
            raise FactoryConfigError(f"{path}.provider must name a configured provider")
        if capability.get("scope") not in RESOURCE_SCOPES:
            raise FactoryConfigError(
                f"{path}.scope must be attempt or execution"
            )
        if capability.get("mode") not in RESOURCE_MODES:
            raise FactoryConfigError(
                f"{path}.mode must be exclusive, namespaced, capacity, or prerequisite"
            )
        if not isinstance(capability.get("config", {}), dict):
            raise FactoryConfigError(f"{path}.config must be an object")
        provider = providers[capability["provider"]]
        if provider["kind"] == "portless":
            config = capability.get("config", {})
            if not isinstance(config.get("service_name"), str) or not config["service_name"]:
                raise FactoryConfigError(
                    f"{path}.config.service_name must be a non-empty string"
                )
            command = config.get("command")
            if (
                not isinstance(command, list) or not command
                or any(not isinstance(item, str) or not item for item in command)
            ):
                raise FactoryConfigError(
                    f"{path}.config.command must be a non-empty argv array"
                )
            banned = {
                "--force", "--lan", "--tailscale", "--funnel", "--ngrok",
                "--tunnel", "--wildcard", "--tld", "--cert", "--key", "--no-tls",
            }
            forbidden = [
                item for item in command
                if any(item == flag or item.startswith(flag + "=") for flag in banned)
            ]
            if forbidden:
                raise FactoryConfigError(
                    f"{path}.config.command contains forbidden Portless flags"
                )
    for project_key, project in values.get("projects", {}).items():
        override = project.get("workspace")
        if override is None:
            continue
        path = f"config.projects.{project_key}.workspace"
        if not isinstance(override, dict):
            raise FactoryConfigError(f"{path} must be an object")
        unknown = set(override) - {"root", "root_env", "remote", "base_ref", "retention"}
        if unknown:
            raise FactoryConfigError(f"{path} contains unknown fields")
        merged = dict(workspace)
        if "root" in override:
            merged.pop("root_env", None)
        if "root_env" in override:
            merged.pop("root", None)
        merged.update(override)
        roots = [key for key in ("root", "root_env") if merged.get(key)]
        if len(roots) > 1:
            raise FactoryConfigError(f"{path} accepts at most one workspace root")
        if "root_env" in merged and not ENV_NAME.fullmatch(str(merged["root_env"])):
            raise FactoryConfigError(f"{path}.root_env must name an environment variable")
        for key in ("remote", "base_ref"):
            if not isinstance(merged.get(key), str) or not merged[key].strip():
                raise FactoryConfigError(f"{path}.{key} must be a non-empty string")
        if merged.get("retention") not in ("until_terminal", "explicit"):
            raise FactoryConfigError(
                f"{path}.retention must be until_terminal or explicit"
            )


def _validate_projects(projects: Any, workflow_names: set[str] | None = None) -> None:
    if not isinstance(projects, dict) or not projects:
        raise FactoryConfigError("config.projects must contain at least one project")
    for key, project in projects.items():
        path = f"config.projects.{key}"
        if not isinstance(key, str) or not PROJECT_KEY.fullmatch(key):
            raise FactoryConfigError(f"{path} must use a lowercase hyphenated project key")
        if not isinstance(project, dict):
            raise FactoryConfigError(f"{path} must be an object")
        if not isinstance(project.get("display_name"), str) or not project["display_name"].strip():
            raise FactoryConfigError(f"{path}.display_name must be a non-empty string")
        if not isinstance(project.get("enabled_by_default"), bool):
            raise FactoryConfigError(f"{path}.enabled_by_default must be true or false")
        if "workflow" in project and (
            not isinstance(project["workflow"], str)
            or (workflow_names is not None and project["workflow"] not in workflow_names)
        ):
            raise FactoryConfigError(f"{path}.workflow must name a configured workflow")
        tracker = project.get("tracker")
        if (
            not isinstance(tracker, dict)
            or not isinstance(tracker.get("kind"), str)
            or not tracker["kind"].strip()
        ):
            raise FactoryConfigError(f"{path}.tracker.kind must be a non-empty string")
        if "project_slug" in tracker and (
            not isinstance(tracker["project_slug"], str) or not tracker["project_slug"].strip()
        ):
            raise FactoryConfigError(f"{path}.tracker.project_slug must be a non-empty string")
        project_ids = [name for name in ("project_id", "project_id_env") if tracker.get(name)]
        if len(project_ids) != 1:
            raise FactoryConfigError(
                f"{path}.tracker requires exactly one of project_id or project_id_env"
            )
        repository_paths = [
            name for name in ("repository_path", "repository_path_env") if project.get(name)
        ]
        if len(repository_paths) != 1:
            raise FactoryConfigError(
                f"{path} requires exactly one of repository_path or repository_path_env"
            )


def _positive_integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FactoryConfigError(f"{path} must be a positive integer")
    return value


def _validate_scheduler(values: dict[str, Any]) -> None:
    if values["schema_version"] < 5:
        return
    scheduler = values.get("scheduler")
    if not isinstance(scheduler, dict):
        raise FactoryConfigError("config.scheduler must be an object")
    unknown = set(scheduler) - {"poll_interval_ms", "claim_ttl_seconds", "limits"}
    if unknown:
        raise FactoryConfigError("config.scheduler contains unknown fields")
    _positive_integer(
        scheduler.get("poll_interval_ms"), "config.scheduler.poll_interval_ms"
    )
    _positive_integer(
        scheduler.get("claim_ttl_seconds"), "config.scheduler.claim_ttl_seconds"
    )
    limits = scheduler.get("limits")
    if not isinstance(limits, dict):
        raise FactoryConfigError("config.scheduler.limits must be an object")
    if set(limits) - {"host", "projects", "runners"}:
        raise FactoryConfigError("config.scheduler.limits contains unknown fields")
    host = _positive_integer(limits.get("host"), "config.scheduler.limits.host")
    for group in ("projects", "runners"):
        entries = limits.get(group, {})
        if not isinstance(entries, dict):
            raise FactoryConfigError(
                f"config.scheduler.limits.{group} must be an object"
            )
        for key, value in entries.items():
            path = f"config.scheduler.limits.{group}.{key}"
            if not isinstance(key, str) or not PROJECT_KEY.fullmatch(key):
                raise FactoryConfigError(
                    f"{path} must use a lowercase hyphenated key"
                )
            limit = _positive_integer(value, path)
            if limit > host:
                raise FactoryConfigError(f"{path} cannot exceed the host limit")
    unknown_projects = set(limits.get("projects", {})) - set(values["projects"])
    if unknown_projects:
        raise FactoryConfigError(
            "config.scheduler.limits.projects references unknown projects: "
            + ", ".join(sorted(unknown_projects))
        )


@dataclass(frozen=True)
class FactoryConfig:
    path: Path
    values: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> "FactoryConfig":
        resolved = Path(path).expanduser().resolve()
        values = json.loads(resolved.read_text(encoding="utf-8"))
        _reject_embedded_secrets(values)
        if values.get("schema_version") not in (2, 3, 4, 5):
            raise FactoryConfigError(
                "factory config requires schema_version 2, 3, 4, or 5"
            )
        for key in ("factory_id", "ledger_path"):
            _require_string(values, key)
        _validate_workflows(values)
        _validate_preparation(values)
        workflow_names = (
            set(values["workflows"]) if values["schema_version"] >= 3 else None
        )
        _validate_projects(values.get("projects"), workflow_names)
        _validate_scheduler(values)
        return cls(resolved, values)

    @classmethod
    def from_environment(cls) -> "FactoryConfig":
        path = os.environ.get("DOTFACTORY_CONFIG")
        if not path:
            raise FactoryConfigError("DOTFACTORY_CONFIG is not set")
        return cls.load(path)

    @property
    def project_keys(self) -> tuple[str, ...]:
        return tuple(self.values["projects"])

    def selected_project_keys(self, only: list[str] | None = None) -> tuple[str, ...]:
        if only is None:
            return tuple(
                key
                for key, project in self.values["projects"].items()
                if project["enabled_by_default"]
            )
        unknown = [key for key in only if key not in self.values["projects"]]
        if unknown:
            raise FactoryConfigError("unknown project: " + ", ".join(unknown))
        return tuple(dict.fromkeys(only))

    def resolve_project(
        self, project_key: str, *, environment: dict[str, str] | None = None
    ) -> dict[str, Any]:
        if project_key not in self.values["projects"]:
            raise FactoryConfigError(f"unknown project: {project_key}")
        environment = os.environ if environment is None else environment
        project = self.values["projects"][project_key]

        def resolve(direct: str, reference: str, path: str) -> str:
            if direct in project:
                return str(project[direct])
            env_name = str(project[reference])
            value = environment.get(env_name)
            if not value:
                raise FactoryConfigError(f"{path} requires environment variable {env_name}")
            return value

        tracker = project["tracker"]
        if "project_id" in tracker:
            tracker_project_id = str(tracker["project_id"])
        else:
            env_name = str(tracker["project_id_env"])
            tracker_project_id = environment.get(env_name, "")
            if not tracker_project_id:
                raise FactoryConfigError(
                    f"config.projects.{project_key}.tracker requires environment variable {env_name}"
                )
        repository_path = Path(resolve(
            "repository_path", "repository_path_env",
            f"config.projects.{project_key}.repository_path",
        )).expanduser()
        if not repository_path.is_absolute():
            repository_path = self.path.parent / repository_path
        return {
            "project_key": project_key,
            "display_name": project["display_name"],
            "enabled_by_default": project["enabled_by_default"],
            "repository_path": str(repository_path.resolve()),
            "tracker_kind": tracker["kind"],
            "tracker_project_id": tracker_project_id,
            "tracker_project_slug": tracker.get("project_slug"),
            "workflow": project.get("workflow", self.values.get("default_workflow")),
        }

    def resolve_workflow(self, project_key: str) -> dict[str, Any]:
        if project_key not in self.values["projects"]:
            raise FactoryConfigError(f"unknown project: {project_key}")
        project = self.values["projects"][project_key]
        if self.values["schema_version"] == 2:
            path = self.values["workflow_path"]
            profile_paths: list[str] = []
            defaults: dict[str, Any] = {}
            name = "legacy-default"
        else:
            name = str(project.get("workflow", self.values["default_workflow"]))
            workflow = self.values["workflows"][name]
            path = workflow["path"]
            profile_paths = list(workflow.get("profile_paths", []))
            defaults = dict(workflow.get("defaults", {}))

        def relative(value: str) -> str:
            candidate = Path(value).expanduser()
            if candidate.is_absolute():
                return str(candidate)
            return str((self.path.parent / candidate).resolve())

        return {
            "name": name,
            "path": relative(str(path)),
            "profile_paths": [relative(item) for item in profile_paths],
            "defaults": defaults,
        }

    def resolve_preparation(
        self, project_key: str, *, environment: dict[str, str] | None = None
    ) -> dict[str, Any]:
        if project_key not in self.values["projects"]:
            raise FactoryConfigError(f"unknown project: {project_key}")
        if self.values["schema_version"] < 4:
            return {
                "workspace": None,
                "providers": {},
                "capabilities": {},
            }
        environment = os.environ if environment is None else environment
        preparation = self.values["preparation"]
        workspace = dict(preparation["workspace"])
        override = self.values["projects"][project_key].get("workspace", {})
        if "root" in override:
            workspace.pop("root_env", None)
        if "root_env" in override:
            workspace.pop("root", None)
        workspace.update(override)
        if "root" in workspace:
            candidate = Path(str(workspace["root"])).expanduser()
            root = candidate if candidate.is_absolute() else self.path.parent / candidate
        elif "root_env" in workspace:
            env_name = str(workspace["root_env"])
            value = environment.get(env_name)
            if not value:
                raise FactoryConfigError(
                    f"config.preparation.workspace requires environment variable {env_name}"
                )
            root = Path(value).expanduser()
            if not root.is_absolute():
                raise FactoryConfigError(
                    f"config.preparation.workspace environment variable {env_name} "
                    "must contain an absolute path"
                )
        else:
            project = self.values["projects"][project_key]
            if "repository_path" in project:
                repository_value = str(project["repository_path"])
            else:
                env_name = str(project["repository_path_env"])
                repository_value = environment.get(env_name, "")
                if not repository_value:
                    raise FactoryConfigError(
                        f"config.projects.{project_key}.repository_path requires "
                        f"environment variable {env_name}"
                    )
            repository = Path(repository_value).expanduser()
            if not repository.is_absolute():
                repository = self.path.parent / repository
            root = repository.resolve() / ".worktrees"
        return {
            "workspace": {
                "root": str(root.resolve()),
                "remote": str(workspace["remote"]),
                "base_ref": str(workspace["base_ref"]),
                "retention": str(workspace["retention"]),
            },
            "providers": {
                key: dict(value) for key, value in preparation["providers"].items()
            },
            "retry": {
                "initial_seconds": 5,
                "maximum_seconds": 60,
                "deadline_seconds": 900,
                **dict(preparation.get("retry", {})),
            },
            "capabilities": {
                key: dict(value) for key, value in preparation.get("capabilities", {}).items()
            },
        }

    def resolve_scheduler(self) -> dict[str, Any]:
        if self.values["schema_version"] < 5:
            return {
                "poll_interval_ms": 30000,
                "claim_ttl_seconds": 120,
                "limits": {"host": 1, "projects": {}, "runners": {}},
            }
        scheduler = self.values["scheduler"]
        limits = scheduler["limits"]
        return {
            "poll_interval_ms": int(scheduler["poll_interval_ms"]),
            "claim_ttl_seconds": int(scheduler["claim_ttl_seconds"]),
            "limits": {
                "host": int(limits["host"]),
                "projects": {
                    str(key): int(value)
                    for key, value in limits.get("projects", {}).items()
                },
                "runners": {
                    str(key): int(value)
                    for key, value in limits.get("runners", {}).items()
                },
            },
        }

    def validate_resource_names(
        self, project_key: str, resources: Any
    ) -> tuple[str, ...]:
        if not isinstance(resources, list) or any(
            not isinstance(item, str) or not item for item in resources
        ):
            raise FactoryConfigError(
                f"config.projects.{project_key}.resources must be an array of names"
            )
        if project_key not in self.values["projects"]:
            raise FactoryConfigError(f"unknown project: {project_key}")
        configured = (
            set(self.values.get("preparation", {}).get("capabilities", {}))
            if self.values["schema_version"] >= 4 else set()
        )
        unknown = sorted(set(resources) - configured)
        if unknown:
            raise FactoryConfigError(
                f"config.projects.{project_key}.resources references unknown capabilities: "
                + ", ".join(unknown)
            )
        return tuple(dict.fromkeys(resources))

    def configure_ledger(
        self, ledger: Any, *, environment: dict[str, str] | None = None,
        only: list[str] | None = None,
    ) -> None:
        ledger.configure_factory(self.values["factory_id"])
        for project_key in self.selected_project_keys(only):
            project = self.resolve_project(project_key, environment=environment)
            ledger.register_project(
                project_key,
                display_name=project["display_name"],
                tracker_kind=project["tracker_kind"],
                tracker_project_id=project["tracker_project_id"],
                tracker_project_slug=project["tracker_project_slug"],
            )
