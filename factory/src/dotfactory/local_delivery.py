"""Initialize the supported local lane without changing a project checkout."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .instance import FactoryConfig


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments], check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
    )
    if result.returncode:
        raise ValueError("repository prerequisite failed: " + " ".join(arguments[:2]))
    return result.stdout.strip()


def initialize(
    *, repository: str, output: str, project: str, linear_project: str,
    logfire: bool = False,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", project):
        raise ValueError("project must be a lowercase slug (1–63 characters)")
    if not linear_project.strip():
        raise ValueError("linear-project must identify the owning Linear project")
    source = Path(repository).expanduser().resolve(strict=True)
    destination = Path(output).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("output already exists; choose a new instance directory")
    if not destination.parent.is_dir():
        raise ValueError("output parent directory must already exist")
    destination = destination.parent.resolve() / destination.name
    top = Path(_git(source, "rev-parse", "--show-toplevel")).resolve()
    if top != source:
        raise ValueError("repository must be the Git checkout root")
    _git(source, "remote", "get-url", "origin")
    base = _git(source, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
    if destination == source or source in destination.parents:
        # State and owned workspaces must not become accidental source inputs.
        _git(source, "check-ignore", "--", str(destination / "workspaces"))

    factory = Path(__file__).resolve().parents[2]
    values = json.loads((factory / "factory.example.json").read_text(encoding="utf-8"))
    values.update(
        factory_id=project + "-local", ledger_path=str(destination / "factory.db"),
        default_workflow="verified-python",
    )
    lane = values["workflows"]["verified-python"]
    lane["path"] = str(factory / "workflows" / "verified-python.dot")
    values["workflows"] = {"verified-python": lane}
    values["runners"] = {"codex": values["runners"]["codex"]}
    values["scheduler"]["limits"] = {
        "host": 1, "projects": {project: 1}, "runners": {"codex": 1},
    }
    values["preparation"]["workspace"].update(
        root=str(destination / "workspaces"), retention="explicit",
    )
    values["preparation"]["capabilities"] = {}
    values["projects"] = {project: {
        "display_name": project, "enabled_by_default": True,
        "workflow": "verified-python", "repository_path": str(source),
        "tracker": {"kind": "linear", "project_id": linear_project},
    }}
    values["projections"]["linear"]["enabled"] = False
    values["projections"]["logfire"]["enabled"] = logfire
    payload = json.dumps(values, indent=2) + "\n"
    # Validate before creating anything at the requested destination. No runner,
    # verifier, remote fetch, ledger, socket, or credential resolution at init.
    with tempfile.TemporaryDirectory(prefix="dotfactory-config-") as temporary:
        candidate = Path(temporary) / "factory.json"
        candidate.write_text(payload, encoding="utf-8")
        config = FactoryConfig.load(candidate)
        config.resolve_workflow(project)
    destination.mkdir(mode=0o700)  # exclusive: concurrent init never overwrites
    path = destination / "factory.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
    command = [sys.executable, "-m", "dotfactory", "run", "--config", str(path),
               "--project", project, "--issue", "ISSUE-ID", "--description-file",
               "TASK.md", "--until-state", "Review"]
    return {
        "config": str(path), "project": project, "workflow": "verified-python",
        "repository": str(source), "checked_base_sha": base,
        "runner": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium",
        "linear_sync": "disabled", "logfire": "enabled" if logfire else "disabled",
        "next": shlex.join(command), "next_argv": command,
        "not_checked": ["live runner authentication", "verification OS permissions",
                        "Logfire credentials/delivery", "fresh remote main (fetched at run)"],
    }
