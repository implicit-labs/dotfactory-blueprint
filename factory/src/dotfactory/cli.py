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
from .instance import FactoryConfig
from .lifecycle import FactoryRuntime, fixture_runner


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
            "permission_mode": "approve-for-me", "capabilities": [],
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


def _run(args: argparse.Namespace) -> int:
    config = FactoryConfig.load(args.config)
    with FactoryRuntime(config, project_keys=[args.project]) as runtime:
        _install_signals(runtime)
        issue = args.issue
        title = args.title
        if not issue:
            discovered = runtime.discover_issue(args.project)
            issue = str(discovered["identifier"])
            title = str(discovered.get("title") or issue)
        execution = runtime.start_issue(
            args.project, issue, title=title
        )
        receipt = runtime.run(
            [execution], watch=args.watch, max_ticks=args.max_ticks
        )
        print(json.dumps(receipt.as_dict(), indent=2, sort_keys=True))
    return 0


def _attention(args: argparse.Namespace) -> int:
    config = FactoryConfig.load(args.config)
    with FactoryRuntime(
        config, project_keys=[args.project], control_only=True,
    ) as runtime:
        receipt = runtime.control_service(args.project).execute(
            args.execution, command_id=args.command_id,
            principal=Principal(args.subject, "operator", "cli"),
            request={
                "action": "attention", "expected_state": args.expected_state,
                "parameters": {
                    "attention_id": args.attention_id,
                    "remedy": args.remedy,
                    "expected_attempt_id": args.expected_attempt,
                },
            },
        )
        print(json.dumps(receipt, indent=2, sort_keys=True))
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


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dotfactory")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one recoverable factory lifecycle")
    run.add_argument("--config", default=os.environ.get("DOTFACTORY_CONFIG"))
    run.add_argument("--project", required=True)
    run.add_argument(
        "--issue", help="issue identifier; omit to discover the oldest pickup issue"
    )
    run.add_argument("--title")
    run.add_argument("--watch", action="store_true")
    run.add_argument("--max-ticks", type=int, default=100)
    run.set_defaults(callback=_run)
    attention = commands.add_parser(
        "attention", help="record an audited attention remedy without running work"
    )
    attention.add_argument("--config", default=os.environ.get("DOTFACTORY_CONFIG"))
    attention.add_argument("--project", required=True)
    attention.add_argument("--execution", required=True)
    attention.add_argument("--attention-id", required=True)
    attention.add_argument("--expected-state", required=True)
    attention.add_argument("--expected-attempt", required=True)
    attention.add_argument("--remedy", choices=("retry",), required=True)
    attention.add_argument("--command-id", required=True)
    attention.add_argument(
        "--subject", default=os.environ.get("USER", "local-operator")
    )
    attention.set_defaults(callback=_attention)
    demo = commands.add_parser("demo", help="run a disposable Git-backed toy lifecycle")
    demo.add_argument("--output")
    demo.set_defaults(callback=_demo)
    args = parser.parse_args(arguments)
    if args.command in ("run", "attention") and not args.config:
        parser.error(f"{args.command} requires --config or DOTFACTORY_CONFIG")
    try:
        return int(args.callback(args))
    except Exception as error:
        print(f"dotfactory: {error}", file=sys.stderr)
        return 1
