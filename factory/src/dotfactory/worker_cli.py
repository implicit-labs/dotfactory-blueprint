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
    route = config.resolve_runners()[args.runner]
    selected = policy["workers"][args.worker]
    report = call(selected, {"op": "probe", "kind": route["kind"], "command": route["command"],
                             "billing": selected["billing"], "requires": args.require,
                             "environment_envs": route["environment_envs"]})
    print(json.dumps({"worker": args.worker, "billing": selected["billing"], **report}, indent=2))
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
    probe.add_argument("--runner", required=True)
    probe.add_argument("--require", action="append", default=[])
    probe.set_defaults(callback=check)
