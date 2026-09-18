"""Prepare durable cloud state, then host one coordinator or await setup."""

import os
from pathlib import Path
import signal
import sys
import tempfile
import threading


def prepare(root=Path("/data")):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("worker disk must be an existing directory")
    for name in ("codex", "claude", "work", "instances", "repositories"):
        path = root / name
        if path.is_symlink():
            raise ValueError("worker disk directories cannot be symlinks")
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    with tempfile.TemporaryFile(dir=root) as probe:
        probe.write(b"ready\n")
        probe.flush()
        os.fsync(probe.fileno())


def coordinator_command(environ, root=Path("/data")):
    config = environ.get("DOTFACTORY_CONFIG", "")
    project = environ.get("DOTFACTORY_PROJECT", "")
    if not config and not project:
        return None
    if not config or not project:
        raise ValueError("coordinator requires DOTFACTORY_CONFIG and DOTFACTORY_PROJECT")
    path = Path(config)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("coordinator config must be an existing absolute file")
    if root.resolve() not in path.resolve().parents:
        raise ValueError("coordinator config must be persisted under /data")
    from dotfactory.instance import FactoryConfig
    config_values = FactoryConfig.load(path)
    resolved_project = config_values.resolve_project(project, environment=environ)
    preparation = config_values.resolve_preparation(project, environment=environ)
    ledger = Path(config_values.values["ledger_path"]).expanduser()
    if not ledger.is_absolute():
        ledger = path.parent / ledger
    for state_path in (ledger, Path(resolved_project["repository_path"]),
                       Path(preparation["workspace"]["root"])):
        if root.resolve() not in state_path.resolve().parents:
            raise ValueError("coordinator ledger, repository and workspaces must stay under /data")
    for worker in config_values.values.get("execution", {}).get("workers", {}).values():
        if (worker["transport"] == "local"
                and root.resolve() not in Path(worker["root"]).resolve().parents):
            raise ValueError("local worker attempts must stay under /data")
    return [sys.executable, "-m", "dotfactory", "work", "--config", str(path),
            "--project", project]


def main():
    if os.geteuid() != 10001 or os.getegid() != 10001:
        raise ValueError("worker must run as UID/GID 10001")
    prepare()
    command = coordinator_command(os.environ)
    if command:
        os.execv(sys.executable, command)
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stopped.set())
    print("cloud disk ready; runtime available for setup over authenticated SSH", flush=True)
    stopped.wait()


if __name__ == "__main__":
    main()
