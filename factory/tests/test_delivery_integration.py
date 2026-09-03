import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotfactory import (  # noqa: E402
    DurableKernel, FakePreparedRunner, RunnerResult, SQLiteLedger,
    run_prepared_attempt,
)
from dotfactory.portless import PortlessProvider  # noqa: E402
from dotfactory.resources import PreparationEngine  # noqa: E402
from dotfactory.runner import runner_request  # noqa: E402
from dotfactory.workspace import GitWorkspaceProvider  # noqa: E402


@unittest.skipUnless(
    os.environ.get("DOTFACTORY_PORTLESS_LIVE") == "1",
    "set DOTFACTORY_PORTLESS_LIVE=1 on a configured Portless host",
)
class DeliveryBoundaryIntegrationTests(unittest.TestCase):
    def git(self, directory, *arguments):
        return subprocess.run(
            ["git", "-C", str(directory), *arguments], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()

    def seed_repository(self, root):
        origin = root / "origin.git"
        seed = root / "seed"
        checkout = root / "checkout"
        subprocess.run(
            ["git", "init", "--bare", str(origin)], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "init", "-b", "main", str(seed)], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.git(seed, "config", "user.email", "scenario@example.com")
        self.git(seed, "config", "user.name", "Scenario")
        (seed / ".gitignore").write_text("/.worktrees/\n")
        (seed / "README.md").write_text("delivery integration fixture\n")
        self.git(seed, "add", ".gitignore", "README.md")
        self.git(seed, "commit", "-m", "seed integration fixture")
        self.git(seed, "remote", "add", "origin", str(origin))
        self.git(seed, "push", "-u", "origin", "main")
        subprocess.run(
            ["git", "clone", "-b", "main", str(origin), str(checkout)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return checkout

    def wait_for_url(self, url):
        command = ["curl", "--fail", "--silent", "--show-error", "--http1.1"]
        ca = Path.home() / ".portless" / "ca.pem"
        if ca.is_file():
            command.extend(("--cacert", str(ca)))
        command.append(url)
        last = None
        for _attempt in range(50):
            last = subprocess.run(
                command, check=False, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            if last.returncode == 0:
                return
            time.sleep(0.1)
        self.fail(last.stderr.strip() or "Portless route did not become ready")

    def test_real_git_worktree_and_portless_route_are_owned_and_cleaned(self):
        with tempfile.TemporaryDirectory(prefix="dotfactory-delivery-integration-") as directory:
            root = Path(directory)
            repository = self.seed_repository(root)
            main_sha = self.git(repository, "rev-parse", "HEAD")
            ledger = SQLiteLedger(root / "factory.db")
            ledger.configure_factory("delivery-integration")
            ledger.register_project(
                "dotfactory", display_name="dotfactory", tracker_kind="linear",
                tracker_project_id="project-dotfactory",
            )
            kernel = DurableKernel(
                ledger, ROOT / "workflows" / "default.dot",
                factory_defaults={"runner": "codex", "resources": ["local-web"]},
            )
            execution = kernel.begin(
                "dotfactory", "DEMO-PORTLESS",
                {"title": "Disposable Git and Portless integration"},
                command_id="integration-begin",
            )
            kernel.transition(
                execution, "Autoplanning", actor="agent", signal="listener_claim",
                owner="integration-planner", command_id="integration-claim",
            )
            portless = PortlessProvider(
                command=os.environ.get("DOTFACTORY_PORTLESS_COMMAND", "portless"),
                preflight_command=os.environ.get(
                    "DOTFACTORY_PORTLESS_PREFLIGHT",
                    "dotfactory-portless-preflight",
                ),
                startup_timeout_seconds=20,
            )
            engine = PreparationEngine(
                ledger, workspace_provider=GitWorkspaceProvider(),
                providers={"portless": portless},
                owner_token="delivery-integration-owner",
            )
            launch = None
            try:
                prepared = engine.prepare(
                    runner_request(kernel, execution),
                    project={"repository_path": str(repository)},
                    preparation_config={
                        "workspace": {
                            "root": str(repository / ".worktrees"),
                            "remote": "origin", "base_ref": "main",
                            "retention": "until_terminal",
                        },
                        "providers": {"portless": {"kind": "portless"}},
                        "capabilities": {
                            "local-web": {
                                "provider": "portless", "scope": "attempt",
                                "mode": "exclusive", "config": {
                                    "service_name": "web",
                                    "command": [
                                        "python3", "-u", "-c",
                                        "import http.server,os;"
                                        "http.server.ThreadingHTTPServer("
                                        "('127.0.0.1',int(os.environ['PORT'])),"
                                        "http.server.SimpleHTTPRequestHandler"
                                        ").serve_forever()",
                                    ],
                                },
                            },
                        },
                    },
                )
                self.assertEqual(
                    "ready", prepared.disposition,
                    prepared.error or prepared.attention,
                )
                launch = prepared.launch
                self.assertEqual("DEMO-PORTLESS-1", Path(launch.workspace_path).name)
                self.assertEqual("factory/demo-portless-1", launch.branch_name)
                self.assertTrue(
                    urlsplit(launch.urls[0]).hostname.endswith(".localhost")
                )
                self.wait_for_url(launch.urls[0])
                transition = run_prepared_attempt(
                    kernel, engine, launch,
                    FakePreparedRunner([
                        RunnerResult(
                            "succeeded", "complete",
                            ({"kind": "integration", "uri": launch.urls[0]},),
                        ),
                    ]),
                    command_id="integration-complete",
                )
                self.assertEqual("Ready", transition["to_state"])
                self.assertIsNone(portless.route_inspector(launch.urls[0]))
                self.assertEqual("ready", engine.cleanup_workspace(execution).disposition)
                self.assertFalse(Path(launch.workspace_path).exists())
                self.assertEqual(main_sha, self.git(repository, "rev-parse", "HEAD"))
            finally:
                if launch is not None and Path(launch.workspace_path).exists():
                    allocation = ledger.connection.execute(
                        "SELECT status FROM resource_allocations WHERE id=?",
                        (launch.allocation_ids[0],),
                    ).fetchone()
                    if allocation and allocation["status"] != "released":
                        engine.cleanup_attempt(launch)
                    engine.cleanup_workspace(execution)
                ledger.close()


if __name__ == "__main__":
    unittest.main()
