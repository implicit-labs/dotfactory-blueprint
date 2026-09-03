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


def doctor_summary(value: str) -> tuple[int, int] | None:
    match = re.search(
        r"Summary:\s+(\d+)\s+failures?,\s+(\d+)\s+warnings?", value,
        re.IGNORECASE,
    )
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def emit(checks: dict[str, object], remediation: list[str]) -> int:
    ok = not remediation
    print(json.dumps({
        "ok": ok, "checks": checks, "remediation": remediation,
    }, sort_keys=True))
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portless", default="portless")
    parser.add_argument("--version", default="0.15.6")
    parser.add_argument("--node-minimum", type=int, default=24)
    parser.add_argument("--node-only", action="store_true")
    arguments = parser.parse_args()
    checks: dict[str, object] = {}

    node_path = shutil.which("node")
    node_result = run([node_path, "--version"]) if node_path else None
    node_version = version_number(
        (node_result.stdout + node_result.stderr) if node_result else ""
    )
    node_major = int(node_version.split(".")[0]) if node_version else None
    checks["node_major"] = node_major
    checks["node_supported"] = (
        node_major is not None and node_major >= arguments.node_minimum
    )
    if not checks["node_supported"]:
        return emit(checks, [
            f"Put Node {arguments.node_minimum} or newer first on PATH.",
        ])
    if arguments.node_only:
        return emit(checks, [])

    portless_path = shutil.which(arguments.portless)
    checks["portless_installed"] = portless_path is not None
    if not portless_path:
        return emit(checks, [
            f"Install Portless {arguments.version} with the active Node runtime.",
        ])
    version_result = run([portless_path, "--version"])
    observed = version_number(version_result.stdout + version_result.stderr)
    checks["portless_version"] = observed
    checks["portless_version_matches"] = observed == arguments.version
    if not checks["portless_version_matches"]:
        return emit(checks, [
            f"Install the pinned Portless {arguments.version} package.",
        ])

    doctor = run([portless_path, "doctor"])
    summary = doctor_summary(doctor.stdout + doctor.stderr)
    checks["doctor_exit_ok"] = doctor.returncode == 0
    checks["doctor_summary_found"] = summary is not None
    checks["doctor_failures"] = summary[0] if summary else None
    checks["doctor_warnings"] = summary[1] if summary else None
    checks["doctor_healthy"] = (
        doctor.returncode == 0 and summary == (0, 0)
    )
    if not checks["doctor_healthy"]:
        return emit(checks, [
            "Run `portless trust` once if the local CA is untrusted.",
            "Run `portless service install` once if the proxy is not running.",
            "Re-run `portless doctor`, then this preflight.",
        ])
    return emit(checks, [])


if __name__ == "__main__":
    sys.exit(main())
