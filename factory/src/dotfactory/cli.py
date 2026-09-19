"""Command-line entry point for the composed factory lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .control import Principal
from .datasets import (
    HostedDatasetPublisher, HostedDatasetSettings, execution_dataset_case,
    write_dataset_bundle,
)
from .instance import FactoryConfig
from .lifecycle import FactoryRuntime, _ledger_path, fixture_runner


def _git(directory: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments], check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")


def _demo_config(root: Path) -> Path:
    origin = root / "origin.git"
    seed = root / "seed"
    repository = root / "repository"
    subprocess.run(
        ["git", "init", "--bare", str(origin)], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(seed)], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    _git(seed, "config", "user.email", "demo@dotfactory.local")
    _git(seed, "config", "user.name", "dotfactory demo")
    (seed / ".gitignore").write_text("/.worktrees/\n", encoding="utf-8")
    (seed / "README.md").write_text("dotfactory lifecycle demo\n", encoding="utf-8")
    _git(seed, "add", ".gitignore", "README.md")
    _git(seed, "commit", "-m", "seed lifecycle demo")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "clone", "-b", "main", str(origin), str(repository)], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    workflow = Path(__file__).resolve().parents[2] / "workflows" / "default.dot"
    config = {
        "schema_version": 6, "factory_id": "lifecycle-demo",
        "ledger_path": str(root / "factory.db"), "default_workflow": "default",
        "workflows": {"default": {
            "path": str(workflow), "profile_paths": [],
            "defaults": {"runner": "codex", "resources": []},
        }},
        "scheduler": {
            "poll_interval_ms": 50, "claim_ttl_seconds": 30,
            "limits": {"host": 1, "projects": {"demo": 1},
                       "runners": {"codex": 1}},
        },
        "runners": {"codex": {
            "kind": "codex", "command": "codex", "minimum_version": "0.1.0",
            "permission_mode": "approve-for-me",
            "default_model": "gpt-5.6-sol",
            "default_reasoning_effort": "medium", "capabilities": [],
        }},
        "preparation": {
            "workspace": {"remote": "origin", "base_ref": "main",
                          "retention": "until_terminal"},
            "providers": {"portless": {
                "kind": "portless", "command": "portless", "version": "0.15.6",
                "node_minimum": 24,
                "preflight_command": "dotfactory-portless-preflight",
            }},
            "capabilities": {},
        },
        "projects": {"demo": {
            "display_name": "Lifecycle demo", "enabled_by_default": True,
            "workflow": "default", "repository_path": str(repository),
            "tracker": {"kind": "linear", "project_id": "demo-project"},
        }},
        "projections": {"linear": {
            "enabled": False, "token_env": "LINEAR_API_KEY",
            "endpoint": "https://api.linear.app/graphql",
        }},
    }
    path = root / "factory.json"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def _install_signals(runtime: FactoryRuntime) -> None:
    def stop(_number: int, _frame: Any) -> None:
        runtime.request_stop("signal")
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


def _run_exit_code(runtime: FactoryRuntime, receipt: Any) -> int:
    if receipt.shutdown_reason not in ("settled", "target_state", "drained") or not receipt.ticks:
        return 1
    for execution in receipt.executions:
        snapshot = runtime.ledger.run_snapshot(str(execution["execution_id"]))
        if snapshot.get("attention_requests"):
            return 1
    final_disposition = receipt.ticks[-1]["scheduler"]["disposition"]
    if receipt.shutdown_reason in ("target_state", "drained"):
        return 0
    return 0 if final_disposition == "idle" else 1


def _load_description(path_value: str | None) -> str:
    if path_value is None:
        return ""
    path = Path(path_value)
    if not path.is_file():
        raise ValueError("description file is not a regular file")
    try:
        with path.open("r", encoding="utf-8") as description_file:
            description = description_file.read(65_537)
    except UnicodeDecodeError as error:
        raise ValueError("description file is not valid UTF-8") from error
    except OSError as error:
        raise ValueError("description file could not be read") from error
    if len(description) > 65_536:
        raise ValueError("description file exceeds 65,536 characters")
    return description


def _run(args: argparse.Namespace) -> int:
    config = FactoryConfig.load(args.config)
    description = _load_description(args.description_file)
    with FactoryRuntime(config, project_keys=[args.project]) as runtime:
        _install_signals(runtime)
        runtime.enable_operator()
        issue = args.issue
        title = args.title
        if not issue:
            discovered = runtime.discover_issue(args.project)
            issue = str(discovered["identifier"])
            title = str(discovered.get("title") or issue)
        execution = runtime.start_issue(
            args.project, issue, title=title,
            description=description,
        )
        receipt = runtime.run(
            [execution], watch=args.watch,
            max_ticks=args.max_ticks if args.max_ticks is not None else 100,
            until_state=args.until_state,
        )
        print(json.dumps(receipt.as_dict(), indent=2, sort_keys=True))
        exit_code = _run_exit_code(runtime, receipt)
    return exit_code


def _work(args: argparse.Namespace) -> int:
    from .work_queue import WorkQueue
    config = FactoryConfig.load(args.config)
    if args.max_ticks is not None and args.max_ticks < 1:
        raise ValueError("max-ticks must be positive")
    if not config.values.get("work_queue", {}).get("enabled", False):
        raise ValueError("work requires work_queue.enabled=true; opt in before starting discovery")
    with FactoryRuntime(config, project_keys=args.project) as runtime:
        queue = WorkQueue(runtime)
        _install_signals(runtime)
        runtime.enable_operator()
        ticks = 0
        while (not runtime.stop_requested and not runtime.drain_requested
               and (args.max_ticks is None or ticks < args.max_ticks)):
            ticks += 1
            print(json.dumps(queue.step(), sort_keys=True), flush=True)
            if args.max_ticks is None or ticks < args.max_ticks:
                runtime._wait(runtime.scheduler.policy.poll_interval_ms / 1000)
        reason = "drained" if runtime.drain_requested else runtime.shutdown_reason if runtime.stop_requested else "max_ticks"
        for project in runtime.project_keys:
            runtime.ledger.record_operating_receipt("queue", project, None, {"status": reason})
        print(json.dumps({"status": reason, "ticks": ticks}), flush=True)
    return 0


def _attention(args: argparse.Namespace) -> int:
    config = FactoryConfig.load(args.config)
    request = {
        "action": "attention", "expected_state": args.expected_state,
        "confirmed": args.confirm,
        "parameters": {"attention_id": args.attention_id, "remedy": args.remedy,
                       "expected_attempt_id": args.expected_attempt},
    }
    from .operator import send, socket_path
    endpoint = socket_path(_ledger_path(config))
    if endpoint.exists():
        try:
            response = send(endpoint, {"operation": "command", "project": args.project,
                                       "execution": args.execution, "command_id": args.command_id,
                                       "request": request})
        except (ConnectionRefusedError, FileNotFoundError):
            # No connection accepted the command. The fallback still must win
            # the exclusive instance lock before it may write anything.
            pass
        else:
            print(json.dumps(response, indent=2, sort_keys=True))
            return 0 if response.get("ok") and response["data"]["status"] == "completed" else 1
    with FactoryRuntime(
        config, project_keys=[args.project], control_only=True,
    ) as runtime:
        receipt = runtime.control_service(args.project).execute(
            args.execution, command_id=args.command_id,
            principal=Principal(args.subject, "approver", "cli"), request=request,
        )
        print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "completed" else 1


def _operator(args: argparse.Namespace) -> int:
    from .operator import send, socket_path
    config = FactoryConfig.load(args.config)
    message = {"operation": args.operation, "project": args.project,
               "execution": args.execution}
    if args.operation == "command":
        if not args.request_file or not args.command_id or not args.execution:
            raise ValueError("command requires execution, command-id, and request-file")
        message.update({"command_id": args.command_id,
                        "request": json.loads(Path(args.request_file).read_text())})
    endpoint = socket_path(_ledger_path(config))
    response = send(endpoint, message)
    print(json.dumps(response, indent=2, sort_keys=True))
    result = response.get("data", {})
    return 0 if response.get("ok") and result.get("status") not in ("failed", "denied") else 1


def _initialize(args: argparse.Namespace) -> int:
    from .local_delivery import initialize
    result = initialize(
        repository=args.repository, output=args.output, project=args.project,
        linear_project=args.linear_project, logfire=args.logfire,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _status(args: argparse.Namespace) -> int:
    from .control import ObservationService
    from .operator import send, socket_path
    config = FactoryConfig.load(args.config)
    config.selected_project_keys([args.project])
    ledger_path = _ledger_path(config)
    endpoint = socket_path(ledger_path)
    if endpoint.exists():
        try:
            response = send(endpoint, {
                "operation": "status", "project": args.project,
                "execution": args.execution,
            })
        except (ConnectionRefusedError, FileNotFoundError):
            pass  # Only a definitely unaccepted read may use the offline lock.
        else:
            print(json.dumps({**response, "transport": "owner-socket"}, indent=2, sort_keys=True))
            return 0 if response.get("ok") else 1
    if not ledger_path.is_file():
        raise ValueError("no local ledger exists; submit a task with run first")
    with FactoryRuntime(config, project_keys=[args.project], control_only=True) as runtime:
        observation = ObservationService(runtime.ledger, runtime.kernels[args.project])
        if args.execution:
            if runtime.ledger.current(args.execution)["project_key"] != args.project:
                raise ValueError("execution is not available in project " + args.project)
            data = observation.run(args.execution)
        else:
            data = observation.runs(project_key=args.project)
        response = {"ok": True, "transport": "offline-exclusive-lock", "data": data}
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0


def _demo(args: argparse.Namespace) -> int:
    parent = Path(args.output).expanduser().resolve() if args.output else Path.cwd() / ".dotfactory"
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="lifecycle-demo-", dir=str(parent)))
    config = FactoryConfig.load(_demo_config(root))
    with FactoryRuntime(config, runner=fixture_runner()) as runtime:
        execution = runtime.start_issue("demo", "DEMO-1", title="Lifecycle demo")
        receipt = runtime.run([execution], max_ticks=20)
        projection = runtime.projects["demo"].kernel
        from .control import ObservationService
        view = ObservationService(runtime.ledger, projection)
        (root / "waterfall.html").write_text(
            view.waterfall_html(execution), encoding="utf-8"
        )
        (root / "receipt.json").write_text(
            json.dumps(receipt.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "demo_root": str(root), "receipt": str(root / "receipt.json"),
        "waterfall": str(root / "waterfall.html"),
        "current_state": receipt.executions[0]["current_state"],
        "digest": receipt.digest,
    }, indent=2, sort_keys=True))
    return 0


def _delivery_export(args: argparse.Namespace) -> int:
    from .delivery import export_review
    config = FactoryConfig.load(args.config)
    with FactoryRuntime(config, project_keys=[args.project], control_only=True) as runtime:
        if runtime.ledger.current(args.execution)["project_key"] != args.project:
            raise RuntimeError("execution is outside the selected project")
        receipt = export_review(runtime.ledger, args.execution, args.output)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def _dataset(args: argparse.Namespace) -> int:
    config = FactoryConfig.load(args.config)
    with FactoryRuntime(
        config, project_keys=[args.project], control_only=True,
    ) as runtime:
        current = runtime.ledger.current(args.execution)
        if current["project_key"] != args.project:
            raise RuntimeError(
                f"execution is not available in project {args.project}"
            )
        from .control import ObservationService
        projection = ObservationService(
            runtime.ledger, runtime.kernels[args.project]
        ).execution_projection(args.execution)
        case = execution_dataset_case(runtime.ledger, args.execution, projection)
        receipt = write_dataset_bundle(args.output, [case])
        receipt["case_id"] = case["case_id"]
        receipt["hosted"] = {"enabled": False}
        if args.publish_hosted:
            settings = config.resolve_logfire_projection(
                environment=runtime.environment
            )
            if not settings["dataset_enabled"]:
                raise RuntimeError("hosted Logfire datasets are not enabled")
            receipt["hosted"] = HostedDatasetPublisher(
                HostedDatasetSettings(
                    api_key=str(settings["dataset_api_key"]),
                    project=str(settings["project"]), region=str(settings["region"]),
                    dataset_name=str(settings["dataset_name"]),
                ), ledger=runtime.ledger,
            ).publish(
                [case], command_id=f"dataset:{args.execution}:{case['case_id']}"
            )
        print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def _doctor(args: argparse.Namespace) -> int:
    from .doctor import run

    return run(args.config, json_output=args.json)


def _listener(args: argparse.Namespace) -> int:
    from .listener_control import manage_listener

    result = manage_listener(
        args.listener_action,
        service_id=args.service_id,
        token_env=args.token_env,
        timeout=args.timeout,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dotfactory")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser(
        "doctor", help="check read-only local factory prerequisites"
    )
    doctor.add_argument("--config", required=True)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(callback=_doctor)
    from .worker_cli import add_commands
    add_commands(commands)
    work = commands.add_parser("work", help="continuously discover Linear work and run the coordinator")
    work.add_argument("--config", default=os.environ.get("DOTFACTORY_CONFIG"))
    work.add_argument("--project", action="append")
    work.add_argument("--max-ticks", type=int)
    work.set_defaults(callback=_work)
    run = commands.add_parser("run", help="run one recoverable factory lifecycle")
    run.add_argument("--config", default=os.environ.get("DOTFACTORY_CONFIG"))
    run.add_argument("--project", required=True)
    run.add_argument(
        "--issue", help="issue identifier; omit to discover the oldest pickup issue"
    )
    run.add_argument("--title")
    run.add_argument("--description-file")
    run.add_argument("--until-state")
    run.add_argument("--watch", action="store_true")
    run.add_argument("--max-ticks", type=int)
    run.set_defaults(callback=_run)
    attention = commands.add_parser(
        "attention", help="record an audited attention remedy without running work"
    )
    attention.add_argument("--config", default=os.environ.get("DOTFACTORY_CONFIG"))
    attention.add_argument("--project", required=True)
    attention.add_argument("--execution", required=True)
    attention.add_argument("--attention-id", required=True)
    attention.add_argument("--expected-state", required=True)
    attention.add_argument("--expected-attempt")
    attention.add_argument("--remedy", choices=("retry", "cancel", "retain", "quarantine", "release"), required=True)
    attention.add_argument("--confirm", action="store_true")
    attention.add_argument("--command-id", required=True)
    attention.add_argument(
        "--subject", default=os.environ.get("USER", "local-operator")
    )
    attention.set_defaults(callback=_attention)
    operator = commands.add_parser("operator", help="control the running factory over its owner-only socket")
    operator.add_argument("operation", choices=("status", "artifacts", "delivery", "command", "drain"))
    operator.add_argument("--config", default=os.environ.get("DOTFACTORY_CONFIG"))
    operator.add_argument("--project", required=True)
    operator.add_argument("--execution")
    operator.add_argument("--request-file")
    operator.add_argument("--command-id")
    operator.set_defaults(callback=_operator)
    demo = commands.add_parser("demo", help="run a disposable Git-backed toy lifecycle")
    demo.add_argument("--output")
    demo.set_defaults(callback=_demo)
    dataset = commands.add_parser(
        "dataset", help="export a deterministic local execution dataset"
    )
    dataset.add_argument("--config", default=os.environ.get("DOTFACTORY_CONFIG"))
    dataset.add_argument("--project", required=True)
    dataset.add_argument("--execution", required=True)
    dataset.add_argument("--output", required=True)
    dataset.add_argument("--publish-hosted", action="store_true")
    dataset.set_defaults(callback=_dataset)
    delivery = commands.add_parser("delivery", help="export a verified review packet after stopping the runtime")
    delivery.add_argument("--config", default=os.environ.get("DOTFACTORY_CONFIG"))
    delivery.add_argument("--project", required=True)
    delivery.add_argument("--execution", required=True)
    delivery.add_argument("--output", required=True)
    delivery.set_defaults(callback=_delivery_export)
    initialize = commands.add_parser("init", help="initialize a local verified Python delivery instance")
    initialize.add_argument("--repository", required=True)
    initialize.add_argument("--output", required=True, help="new instance directory; never overwritten")
    initialize.add_argument("--project", required=True)
    initialize.add_argument("--linear-project", required=True, help="owning Linear project ID (sync stays disabled)")
    initialize.add_argument("--logfire", action="store_true", help="enable telemetry independently of Linear sync")
    initialize.set_defaults(callback=_initialize)
    status = commands.add_parser("status", help="inspect a running or stopped local factory")
    status.add_argument("--config", default=os.environ.get("DOTFACTORY_CONFIG"))
    status.add_argument("--project", required=True)
    status.add_argument("--execution")
    status.set_defaults(callback=_status)
    listener = commands.add_parser(
        "listener", help="inspect, suspend, or resume the hosted receipt listener"
    )
    listener.add_argument(
        "listener_action", choices=("status", "suspend", "resume")
    )
    listener.add_argument(
        "--service-id", default=os.environ.get("DOTFACTORY_LISTENER_SERVICE_ID")
    )
    listener.add_argument("--token-env", default="RENDER_API_KEY")
    listener.add_argument("--timeout", type=float, default=10.0)
    listener.set_defaults(callback=_listener)
    from .control_server import serve
    server = commands.add_parser("serve", help="serve the authenticated loopback control API")
    server.add_argument("--config", default=os.environ.get("DOTFACTORY_CONFIG"))
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--token-env", default="DOTFACTORY_API_TOKEN")
    server.add_argument("--role", choices=("viewer", "operator", "approver"), default="viewer")
    server.add_argument("--subject", default=os.environ.get("USER", "local-operator"))
    server.set_defaults(callback=serve)
    args = parser.parse_args(arguments)
    if args.command in ("run", "attention", "operator", "dataset", "delivery", "status", "serve", "work") and not args.config:
        parser.error(f"{args.command} requires --config or DOTFACTORY_CONFIG")
    try:
        return int(args.callback(args))
    except Exception as error:
        print(f"dotfactory: {error}", file=sys.stderr)
        return 1
