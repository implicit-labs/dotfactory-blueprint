"""Explicit setup and diagnostics for user-owned workers."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from . import worker
from .execution import call, remote_command, validate_policy
from .instance import FactoryConfig


def install(args):
    if args.config:
        config = FactoryConfig.load(args.config).values["execution"]["workers"][args.worker]
        destination = config["entrypoint"]
    else:
        if not args.target or not args.directory:
            raise ValueError("worker-install requires --config/--worker or --target/--directory")
        destination = str(Path(args.directory) / "worker.py")
        config = {"transport": "ssh", "target": args.target, "root": args.directory,
                  "entrypoint": destination, "billing": "subscription"}
    validate_policy({"workers": {"worker": config}, "stages": {"setup": {
        "workers": ["worker"], "scope": "portable", "requires": [], "checks": [],
    }}})
    content = Path(worker.__file__).read_bytes()
    # A fixed Python installer receives reviewed source on stdin; no credentials are copied.
    installer = (
        "import os,sys,tempfile; from pathlib import Path; "
        "p=Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); "
        "f=tempfile.NamedTemporaryFile(dir=p.parent,delete=False); "
        "f.write(sys.stdin.buffer.read()); f.close(); "
        "os.chmod(f.name,0o700); os.replace(f.name,p)"
    )
    result = subprocess.run(remote_command(config, ["python3", "-c", installer, destination]),
                            input=content, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError("worker installation failed")
    print(json.dumps({"entrypoint": destination, "sha256": hashlib.sha256(content).hexdigest()}))
    return 0


def check(args):
    config = FactoryConfig.load(args.config)
    policy = config.values.get("execution")
    if not policy:
        raise ValueError("config has no execution policy")
    from .configuration import resolve, load_override
    from .execution import worker_requirements, validate_report
    from .verification_host import inspect as inspect_coordinator
    override = load_override(getattr(args, "execution_config", None))
    project = getattr(args, "project", None)
    stage = getattr(args, "stage", None)
    if bool(project) != bool(stage) or (override is not None and not project):
        raise ValueError("worker-check scoped settings require --project and --stage")
    rule = {}
    coordinator = {}
    runner = args.runner
    if project:
        policy, _sources, graph = resolve(config, project, override)
        if stage not in policy["stages"]:
            raise ValueError("stage has no execution settings")
        rule = policy["stages"][stage]
        if args.worker not in rule["workers"]:
            raise ValueError("worker is not a candidate for the effective stage")
        node = next(state for state in graph.states if state["id"] == stage)
        expected_runner = node["execution"]["runner"]
        if runner is not None and runner != expected_runner:
            raise ValueError("runner differs from the resolved workflow stage")
        runner = expected_runner
        for state, configured in policy["stages"].items():
            if configured.get("coordinator"):
                coordinator[state] = inspect_coordinator(configured["coordinator"], execute=True)
        if any(not item["available"] for item in coordinator.values()):
            print(json.dumps({"available": False, "coordinator": coordinator}, indent=2))
            return 1
        repository = config.resolve_project(project)["repository_path"]
        requirements = sorted(worker_requirements(rule, repository) | set(args.require))
    else:
        if not runner:
            raise ValueError("worker-check requires --runner or a project/stage")
        requirements = args.require
    route = config.resolve_runners()[runner]
    selected = policy["workers"][args.worker]
    probes = rule.get("readiness", [])
    report = call(selected, {"op": "probe", "kind": route["kind"], "command": route["command"],
                             "billing": selected["billing"], "requires": requirements,
                             "environment_envs": route["environment_envs"], "readiness": probes},
                  timeout=45 + sum(item.get("timeout_seconds", 10) for item in probes))
    validate_report(report, probes, route["minimum_version"])
    print(json.dumps({"worker": args.worker, "billing": selected["billing"],
                      "coordinator": coordinator, **report}, indent=2))
    return 0 if report["available"] else 1


def add_commands(commands):
    setup = commands.add_parser("worker-install", help="install only the worker protocol over SSH")
    setup.add_argument("--target")
    setup.add_argument("--directory")
    setup.add_argument("--config")
    setup.add_argument("--worker")
    setup.set_defaults(callback=install)
    probe = commands.add_parser("worker-check", help="check worker tools and native authentication")
    probe.add_argument("--config", required=True)
    probe.add_argument("--worker", required=True)
    probe.add_argument("--runner")
    probe.add_argument("--project")
    probe.add_argument("--stage")
    probe.add_argument("--execution-config")
    probe.add_argument("--require", action="append", default=[])
    probe.set_defaults(callback=check)
