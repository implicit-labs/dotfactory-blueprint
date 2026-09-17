"""Read-only local readiness checks for a factory configuration."""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .instance import FactoryConfig


@dataclass(frozen=True)
class Check:
    id: str
    scope: str
    status: str
    required: bool
    message: str
    remedy: str
    boundary: str


def _check(
    check_id: str,
    scope: str,
    status: str,
    required: bool,
    message: str,
    remedy: str,
    boundary: str,
) -> Check:
    return Check(check_id, scope, status, required, message, remedy, boundary)


def _git(repository: Path, arguments: Sequence[str]) -> Optional[str]:
    """Return stdout for a successful read-only Git query, otherwise None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _repository_path(
    config: FactoryConfig, project_key: str, environment: Mapping[str, str]
) -> Optional[Path]:
    project = config.values["projects"][project_key]
    if "repository_path" in project:
        value = str(project["repository_path"])
    else:
        value = environment.get(str(project["repository_path_env"]), "")
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config.path.parent / path
    return path.resolve()


def _project_checks(
    config: FactoryConfig, project_key: str, environment: Mapping[str, str]
) -> List[Check]:
    scope = "project:" + project_key
    repository = _repository_path(config, project_key, environment)
    top_level = None if repository is None else _git(
        repository, ("rev-parse", "--show-toplevel")
    )
    is_root = False
    if repository is not None and top_level:
        try:
            is_root = Path(top_level).resolve() == repository
        except OSError:
            is_root = False
    checks = [
        _check(
            scope + ":git-root",
            scope,
            "pass" if is_root else "fail",
            True,
            "The configured repository is an accessible Git root."
            if is_root
            else "The configured repository is not an accessible Git root.",
            "Set repository_path (or its environment variable) to the Git top-level directory.",
            "Only local filesystem and Git metadata were inspected.",
        )
    ]
    has_origin = repository is not None and _git(
        repository, ("remote", "get-url", "origin")
    ) is not None
    checks.append(
        _check(
            scope + ":origin",
            scope,
            "pass" if has_origin else "fail",
            True,
            "The repository has an origin remote."
            if has_origin
            else "The repository does not have an accessible origin remote.",
            "Add an origin remote for the configured repository.",
            "The remote URL was discarded and is never reported.",
        )
    )
    has_origin_main = repository is not None and _git(
        repository,
        ("rev-parse", "--verify", "refs/remotes/origin/main^{commit}"),
    ) is not None
    checks.append(
        _check(
            scope + ":origin-main",
            scope,
            "pass" if has_origin_main else "fail",
            True,
            "The origin/main commit is available locally."
            if has_origin_main
            else "The origin/main commit is not available locally.",
            "Fetch origin/main outside doctor, then run this check again.",
            "Doctor does not fetch or make any network request.",
        )
    )
    return checks


def _runner_checks(
    name: str, values: Mapping[str, Any], environment: Mapping[str, str]
) -> List[Check]:
    scope = "runner:" + name
    command = str(values["command"])
    try:
        executable = shlex.split(command)[0]
    except (ValueError, IndexError):
        executable = ""
    installed = bool(executable and shutil.which(executable))
    checks = [
        _check(
            scope + ":executable",
            scope,
            "pass" if installed else "fail",
            True,
            "The configured runner executable is installed."
            if installed
            else "The configured runner executable was not found.",
            "Install the configured runner executable or correct its command.",
            "The executable was located without launching it.",
        )
    ]
    for env_name in values.get("environment_envs", []):
        present = bool(environment.get(str(env_name)))
        checks.append(
            _check(
                scope + ":environment:" + str(env_name),
                scope,
                "pass" if present else "fail",
                True,
                "Required environment variable " + str(env_name) + " is present."
                if present
                else "Required environment variable " + str(env_name) + " is missing.",
                "Set " + str(env_name) + " in the runner environment.",
                "Only presence was checked; the value was not read or reported.",
            )
        )
    checks.append(
        _check(
            scope + ":authentication",
            scope,
            "not_checked",
            False,
            "Live authentication was not checked.",
            "Run the runner's normal authenticated preflight before delivery.",
            "No runner process or live authentication was attempted.",
        )
    )
    return checks


def _environment_check(
    integration: str, env_name: str, environment: Mapping[str, str]
) -> Check:
    scope = "integration:" + integration
    present = bool(environment.get(env_name))
    return _check(
        scope + ":environment:" + env_name,
        scope,
        "pass" if present else "fail",
        True,
        "Required environment variable " + env_name + " is present."
        if present
        else "Required environment variable " + env_name + " is missing.",
        "Set " + env_name + " for the enabled integration.",
        "Only presence was checked; the value and live authentication were not checked.",
    )


def _enabled_integration_checks(
    name: str, env_names: Sequence[str], environment: Mapping[str, str]
) -> List[Check]:
    scope = "integration:" + name
    checks = [_environment_check(name, item, environment) for item in env_names]
    checks.append(
        _check(
            scope + ":authentication",
            scope,
            "not_checked",
            False,
            "Live authentication was not checked.",
            "Run the integration's normal authenticated preflight before delivery.",
            "No credential store or remote service was accessed.",
        )
    )
    return checks


def _disabled_integration(name: str) -> Check:
    scope = "integration:" + name
    return _check(
        scope + ":enabled",
        scope,
        "skipped",
        False,
        "The integration is disabled; its credential checks were skipped.",
        "Enable the integration before requiring its environment variables.",
        "Disabled integrations do not affect readiness.",
    )


def _integration_checks(
    config: FactoryConfig, environment: Mapping[str, str]
) -> List[Check]:
    projections = config.values.get("projections", {})
    checks: List[Check] = []
    linear = projections.get("linear")
    if not linear or not linear.get("enabled"):
        checks.append(_disabled_integration("linear"))
    else:
        names = [str(linear["token_env"])]
        checks.extend(_enabled_integration_checks("linear", names, environment))
        if linear.get("webhook_secret_env"):
            checks.append(_check(
                "integration:linear:webhook", "integration:linear", "not_checked", False,
                "Webhook readiness was not checked; polling does not require a webhook secret.",
                "Configure and validate the webhook secret separately before accepting webhooks.",
                "Doctor checks local polling prerequisites, not webhook delivery or signature validation.",
            ))

    logfire = projections.get("logfire")
    if not logfire or not logfire.get("enabled"):
        checks.append(_disabled_integration("logfire"))
    else:
        names = [
            str(logfire.get("endpoint_env", "OTEL_EXPORTER_OTLP_ENDPOINT")),
            str(logfire.get("headers_env", "OTEL_EXPORTER_OTLP_HEADERS")),
        ]
        checks.extend(_enabled_integration_checks("logfire", names, environment))

    if not logfire or not logfire.get("dataset_enabled"):
        checks.append(_disabled_integration("hosted-dataset"))
    else:
        checks.extend(
            _enabled_integration_checks(
                "hosted-dataset", [str(logfire["dataset_api_key_env"])], environment
            )
        )
    return checks


def _result(checks: Sequence[Check]) -> Dict[str, Any]:
    ordered = sorted(checks, key=lambda item: item.id)
    summary = {
        "passed": sum(item.status == "pass" for item in ordered),
        "failed": sum(item.status == "fail" for item in ordered),
        "skipped": sum(item.status == "skipped" for item in ordered),
        "not_checked": sum(item.status == "not_checked" for item in ordered),
    }
    failed = any(item.required and item.status == "fail" for item in ordered)
    return {
        "schema_version": 1,
        "command": "doctor",
        "status": "fail" if failed else "pass",
        "summary": summary,
        "checks": [asdict(item) for item in ordered],
    }


def inspect(config_path: str, environment: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Inspect local prerequisites without constructing runtime state."""
    environment = os.environ if environment is None else environment
    try:
        config = FactoryConfig.load(config_path)
    except (OSError, ValueError, TypeError):
        return _result(
            [
                _check(
                    "config",
                    "config",
                    "fail",
                    True,
                    "The factory configuration could not be loaded and validated.",
                    "Confirm the path is readable and contains a valid factory configuration.",
                    "Configuration contents and exception details were not reported.",
                )
            ]
        )
    checks = [
        _check(
            "config",
            "config",
            "pass",
            True,
            "The factory configuration loaded and validated.",
            "No configuration remedy is required.",
            "Validation used FactoryConfig.load without creating runtime state.",
        )
    ]
    for project_key in config.project_keys:
        checks.extend(_project_checks(config, project_key, environment))
    for name, values in config.values.get("runners", {}).items():
        checks.extend(_runner_checks(str(name), values, environment))
    checks.extend(_integration_checks(config, environment))
    return _result(checks)


def render_text(result: Mapping[str, Any]) -> str:
    labels = {
        "pass": "PASS",
        "fail": "FAIL",
        "skipped": "SKIPPED",
        "not_checked": "NOT CHECKED",
    }
    lines = ["dotfactory doctor: " + str(result["status"]).upper()]
    for item in result["checks"]:
        lines.extend(
            [
                "[" + labels[str(item["status"])] + "] " + str(item["id"]) + ": " + str(item["message"]),
                "  Remedy: " + str(item["remedy"]),
                "  Boundary: " + str(item["boundary"]),
            ]
        )
    return "\n".join(lines) + "\n"


def run(config_path: str, json_output: bool = False) -> int:
    result = inspect(config_path)
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render_text(result), end="")
    return 0 if result["status"] == "pass" else 1
