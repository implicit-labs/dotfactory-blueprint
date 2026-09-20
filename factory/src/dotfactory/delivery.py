"""Host-checked delivery receipts for the first, deliberately narrow Python lane."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .runner import RunnerResult

PLANNED_CONTRACTS = {"plan-result-v2", "implementation-result-v2", "python-verification-v2"}
CONTRACTS = {"plan-result-v1", "implementation-result-v1", "python-verification-v1"} | PLANNED_CONTRACTS
FAILURE_LABELS = {"failed", "retry", "exhausted", "blocked", "cancel", "duplicate"}
MAX_BYTES = 4 * 1024 * 1024
VERIFICATION_POLICY_SCHEMA_VERSION = 1
VERIFICATION_TIMEOUT_SECONDS = (60, 120)
LEGACY_VERIFICATION_TIMEOUT_SECONDS = 60
VERIFICATION_DEFINITION_KEYS = {"schema_version", "verification_policy", "criteria"}


class DeliveryError(ValueError):
    pass


def resolve_verification_policy(definition: dict[str, Any]) -> dict[str, int]:
    """Resolve the bounded host policy selected by a schema-version-1 plan."""
    if not isinstance(definition, dict) or definition.get("schema_version") != 1:
        raise DeliveryError("verification policy requires schema_version 1")
    unknown = set(definition) - VERIFICATION_DEFINITION_KEYS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise DeliveryError(f"verification definition has unknown top-level fields: {names}")
    if "verification_policy" not in definition:
        timeout = LEGACY_VERIFICATION_TIMEOUT_SECONDS
    else:
        policy = definition["verification_policy"]
        if not isinstance(policy, dict) or set(policy) != {"timeout_seconds"}:
            raise DeliveryError("verification_policy accepts only timeout_seconds")
        timeout = policy["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise DeliveryError("verification timeout_seconds must be an integer")
        if timeout not in VERIFICATION_TIMEOUT_SECONDS:
            raise DeliveryError("verification timeout_seconds must be 60 or 120")
    return {
        "schema_version": VERIFICATION_POLICY_SCHEMA_VERSION,
        "timeout_seconds": timeout,
    }


def verification_policy_guidance() -> str:
    """Render planning guidance from the constants enforced by the host."""
    allowed = " or ".join(str(value) for value in VERIFICATION_TIMEOUT_SECONDS)
    example = json.dumps({
        "schema_version": VERIFICATION_POLICY_SCHEMA_VERSION,
        "verification_policy": {"timeout_seconds": VERIFICATION_TIMEOUT_SECONDS[-1]},
        "criteria": [{
            "id": "required-check",
            "requirement": "Describe required behavior",
            "kind": "automated",
            "files": [".factory/verify.py", "tests/test_required.py"],
        }],
    }, separators=(",", ":"))
    return (
        "Authoritative host verification contract:\n"
        f"- Python runs as sys.executable ({sys.executable}, Python {platform.python_version()}) "
        "with isolated mode (-I); Python children must use sys.executable, not PATH discovery.\n"
        f"- The executable environment uses PATH={os.defpath}, an isolated temporary HOME, "
        "no ambient secret or credential variables, and no stdin.\n"
        "- Verification receives an isolated raw-Git export of committed source; ignored and "
        "untracked files are excluded, and committed-source mutation is rejected.\n"
        f"- Exact schema shape for an explicit deadline: {example}\n"
        f"- verification_policy.timeout_seconds may be {allowed}; omitting verification_policy "
        f"retains the legacy {LEGACY_VERIFICATION_TIMEOUT_SECONDS}-second deadline. "
        "timeout_seconds at the top level, unknown top-level fields, unknown policy keys, and "
        "other values are rejected.\n"
        "- Output and committed source are each bounded to 4 MiB. Operator cancel and deadline "
        "expiry terminate the verifier's owned process group."
    )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True).encode()


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=15)
    if result.returncode:
        raise DeliveryError("cannot inspect delivery Git state")
    if len(result.stdout) > MAX_BYTES:
        raise DeliveryError("delivery exceeds the 4 MiB inspection limit")
    return result.stdout


def local_file(root: Path, uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.query or parsed.fragment or parsed.netloc or parsed.scheme not in ("", "file"):
        raise DeliveryError("delivery evidence must reference a local workspace file")
    value = Path(unquote(parsed.path))
    candidate = value if value.is_absolute() else root / value
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise DeliveryError("evidence is outside the workspace") from error
    if not relative.parts or any(part in ("..", ".git") for part in relative.parts):
        raise DeliveryError("unsafe evidence path")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise DeliveryError("symlink evidence is not accepted")
    if not candidate.is_file() or not 0 < candidate.stat().st_size <= MAX_BYTES:
        raise DeliveryError("evidence file is missing, empty, or oversized")
    return candidate


def source_snapshot(workspace: dict[str, Any]) -> dict[str, Any]:
    root = Path(workspace["path"]).resolve()
    if git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise DeliveryError("commit all delivery files before proposing completion")
    head = git(root, "rev-parse", "HEAD").decode().strip()
    base = str(workspace["base_sha"])
    git(root, "merge-base", "--is-ancestor", base, head)
    patch = git(root, "diff", "--binary", base, head, "--")
    names = git(root, "diff", "--name-only", "-z", base, head, "--").decode().split("\0")
    return {"base_sha": base, "head_sha": head, "patch_sha256": digest(patch),
            "changed_files": [name for name in names if name]}


def _evidence(root: Path, evidence: tuple[dict[str, Any], ...]) -> list[dict[str, str]]:
    if not evidence or len(evidence) > 32:
        raise DeliveryError("provide between 1 and 32 local evidence files")
    files = []
    for item in evidence:
        path = local_file(root, str(item.get("uri", "")))
        relative = path.relative_to(root).as_posix()
        # Untracked/ignored files cannot be bound by the source revision.
        git(root, "ls-files", "--error-unmatch", "--", relative)
        files.append({"kind": str(item.get("kind", "")), "path": relative,
                      "sha256": digest(path.read_bytes())})
    return files


def _planned_check_path(name: Any) -> bool:
    """Canonical Git-relative Python check, not a URI or normalized alias."""
    if not isinstance(name, str) or not name.endswith(".py"):
        return False
    if any(character in name for character in ("\\", "%", "?", "#", ":", "\x00")):
        return False
    parts = name.split("/")
    if any(part in ("", ".", "..", ".git") for part in parts):
        return False
    return len(parts) > 1 and (parts[0] == ".factory" or "tests" in parts[:-1])


def verification_definition(root: Path) -> dict[str, Any]:
    """Validate proposed acceptance coverage without executing unapproved code."""
    import ast
    path = local_file(root, ".factory/verification.json")
    if path.stat().st_size > 65536:
        raise DeliveryError("verification definition exceeds 64 KiB")
    definition = json.loads(path.read_text())
    criteria = definition.get("criteria") if isinstance(definition, dict) else None
    if not isinstance(definition, dict) or definition.get("schema_version") != 1 or not isinstance(criteria, list) or not 1 <= len(criteria) <= 32:
        raise DeliveryError("verification definition requires schema_version 1 and 1-32 criteria")
    files = {".factory/plan.md", ".factory/verification.json", ".factory/verify.py"}
    ids = set()
    automated = False
    for criterion in criteria:
        if not isinstance(criterion, dict):
            raise DeliveryError("criterion must be an object")
        identifier = criterion.get("id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in ids:
            raise DeliveryError("criteria require unique nonempty IDs")
        ids.add(identifier)
        if not isinstance(criterion.get("requirement"), str) or not criterion["requirement"].strip():
            raise DeliveryError("criterion requires its acceptance requirement")
        if criterion.get("kind") == "automated":
            paths = criterion.get("files")
            if not isinstance(paths, list) or not paths or len(paths) > 32:
                raise DeliveryError("automated criterion requires test files")
            for name in paths:
                if not _planned_check_path(name):
                    raise DeliveryError("planned checks must be canonical repository-relative Python files under a tests/ directory or root .factory/")
                files.add(name)
            automated = True
        elif criterion.get("kind") == "manual":
            if not isinstance(criterion.get("procedure"), str) or not criterion["procedure"].strip():
                raise DeliveryError("manual criterion requires review steps")
        else:
            raise DeliveryError("criterion kind must be automated or manual")
    if not automated or len(files) > 64:
        raise DeliveryError("plan requires automated checks and at most 64 frozen files")
    hashes = {}
    for name in sorted(files):
        file = local_file(root, name)
        git(root, "ls-files", "--error-unmatch", "--", name)
        if name.endswith(".py"):
            try:
                ast.parse(file.read_text())
            except SyntaxError as error:
                raise DeliveryError(f"invalid planned Python check: {name}") from error
        hashes[name] = digest(file.read_bytes())
    return {"definition": definition, "policy": resolve_verification_policy(definition),
            "files": hashes}


def planned_receipt(ledger: Any, execution_id: str) -> dict[str, Any]:
    for row in ledger.connection.execute(
        "SELECT seq,payload_json FROM events WHERE execution_id=? AND event_type='delivery_checked' ORDER BY seq DESC",
        (execution_id,),
    ):
        receipt = json.loads(row["payload_json"])
        if receipt["contract"] == "plan-result-v2":
            if not receipt["passed"]:
                raise DeliveryError("latest verification plan did not pass its structural check")
            return {"seq": row["seq"], "receipt": receipt}
    raise DeliveryError("planning must define checks before implementation")


def approved_plan(ledger: Any, execution_id: str) -> dict[str, Any]:
    plan = planned_receipt(ledger, execution_id)
    decision = ledger.connection.execute(
        "SELECT id,event_seq,from_state FROM transition_decisions WHERE execution_id=? AND "
        "event_seq>? AND (((from_state='PlanReview' AND actor='human') "
        "OR (from_state='Autoplanning' AND actor='agent')) AND to_state='Ready' "
        "OR (from_state='ReplanReview' AND actor='human' AND to_state='Verifying')) "
        "ORDER BY event_seq DESC LIMIT 1",
        (execution_id, plan["seq"]),
    ).fetchone()
    if not decision:
        raise DeliveryError("verification plan requires human approval or validated Autoplanning before implementation")
    receipt = plan["receipt"]
    if decision["from_state"] == "Autoplanning":
        event = ledger.connection.execute("SELECT payload_json FROM events WHERE seq=?", (decision["event_seq"],)).fetchone()
        payload = json.loads(event["payload_json"])
        if (payload.get("completed_attempt") or {}).get("attempt_id") != receipt["attempt_id"]:
            raise DeliveryError("automatic plan authorization must match the validated planning attempt")
    workspace = ledger.workspace_for_execution(execution_id)
    root = Path(workspace["path"]).resolve()
    git(root, "merge-base", "--is-ancestor", receipt["source"]["head_sha"], "HEAD")
    for name, expected in receipt["verification_plan"]["files"].items():
        if digest(local_file(root, name).read_bytes()) != expected:
            raise DeliveryError(f"approved verification file changed: {name}")
    replacement = receipt.get("replan") or {}
    return {"decision_id": decision["id"], "plan_attempt_id": receipt["attempt_id"],
            "plan_receipt_seq": plan["seq"], "head_sha": receipt["source"]["head_sha"],
            "implementation_base_sha": replacement.get(
                "implementation_base_sha", receipt["source"]["head_sha"]
            ), **receipt["verification_plan"]}


def replan_context(ledger: Any, execution_id: str) -> dict[str, str]:
    """Bind replanning to the exact failed frozen-verifier source revision."""
    approved = approved_plan(ledger, execution_id)
    row = ledger.connection.execute(
        "SELECT seq,payload_json FROM events WHERE execution_id=? "
        "AND event_type='delivery_checked' AND seq>? ORDER BY seq DESC",
        (execution_id, approved["plan_receipt_seq"]),
    ).fetchone()
    if not row:
        raise DeliveryError("replan requires a failed frozen verification receipt")
    receipt = json.loads(row["payload_json"])
    verification = receipt.get("verification") or {}
    if (
        receipt.get("contract") != "python-verification-v2"
        or receipt.get("passed") is not False
        or receipt.get("error") != "pinned verification failed"
        or verification.get("exit_code") in (None, 0)
    ):
        raise DeliveryError("replan requires a failed frozen verification receipt")
    workspace = ledger.workspace_for_execution(execution_id)
    if not workspace:
        raise DeliveryError("replan requires workspace provenance")
    current = source_snapshot(workspace)
    failed = receipt.get("source") or {}
    if current["head_sha"] != failed.get("head_sha"):
        raise DeliveryError(
            "source changed after frozen verification failed; restore or cancel before replanning"
        )
    return {
        "baseline_head_sha": current["head_sha"],
        "replaces_plan_attempt_id": approved["plan_attempt_id"],
        "implementation_base_sha": approved["implementation_base_sha"],
    }


def _recorded_replan_context(ledger: Any, execution_id: str) -> dict[str, str]:
    row = ledger.connection.execute(
        "SELECT e.payload_json FROM transition_decisions td JOIN events e "
        "ON e.seq=td.event_seq WHERE td.execution_id=? "
        "AND td.from_state='Blocked' AND td.to_state='Replanning' "
        "ORDER BY td.event_seq DESC LIMIT 1",
        (execution_id,),
    ).fetchone()
    payload = json.loads(row["payload_json"]) if row else {}
    evidence = [
        item for item in payload.get("evidence", [])
        if item.get("kind") == "replan_baseline"
    ]
    if len(evidence) != 1:
        raise DeliveryError("replanning requires one recorded source baseline")
    item = evidence[0]
    uri = str(item.get("uri", ""))
    context = {
        "baseline_head_sha": uri.removeprefix("git:"),
        "replaces_plan_attempt_id": str(item.get("replaces_plan_attempt_id", "")),
        "implementation_base_sha": str(item.get("implementation_base_sha", "")),
    }
    if (
        not uri.startswith("git:")
        or any(not value for value in context.values())
    ):
        raise DeliveryError("recorded replanning baseline is incomplete")
    return context


def guard_planned_transition(ledger: Any, execution_id: str, states: dict[str, Any],
                             from_state: str, to_state: str, feedback: list[dict[str, Any]] | None = None) -> None:
    if not any(state.get("execution", {}).get("exit_contract") in PLANNED_CONTRACTS for state in states.values()):
        return
    if from_state == "Autoplanning" and to_state == "Ready":
        plan = planned_receipt(ledger, execution_id)["receipt"]
        require_receipt(ledger, plan["attempt_id"], "plan-result-v2")
    if (
        (from_state == "PlanReview" and to_state == "Ready")
        or (from_state == "ReplanReview" and to_state == "Verifying")
    ):
        plan = planned_receipt(ledger, execution_id)["receipt"]
        require_receipt(ledger, plan["attempt_id"], "plan-result-v2")
        if not any(plan["source"]["head_sha"] in str(item.get("body", "")) for item in feedback or []):
            raise DeliveryError("approval feedback must name the exact reviewed plan commit")
    if (
        states[to_state].get("execution", {}).get("exit_contract")
        in ("implementation-result-v2", "python-verification-v2")
        and not (from_state == "ReplanReview" and to_state == "Verifying")
    ):
        plan = approved_plan(ledger, execution_id)
        if from_state == "Ready":
            workspace = ledger.workspace_for_execution(execution_id)
            if source_snapshot(workspace)["head_sha"] != plan["head_sha"]:
                raise DeliveryError("source changed between plan approval and implementation")


def _verify(
    root: Path,
    base: str,
    progress: Callable[[], bool] | None,
    *,
    policy: dict[str, int] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    selected = resolve_verification_policy({
        "schema_version": 1,
        **({"verification_policy": {"timeout_seconds": policy["timeout_seconds"]}}
           if policy is not None else {}),
    })
    if policy is not None and policy != selected:
        raise DeliveryError("verification policy is not canonical")
    timeout_seconds = selected["timeout_seconds"]
    script = git(root, "show", f"{base}:.factory/verify.py")
    if not script.strip():
        raise DeliveryError("verification revision requires .factory/verify.py")
    if git(root, "show", "HEAD:.factory/verify.py") != script:
        raise DeliveryError("delivery cannot change its pinned verification script")
    with tempfile.TemporaryDirectory(prefix="dotfactory-check-") as directory:
        temp = Path(directory)
        checkout = temp / "source"
        checkout.mkdir()
        inventory = {}
        size = 0
        entries = git(root, "ls-tree", "-rz", "--full-tree", "HEAD").split(b"\0")
        if len(entries) > 2001:
            raise DeliveryError("Python delivery exceeds 2000 committed files")
        for entry in entries:
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            relative = Path(os.fsdecode(name))
            if (mode not in (b"100644", b"100755") or kind != b"blob"
                    or relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts):
                raise DeliveryError("Python delivery accepts only regular committed files")
            data = git(root, "cat-file", "blob", oid.decode("ascii"))
            size += len(data)
            if size > MAX_BYTES:
                raise DeliveryError("committed source exceeds 4 MiB")
            destination = checkout / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o700 if mode == b"100755" else 0o600)
            inventory[relative] = digest(data)
        pinned = temp / "verify.py"
        pinned.write_bytes(script)
        output = temp / "check.log"
        from .verification_host import environment as verification_environment
        environment = verification_environment(temp)
        with output.open("wb") as stream:
            process = subprocess.Popen(
                [sys.executable, "-I", str(pinned), str(checkout)], cwd=temp,
                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
            deadline = monotonic() + timeout_seconds
            try:
                while process.poll() is None:
                    if progress and progress():
                        raise DeliveryError("verification canceled by operator")
                    if monotonic() >= deadline:
                        raise DeliveryError(
                            f"verification exceeded {timeout_seconds} seconds"
                        )
                    if output.stat().st_size > MAX_BYTES:
                        raise DeliveryError("verification output exceeded 4 MiB")
                    sleep(0.05)
            finally:
                # This process group was created by this invocation only.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        for relative, expected in inventory.items():
            path = checkout / relative
            if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != expected:
                raise DeliveryError("verification mutated committed source in its export")
        data = output.read_bytes()
        if len(data) > MAX_BYTES:
            raise DeliveryError("verification output exceeded 4 MiB")
        return {"script_sha256": digest(script), "exit_code": process.returncode,
                "output_sha256": digest(data), "output": data[-16384:].decode("utf-8", errors="replace"),
                "output_truncated": len(data) > 16384,
                "input": "isolated export of committed HEAD; ignored files excluded",
                "policy": {
                    "schema_version": selected["schema_version"],
                    "requested_timeout_seconds": timeout_seconds,
                    "effective_timeout_seconds": timeout_seconds,
                    "interpreter": sys.executable,
                    "python_version": platform.python_version(),
                    "isolated": True,
                    "path": environment["PATH"],
                    "home": environment["HOME"],
                    "home_isolated": True,
                    "source_export": "raw committed Git objects",
                    "ambient_secrets_excluded": True,
                }}


def _failed_result(receipt: dict[str, Any], uri: str) -> RunnerResult:
    outcome = "Host delivery check failed: " + str(receipt.get("error", "unknown error"))
    output = receipt.get("verification", {}).get("output", "")
    if output:
        outcome += "\nVerification output: " + output[-3000:]
    return RunnerResult(outcome[:4096], "failed", ({"kind": "delivery_check", "uri": uri},))


def evaluate(ledger: Any, launch: Any, result: RunnerResult,
             *, progress: Callable[[], bool] | None = None) -> RunnerResult:
    contract = launch.request.config.get("exit_contract")
    if not contract:
        return result
    if contract not in CONTRACTS:
        raise DeliveryError(f"unsupported exit contract: {contract}")
    if result.preferred_label in FAILURE_LABELS:
        return result
    request = launch.request
    ledger.assert_attempt_active(request.attempt_id, request.fence_token)
    existing = ledger.event_for_command(f"attempt:{request.attempt_id}:delivery-check:v1")
    if existing:
        if existing["payload"]["contract"] != contract:
            raise DeliveryError("saved delivery check belongs to a different contract")
        uri = f"ledger://delivery-checks/{request.attempt_id}"
        if not existing["payload"]["passed"]:
            return _failed_result(existing["payload"], uri)
        require_receipt(ledger, request.attempt_id, str(contract))
        return RunnerResult(result.outcome, result.preferred_label,
                            (*result.evidence, {"kind": "delivery_check", "uri": uri}))
    receipt = {"schema_version": 1, "contract": contract, "attempt_id": request.attempt_id,
               "workflow_digest": request.workflow_digest, "passed": False,
               "limitations": ["Checks prove only the pinned verification script's assertions.",
                               "Project code is trusted; this is not an OS sandbox."]}
    workspace = ledger.workspace_for_execution(request.execution_id)
    try:
        if result.preferred_label != "complete":
            raise DeliveryError("delivery contract requires complete or an explicit failure label")
        if not workspace or workspace["path"] != launch.workspace_path:
            raise DeliveryError("prepared workspace provenance is missing")
        root = Path(workspace["path"]).resolve()
        source = source_snapshot(workspace)
        receipt["source"] = source
        receipt["files"] = _evidence(root, result.evidence)
        plan = local_file(root, ".factory/plan.md")
        git(root, "ls-files", "--error-unmatch", "--", ".factory/plan.md")
        receipt["plan_sha256"] = digest(plan.read_bytes())
        if contract == "plan-result-v2":
            receipt["verification_plan"] = verification_definition(root)
            if request.state_id == "Replanning":
                context = _recorded_replan_context(ledger, request.execution_id)
                git(root, "merge-base", "--is-ancestor", context["baseline_head_sha"], source["head_sha"])
                changes = git(
                    root, "diff", "--name-only", "-z",
                    context["baseline_head_sha"], source["head_sha"], "--",
                ).decode().split("\0")
                if any(
                    name and name not in receipt["verification_plan"]["files"]
                    for name in changes
                ):
                    raise DeliveryError(
                        "replanning may change only its plan and declared verification files"
                    )
                receipt["replan"] = context
            elif any(
                name not in receipt["verification_plan"]["files"]
                for name in source["changed_files"]
            ):
                raise DeliveryError("planning may change only its plan and declared verification files")
        approved = None
        if contract in ("implementation-result-v2", "python-verification-v2"):
            approved = approved_plan(ledger, request.execution_id)
            receipt["approved_plan"] = approved
        if contract == "plan-result-v1" and any(
            name != ".factory/plan.md" for name in source["changed_files"]
        ):
            raise DeliveryError("planning may change only .factory/plan.md")
        if contract not in ("plan-result-v1", "plan-result-v2"):
            changes = source["changed_files"]
            if approved:
                changes = git(
                    root, "diff", "--name-only", "-z",
                    approved.get("implementation_base_sha", approved["head_sha"]), "HEAD",
                ).decode().split("\0")
            if not any(name and not name.startswith(".factory/") for name in changes):
                raise DeliveryError("delivery has no change outside its planning/evidence files")
            report = local_file(root, ".factory/delivery.json")
            if report.stat().st_size > 65536:
                raise DeliveryError("delivery.json exceeds 64 KiB")
            git(root, "ls-files", "--error-unmatch", "--", ".factory/delivery.json")
            details = json.loads(report.read_text())
            if not isinstance(details, dict) or not isinstance(details.get("summary"), str) or not details["summary"].strip():
                raise DeliveryError("delivery.json requires a nonempty summary")
            if not isinstance(details.get("limitations"), list) or any(not isinstance(x, str) for x in details["limitations"]):
                raise DeliveryError("delivery.json requires a limitations string list")
            receipt["delivery"] = details
            if contract in ("python-verification-v1", "python-verification-v2"):
                # Pre-policy plan-result-v2 receipts are immutable and omit this key.
                # Passing None preserves their historical 60-second verifier budget.
                policy = approved.get("policy") if approved else None
                from .execution import settings_view
                from .verification_host import inspect as inspect_coordinator
                settings = settings_view(ledger, request.execution_id)
                host = settings["stages"].get(request.state_id, {}).get("coordinator") if settings else None
                if host:
                    receipt["coordinator"] = inspect_coordinator(host, execute=True)
                    if not receipt["coordinator"]["available"]:
                        raise DeliveryError("coordinator verification prerequisites unavailable")
                receipt["verification"] = _verify(
                    root,
                    approved["head_sha"] if approved else source["base_sha"],
                    progress,
                    policy=policy,
                )
                if receipt["verification"]["exit_code"] != 0:
                    receipt["failure"] = {
                        "category": "frozen_verification_failed",
                        "recovery": "investigate_then_request_confirmed_replan_if_harness_is_unusable",
                    }
                    raise DeliveryError("pinned verification failed")
        if source_snapshot(workspace) != source:
            raise DeliveryError("verification changed the source being checked")
        receipt["passed"] = True
    except (DeliveryError, OSError, ValueError, subprocess.SubprocessError) as error:
        receipt["error"] = str(error)
    ledger.assert_attempt_active(request.attempt_id, request.fence_token)
    # The ledger owns the result; the provider cannot supply a receipt URI to pass.
    receipt = ledger.record_delivery_check(request.attempt_id, request.fence_token, receipt)["payload"]
    uri = f"ledger://delivery-checks/{request.attempt_id}"
    if not receipt["passed"]:
        return _failed_result(receipt, uri)
    return RunnerResult(result.outcome, result.preferred_label,
                        (*result.evidence, {"kind": "delivery_check", "uri": uri}))


def require_receipt(ledger: Any, attempt_id: str, contract: str) -> None:
    if contract not in CONTRACTS:
        raise DeliveryError(f"unsupported exit contract: {contract}")
    record = ledger.event_for_command(f"attempt:{attempt_id}:delivery-check:v1")
    if not record or not record["payload"].get("passed"):
        raise DeliveryError("completion requires a successful host delivery check")
    receipt = record["payload"]
    if receipt["contract"] != contract:
        raise DeliveryError("delivery check belongs to a different contract")
    workspace = ledger.workspace_for_execution(record["execution_id"])
    if not workspace or source_snapshot(workspace) != receipt["source"]:
        raise DeliveryError("source changed after verification; a new attempt is required")
    root = Path(workspace["path"]).resolve()
    for item in receipt["files"]:
        if digest(local_file(root, item["path"]).read_bytes()) != item["sha256"]:
            raise DeliveryError("evidence changed after verification")


def latest_check(ledger: Any, execution_id: str) -> dict[str, Any] | None:
    ledger.current(execution_id)
    row = ledger.connection.execute(
        "SELECT payload_json FROM events WHERE execution_id=? AND event_type='delivery_checked' "
        "ORDER BY seq DESC LIMIT 1", (execution_id,),
    ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def export_review(ledger: Any, execution_id: str, output: str) -> dict[str, Any]:
    current = ledger.current(execution_id)
    receipt = latest_check(ledger, execution_id)
    planning = current["current_state_id"] in ("PlanReview", "ReplanReview") and receipt and receipt["contract"] == "plan-result-v2"
    if not planning and (current["current_state_id"] != "Review" or not receipt or receipt["contract"] not in ("python-verification-v1", "python-verification-v2")):
        raise DeliveryError("review export requires the verified Python workflow at Review")
    require_receipt(ledger, receipt["attempt_id"], receipt["contract"])
    workspace = ledger.workspace_for_execution(execution_id)
    root = Path(workspace["path"])
    source = receipt["source"]
    patch = git(root, "diff", "--binary", source["base_sha"], source["head_sha"], "--")
    if digest(patch) != source["patch_sha256"]:
        raise DeliveryError("review patch does not match checked source")
    from .handoff import build_handoff
    packet = {"schema_version": 1, "execution_id": execution_id,
              "intent": json.loads(current["intent_snapshot_json"]),
              "context": build_handoff(ledger.connection, execution_id, receipt["attempt_id"]),
              "issue": current["work_item_identifier"], "check": receipt,
              "next_action": "Review and approve the proposed verification plan." if planning else "Review the patch and checks; request changes or explicitly authorize merge."}
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=False)
    definition = receipt.get("verification_plan") or receipt.get("approved_plan")
    if definition:
        (destination / "verification-plan.json").write_bytes(encoded(definition) + b"\n")
        for name in definition["files"]:
            target = destination / "checks" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(local_file(root.resolve(), name).read_bytes())
    (destination / "review.json").write_bytes(encoded(packet) + b"\n")
    (destination / "change.patch").write_bytes(patch)
    (destination / "verification.txt").write_text(receipt.get("verification", {}).get("output", "Checks proposed; not executed or approved.\n"))
    (destination / "README.md").write_text(
        "# Delivery review\n\n" + str(receipt.get("delivery", {}).get("summary", "Review the proposed acceptance criteria, verifier, test files, and manual procedures.")) + "\n\n"
        + f"Source: {source['base_sha']} → {source['head_sha']}\n\n"
        + "Inspect change.patch, review.json (requirements and feedback), verification.txt, and any verification-plan.json/checks directory.\n"
        + ("Approve this exact plan before implementation, or request planning changes.\n\n" if planning else "The pinned checks passed; human review and merge approval remain required.\n\n")
        + "Host check limits:\n" + "\n".join("- " + x for x in receipt["limitations"]) + "\n\n"
        + "Agent-declared limits (recorded before the host check):\n"
        + "\n".join("- " + x for x in receipt.get("delivery", {}).get("limitations", ["Planning validation checks structure, not test adequacy."])) + "\n")
    return {"output": str(destination.resolve()), "head_sha": source["head_sha"],
            "patch_sha256": digest(patch), "review_sha256": digest(encoded(packet) + b"\n")}
