#!/usr/bin/env python3
"""Non-interactive Portless host preflight."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["CI"] = "1"
    return subprocess.run(
        command, check=False, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=environment,
    )


def version_number(value: str) -> str | None:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", value)
    return match.group(1) if match else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portless", default="portless")
    parser.add_argument("--version", default="0.15.6")
    parser.add_argument("--node-minimum", type=int, default=24)
    arguments = parser.parse_args()
    checks: dict[str, object] = {}

    portless_path = shutil.which(arguments.portless)
    checks["portless_installed"] = portless_path is not None
    if not portless_path:
        print(json.dumps({"ok": False, "checks": checks}, sort_keys=True))
        return 2
    observed = version_number(run([portless_path, "--version"]).stdout)
    checks["portless_version"] = observed
    checks["portless_version_matches"] = observed == arguments.version

    node_path = shutil.which("node")
    node_result = run([node_path, "--version"]) if node_path else None
    node_version = version_number(node_result.stdout) if node_result else None
    node_major = int(node_version.split(".")[0]) if node_version else None
    checks["node_major"] = node_major
    checks["node_supported"] = (
        node_major is not None and node_major >= arguments.node_minimum
    )

    doctor = run([portless_path, "doctor"])
    checks["doctor_healthy"] = doctor.returncode == 0
    ok = all((
        checks["portless_version_matches"], checks["node_supported"],
        checks["doctor_healthy"],
    ))
    print(json.dumps({"ok": ok, "checks": checks}, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
