"""Coordinator verification requirements under the delivery isolation contract."""
from __future__ import annotations

import os
import platform
import re
import shutil
import sys
import tempfile

from . import worker


def environment(home):
    return {"PATH": os.defpath, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}


def validate(contract):
    if not isinstance(contract, dict) or set(contract) - {"requires", "python_min_version", "readiness"}:
        raise ValueError("coordinator accepts only requires, python_min_version and readiness")
    requirements = contract.get("requires", [])
    if not isinstance(requirements, list) or any(not isinstance(item, str) or not re.fullmatch(
            r"(?:os|arch|tool):[A-Za-z0-9_.+-]+", item) for item in requirements):
        raise ValueError("coordinator requires OS, architecture or tool requirements")
    version = contract.get("python_min_version")
    if version is not None and (not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version)):
        raise ValueError("coordinator python_min_version must be major.minor[.patch]")
    worker.validate_readiness(contract.get("readiness", []))


def inspect(contract, *, execute=False):
    validate(contract)
    facts = ["os:" + platform.system().lower(), "arch:" + platform.machine().lower()]
    missing = []
    for requirement in contract.get("requires", []):
        if requirement.startswith("tool:") and shutil.which(requirement[5:], path=os.defpath):
            facts.append(requirement)
        elif requirement not in facts:
            missing.append(requirement)
    version = contract.get("python_min_version")
    if version:
        minimum = tuple(int(part) for part in version.split('.'))
        if tuple(sys.version_info[:len(minimum)]) < minimum:
            missing.append("python>=" + version)
    probes = contract.get("readiness", [])
    if execute:
        with tempfile.TemporaryDirectory(prefix="dotfactory-host-") as home:
            # The same interpreter and environment as delivery; no ambient credentials.
            resolved = [{**probe, "command": [sys.executable if arg == "{python}" else arg
                                               for arg in probe["command"]]} for probe in probes]
            results = worker.readiness(resolved, environment(home))
        missing.extend("readiness:" + item["name"] + ":" + item["reason"]
                       for item in results if not item["passed"])
    else:
        results = [{"name": probe["name"], "status": "not_checked"} for probe in probes]
    return {"host": "coordinator", "interpreter": sys.executable,
            "python_version": platform.python_version(), "credentials": "unavailable",
            "path": os.defpath, "facts": sorted(facts), "missing": missing,
            "readiness": results, "available": not missing if execute or not probes else None}
