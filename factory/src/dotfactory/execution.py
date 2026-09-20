"""Stage placement, worker transport, and fenced source handoffs."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import worker
from .resources import PreparationResult


class ExecutionError(RuntimeError):
    pass


def overlay_policy(policy, override, *, origin, provenance=None):
    """Omitted fields inherit; supplied fields replace, including empty lists."""
    if not isinstance(override, dict) or set(override) - {"stages"}:
        raise ValueError("execution override accepts only stages")
    stages = override.get("stages", {})
    if not isinstance(stages, dict):
        raise ValueError("execution override stages must be an object")
    result = copy.deepcopy(policy)
    sources = copy.deepcopy(provenance or {
        state: {field: "instance" for field in rule}
        for state, rule in policy["stages"].items()})
    for state, fields in stages.items():
        if not isinstance(state, str) or not state or not isinstance(fields, dict):
            raise ValueError("execution override stages require named objects")
        result["stages"].setdefault(state, {}).update(copy.deepcopy(fields))
        sources.setdefault(state, {}).update({field: origin for field in fields})
    validate_policy(result)
    return result, sources


def resolve_settings(policy, project=None, override=None, *, work_states=None):
    """Pure resolution shared by admission and inspection; never probes or opens a ledger."""
    project = {} if project is None else project
    override = {} if override is None else override
    resolved, sources = overlay_policy(policy, project, origin="project")
    resolved, sources = overlay_policy(resolved, override, origin="run", provenance=sources)
    if work_states is not None:
        for layer in (project, override):
            if set(layer.get("stages", {})) - set(work_states):
                raise ValueError("execution override names a non-work or unknown workflow stage")
        # Instance defaults can serve several workflows. Unused stage contracts
        # must not block this workflow's coordinator preflight.
        resolved["stages"] = {key: value for key, value in resolved["stages"].items() if key in work_states}
        sources = {key: value for key, value in sources.items() if key in work_states}
    return resolved, sources


def record_admission(db, execution_id, settings):
    db.execute("INSERT INTO execution_policies VALUES (?,?)",
               (execution_id, json.dumps(settings, sort_keys=True)))


def assert_no_frozen_worker_runs(ledger, project_keys):
    if not ledger.connection.execute("SELECT 1 FROM sqlite_master WHERE name='execution_policies'").fetchone():
        return
    rows = ledger.connection.execute(
        "SELECT wi.project_key FROM execution_policies ep "
        "JOIN workflow_executions we ON we.id=ep.execution_id "
        "JOIN work_items wi ON wi.id=we.work_item_id WHERE we.status!='completed'").fetchall()
    if any(row[0] in project_keys for row in rows):
        raise ExecutionError("active runs have frozen worker settings; restore execution configuration before dispatch")


def settings_view(ledger, execution_id):
    if not ledger.connection.execute("SELECT 1 FROM sqlite_master WHERE name='execution_policies'").fetchone():
        return None
    row = ledger.connection.execute("SELECT policy_json FROM execution_policies WHERE execution_id=?", (execution_id,)).fetchone()
    if not row:
        return None
    from .planning import effective_settings
    value = effective_settings(ledger, execution_id)
    return {"stages": value["policy"]["stages"], "provenance": value.get("provenance", {}),
            "digest": worker.digest(value), "frozen_at": value.get("frozen_at", "legacy-first-placement")}


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {"workers", "stages"}:
        raise ValueError("execution requires workers and stages")
    if not isinstance(policy["workers"], dict) or not policy["workers"]:
        raise ValueError("execution workers must be a nonempty object")
    for name, config in policy["workers"].items():
        if not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError("invalid worker name")
        if set(config) - {"transport", "target", "root", "billing", "python", "entrypoint", "agent", "project", "environment", "command", "location"}:
            raise ValueError("unknown worker setting")
        if "location" in config and config["location"] not in ("local", "cloud"):
            raise ValueError("worker location must be local or cloud")
        if config.get("transport") not in ("local", "ssh", "railway") or config.get("billing") not in ("subscription", "api"):
            raise ValueError("worker transport and billing must be explicit")
        if not isinstance(config.get("root"), str) or not config["root"].startswith("/"):
            raise ValueError("worker root must be an absolute path")
        if config["transport"] == "ssh":
            if not re.fullmatch(r"[A-Za-z0-9_@:.-]+", config.get("target", "")) or config["target"].startswith("-"):
                raise ValueError("invalid SSH target")
            if not config.get("entrypoint", "").startswith("/"):
                raise ValueError("SSH worker requires an installed absolute entrypoint")
        if config["transport"] == "railway":
            for key in ("agent", "project", "environment"):
                if not re.fullmatch(r"[0-9a-fA-F-]{36}", config.get(key, "")):
                    raise ValueError("Railway worker requires exact agent, project, and environment UUIDs")
            if not config.get("entrypoint", "").startswith("/"):
                raise ValueError("Railway worker requires an installed absolute entrypoint")
    if not isinstance(policy["stages"], dict) or not policy["stages"]:
        raise ValueError("execution stages must be a nonempty object")
    for rule in policy["stages"].values():
        if not isinstance(rule, dict) or set(rule) - {"workers", "requires", "scope", "checks", "check_timeout_seconds", "readiness", "coordinator"}:
            raise ValueError("unknown execution stage setting")
        if rule.get("scope") not in ("portable", "native"):
            raise ValueError("stage scope must explicitly be portable or native")
        if not isinstance(rule.get("workers"), list) or not rule["workers"] or any(name not in policy["workers"] for name in rule["workers"]):
            raise ValueError("stage requires configured worker candidates in preference order")
        if len({policy["workers"][name]["billing"] for name in rule["workers"]}) != 1:
            raise ValueError("stage candidates cannot silently change billing methods")
        if not isinstance(rule.get("requires"), list) or any(not re.fullmatch(r"(?:os|arch|tool):[A-Za-z0-9_.+-]+", item) for item in rule["requires"]):
            raise ValueError("stage requires explicit OS, architecture, or tool requirements")
        if not isinstance(rule.get("checks"), list) or any(not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in argv) for argv in rule["checks"]):
            raise ValueError("stage checks must be arrays of command arguments")
        worker.validate_readiness(rule.get("readiness", []))
        from .verification_host import validate as validate_coordinator
        validate_coordinator(rule.get("coordinator", {}))
        timeout = rule.get("check_timeout_seconds", 300)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError("check timeout must be 1..3600 seconds")


def validate_report(report, probes, minimum_version):
    """Fail closed for old/malformed workers; diagnostics and dispatch share this check."""
    expected = [item["name"] for item in probes]
    results = report.get("readiness", [])
    if expected and (not isinstance(results, list) or len(results) != len(expected)
            or not all(isinstance(item, dict) for item in results)
            or [item.get("name") for item in results] != expected):
        raise ExecutionError("worker did not report requested readiness probes")
    if expected and any(item.get("passed") is not True or item.get("exit_code") != 0 for item in results):
        report["available"] = False
        report["missing"] = list(report.get("missing", [])) + [
            "readiness:" + item["name"] + ":failed" for item in results
            if item.get("passed") is not True or item.get("exit_code") != 0]
    if report.get("available") is True:
        from .live_runner import _version_tuple
        current, minimum = _version_tuple(report.get("version", "")), _version_tuple(minimum_version)
        width = max(len(current), len(minimum))
        if current + (0,) * (width - len(current)) < minimum + (0,) * (width - len(minimum)):
            raise ExecutionError("runner is too old")


def worker_requirements(rule, repository_path):
    requirements = set(rule["requires"]) | {"tool:git"}
    commands = {Path(argv[0]).name for argv in rule["checks"]}
    requirements.update("tool:" + item for item in commands)
    apple = "xcodebuild" in commands or "xcrun" in commands
    if rule["scope"] == "native":
        repository = Path(repository_path)
        paths = worker.git(repository, "ls-files").splitlines()
        apple = apple or any(".xcodeproj/" in path or ".xcworkspace/" in path for path in paths)
    if apple:
        requirements.update(("os:darwin", "tool:xcodebuild"))
    return requirements


def transport_command(config):
    if config["transport"] == "local":
        return [sys.executable, "-I", str(Path(worker.__file__).resolve())]
    return remote_command(config, [config.get("python", "python3"), "-I", config["entrypoint"]])


def remote_command(config, argv):
    if config["transport"] == "railway":
        return [config.get("command", "railway"), "ca", "ssh", config["agent"],
                "--project", config["project"], "--environment", config["environment"], "--", *argv]
    remote = shlex.join(argv)
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
            config["target"], remote]


def call(config, spec, *, timeout=60):
    data = json.dumps(spec).encode()
    if len(data) > worker.MAX_BYTES:
        raise ExecutionError("worker request exceeds transfer limit")
    try:
        if config["transport"] == "railway" and spec["op"] == "probe":
            inventory = subprocess.run([config.get("command", "railway"), "ca", "list", "--json",
                                        "--project", config["project"], "--environment", config["environment"]],
                                       capture_output=True, text=True, timeout=20)
            if inventory.returncode or not any(item.get("id") == config["agent"] for item in json.loads(inventory.stdout)):
                raise ExecutionError("configured Railway agent is unavailable; explicitly create or select one")
        result = subprocess.run(transport_command(config), input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExecutionError("worker transport unavailable") from error
    if result.returncode:
        # Worker emits only bounded, redacted failures; SSH stderr is not persisted.
        try:
            reason = json.loads(result.stderr)["error"]
        except (ValueError, KeyError, TypeError):
            reason = "worker transport failed"
        raise ExecutionError(str(reason)[:300])
    if len(result.stdout) > worker.MAX_BYTES:
        raise ExecutionError("worker response exceeds transfer limit")
    return json.loads(result.stdout)


class ExecutionManager:
    def __init__(self, ledger, policy, routes, project_overrides=None):
        validate_policy(policy)
        self.ledger = ledger
        self.policy = policy
        self.project_overrides = copy.deepcopy(project_overrides or {})
        self.routes = routes
        self.last_renewal = {}
        with ledger.connection:
            ledger.connection.execute("CREATE TABLE IF NOT EXISTS execution_policies (execution_id TEXT PRIMARY KEY REFERENCES workflow_executions(id), policy_json TEXT NOT NULL)")
            ledger.connection.execute("CREATE TABLE IF NOT EXISTS worker_handoffs (attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), execution_id TEXT NOT NULL REFERENCES workflow_executions(id), manifest_json TEXT NOT NULL)")

    def admission(self, project_key, override=None, *, work_states=None):
        project = self.project_overrides.get(project_key, {})
        override = {} if override is None else override
        policy, sources = resolve_settings(self.policy, project, override, work_states=work_states)
        return {"policy": policy, "routes": {key: asdict(value) for key, value in self.routes.items()},
                "project_key": project_key, "run_overrides": copy.deepcopy(override),
                "provenance": sources, "frozen_at": "admission"}

    def assert_same_override(self, execution_id, override):
        if override is None:
            return
        row = self.ledger.connection.execute("SELECT policy_json FROM execution_policies WHERE execution_id=?", (execution_id,)).fetchone()
        if not row or json.loads(row[0]).get("run_overrides", {}) != override:
            raise ExecutionError("existing run settings are frozen; start a new run to change overrides")

    def _policy(self, request):
        row = self.ledger.connection.execute("SELECT policy_json FROM execution_policies WHERE execution_id=?", (request.execution_id,)).fetchone()
        if row:
            if request.state_id in ("Planning", "Autoplanning"):
                return json.loads(row[0])
            from .planning import effective_settings
            return effective_settings(self.ledger, request.execution_id)
        # Compatibility for runs created before admission snapshots existed.
        project = self.ledger.current(request.execution_id)["project_key"]
        settings = self.admission(project)
        settings["frozen_at"] = "legacy-first-placement"
        with self.ledger.transaction() as db:
            db.execute("INSERT OR IGNORE INTO execution_policies VALUES (?,?)",
                       (request.execution_id, json.dumps(settings, sort_keys=True)))
        row = self.ledger.connection.execute("SELECT policy_json FROM execution_policies WHERE execution_id=?", (request.execution_id,)).fetchone()
        return json.loads(row[0])

    def record(self, request):
        row = self.ledger.connection.execute("SELECT manifest_json FROM worker_handoffs WHERE attempt_id=?", (request.attempt_id,)).fetchone()
        if not row:
            return None
        result = json.loads(row[0])
        if result["fence_digest"] != worker.digest(request.fence_token):
            raise ExecutionError("worker handoff ownership changed")
        return result

    def _save(self, request, manifest):
        self.ledger.assert_attempt_active(request.attempt_id, request.fence_token)
        with self.ledger.connection:
            self.ledger.connection.execute("INSERT INTO worker_handoffs VALUES (?,?,?) ON CONFLICT(attempt_id) DO UPDATE SET manifest_json=excluded.manifest_json",
                                           (request.attempt_id, request.execution_id, json.dumps(manifest, sort_keys=True)))
            key = f"worker:{request.attempt_id}:{manifest['status']}"
            if not self.ledger.event_for_command(key):
                self.ledger._event(
                    self.ledger.connection, execution_id=request.execution_id,
                    state_run_id=None, attempt_id=request.attempt_id,
                    event_type="worker_handoff_" + manifest["status"],
                    payload={key: manifest[key] for key in
                             ("worker", "state", "status", "requirements", "source_sha", "output_sha", "checks")
                             if key in manifest}, idempotency_key=key,
                )

    def precheck(self, request, project):
        self.ledger.assert_attempt_active(request.attempt_id, request.fence_token)
        binding = self._policy(request)
        from .verification_host import inspect as inspect_coordinator
        coordinator_reports = {}
        # All configured verification lanes must be viable before any model work.
        for state, configured in binding["policy"]["stages"].items():
            if configured.get("coordinator"):
                report = inspect_coordinator(configured["coordinator"], execute=True)
                coordinator_reports[state] = report
                if not report["available"]:
                    raise ExecutionError("coordinator verification prerequisites unavailable for " + state + ": " + ", ".join(report["missing"]))
        if self.record(request):
            return
        policy = binding["policy"]
        rule = policy["stages"].get(request.state_id)
        if not rule:
            raise ExecutionError("stage has no declared execution requirements: " + request.state_id)
        if request.config.get("resources"):
            raise ExecutionError("worker placement cannot transfer local resource handles; declare worker-native requirements")
        from .live_runner import RunnerRoute
        route = RunnerRoute(**binding["routes"][request.config["runner"]])
        if route.kind not in ("codex", "claude-code"):
            raise ExecutionError("worker execution supports native Codex and Claude Code")
        requirements = worker_requirements(rule, project["repository_path"])
        failures = []
        for name in rule["workers"]:
            config = policy["workers"][name]
            spec = {"op": "probe", "kind": route.kind, "command": route.command,
                    "billing": config["billing"], "requires": sorted(requirements),
                    "environment_envs": list(route.environment_envs),
                    "readiness": rule.get("readiness", [])}
            try:
                budget = 45 + sum(item.get("timeout_seconds", 10) for item in spec["readiness"])
                report = call(config, spec, timeout=budget)
                validate_report(report, spec["readiness"], route.minimum_version)
                if not report["available"]:
                    failures.append(name + ": " + ", ".join(report["missing"]))
                    continue
            except (ExecutionError, OSError, ValueError) as error:
                failures.append(name + ": " + str(error))
                continue
            manifest = {"worker": name, "worker_config": config, "rule": rule,
                        "route": asdict(route), "report": report, "coordinator_reports": coordinator_reports, "requirements": sorted(requirements),
                        "fence_digest": worker.digest(request.fence_token), "status": "selected",
                        "identity": hashlib.sha256((request.execution_id + request.attempt_id).encode()).hexdigest(),
                        "state": request.state_id, "workflow_digest": request.workflow_digest}
            self._save(request, manifest)
            return
        raise ExecutionError("No eligible worker. " + "; ".join(failures))

    def prepare(self, launch):
        request = launch.request
        record = self.record(request)
        if record["status"] not in ("selected", "preparing"):
            return
        local = Path(launch.workspace_path)
        sha = worker.snapshot(local)
        files = []
        for skill in launch.skills:
            root = Path(skill.entrypoint).parent
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ExecutionError("worker skill transfer does not follow symlinks")
                if path.is_file():
                    data = path.read_bytes()
                    files.append({"path": skill.name + "/" + path.relative_to(root).as_posix(),
                                  "data": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest(),
                                  "mode": path.stat().st_mode})
        if record["status"] == "selected":
            record.update({"source_sha": sha, "local_workspace": str(local), "status": "preparing"})
            record["binding"] = worker.digest(record)
            # Save intent before external allocation. The source object remains in Git after a crash.
            self._save(request, record)
        elif worker.git(local, "rev-parse", sha + "^{tree}") != worker.git(local, "rev-parse", record["source_sha"] + "^{tree}"):
            raise ExecutionError("source changed during worker preparation; retain allocation")
        self._prepare_remote(launch, record, files)

    def _prepare_remote(self, launch, record, files):
        spec = self.spec(record, "prepare")
        spec.update({"bundle": worker.bundle(Path(launch.workspace_path), record["source_sha"]),
                     "skills": files, "schema": json.loads(Path(__file__).with_name("runner_result.schema.json").read_text())})
        result = call(record["worker_config"], spec)
        record.update({"remote_workspace": result["workspace"], "status": "prepared"})
        self._save(launch.request, record)

    @staticmethod
    def spec(record, op):
        return {"op": op, "identity": record["identity"], "root": record["worker_config"]["root"],
                "binding": record["binding"], "source_sha": record["source_sha"],
                "kind": record["route"]["kind"], "billing": record["worker_config"]["billing"],
                "environment_envs": record["route"]["environment_envs"]}

    def translate(self, launch, value):
        record = self.record(launch.request)
        value = value.replace(launch.workspace_path, record["remote_workspace"])
        directory = str(Path(record["remote_workspace"]).parent)
        value = value.replace(str(Path(__file__).with_name("runner_result.schema.json")), directory + "/result.schema.json")
        for skill in launch.skills:
            value = value.replace(str(Path(skill.entrypoint).parent), directory + "/skills/" + skill.name)
        return value

    def run_command(self, launch, argv):
        record = self.record(launch.request)
        spec = self.spec(record, "command")
        spec["argv"] = [self.translate(launch, item) for item in argv]
        return call(record["worker_config"], spec)["stdout"]

    def invocation(self, launch, argv, payload):
        self.ledger.assert_attempt_active(launch.request.attempt_id, launch.request.fence_token)
        record = self.record(launch.request)
        if record["status"] != "prepared":
            raise ExecutionError("worker allocation is not prepared; reconcile before dispatch")
        spec = self.spec(record, "exec")
        from .live_runner import _duration_seconds
        spec.update({"argv": [self.translate(launch, item) for item in argv],
                     "input": base64.b64encode(self.translate(launch, payload.decode()).encode()).decode(),
                     "timeout_seconds": _duration_seconds(launch.request.config.get("timeout", "30m"))})
        return transport_command(record["worker_config"]), json.dumps(spec).encode()

    def renew(self, launch):
        identity = launch.request.attempt_id
        if time.monotonic() - self.last_renewal.get(identity, 0) < 15:
            return
        self.ledger.assert_attempt_active(identity, launch.request.fence_token)
        record = self.record(launch.request)
        call(record["worker_config"], self.spec(record, "renew"), timeout=15)
        self.last_renewal[identity] = time.monotonic()

    def cancel(self, launch):
        record = self.record(launch.request)
        if record and record.get("binding"):
            try:
                call(record["worker_config"], self.spec(record, "cancel"), timeout=15)
            except ExecutionError:
                pass  # Worker lease expires independently within 60 seconds.

    def accept(self, launch):
        request = launch.request
        self.ledger.assert_attempt_active(request.attempt_id, request.fence_token)
        record = self.record(request)
        if record["status"] == "accepted":
            return self.evidence(record)
        if record["status"] != "importing":
            spec = self.spec(record, "collect")
            spec.update({"checks": record["rule"]["checks"], "check_timeout_seconds": record["rule"].get("check_timeout_seconds", 300)})
            result = call(record["worker_config"], spec, timeout=60 + len(spec["checks"]) * spec["check_timeout_seconds"])
            if result["binding"] != record["binding"] or result["source_sha"] != record["source_sha"]:
                raise ExecutionError("returned handoff does not match the prepared input")
            self.ledger.assert_attempt_active(request.attempt_id, request.fence_token)
            local = Path(launch.workspace_path)
            worker.import_bundle(local, result["bundle"], result["output_sha"])
            worker.git(local, "merge-base", "--is-ancestor", record["source_sha"], result["output_sha"])
            record.update({"output_sha": result["output_sha"], "checks": result["checks"], "status": "importing"})
            self._save(request, record)
        local = Path(launch.workspace_path)
        sha = worker.snapshot(local)
        # Content comparison avoids timestamp-dependent snapshot commit identities.
        actual_tree = worker.git(local, "rev-parse", sha + "^{tree}")
        source_tree = worker.git(local, "rev-parse", record["source_sha"] + "^{tree}")
        output_tree = worker.git(local, "rev-parse", record["output_sha"] + "^{tree}")
        if actual_tree not in (source_tree, output_tree):
            raise ExecutionError("coordinator workspace changed during remote work; retain both copies")
        self.ledger.assert_attempt_active(request.attempt_id, request.fence_token)
        if actual_tree == source_tree:
            # Snapshot represents these exact files; reset only index/HEAD, then fast-forward.
            worker.git(local, "reset", "--mixed", record["source_sha"])
            worker.git(local, "merge", "--ff-only", record["output_sha"])
        elif worker.git(local, "rev-parse", "HEAD") != record["output_sha"]:
            raise ExecutionError("ambiguous handoff import; inspect source ownership")
        record["status"] = "accepted"
        self._save(request, record)
        return self.evidence(record)

    @staticmethod
    def evidence(record):
        return {"kind": "worker_handoff", "uri": "git:" + record["output_sha"],
                "worker": record["worker"], "source_sha": record["source_sha"],
                "output_sha": record["output_sha"], "checks": record["checks"]}


class WorkerPreparation:
    def __init__(self, preparation, manager):
        self.preparation = preparation
        self.manager = manager

    def prepare(self, request):
        try:
            self.manager.precheck(request, self.preparation.project)
            result = self.preparation.prepare(request)
            if result.disposition == "ready":
                self.manager.prepare(result.launch)
            return result
        except (ExecutionError, worker.WorkerError) as error:
            return PreparationResult("fatal", error={"category": "worker-placement", "message": str(error)})

    def cleanup_attempt(self, launch):
        return self.preparation.cleanup_attempt(launch)
