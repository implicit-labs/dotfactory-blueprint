"""Provenance-safe Git worktree preparation and cleanup."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class WorkspaceError(RuntimeError):
    pass


class WorkspaceConflict(WorkspaceError):
    pass


class WorkspaceUnsafeCleanup(WorkspaceError):
    pass


@dataclass(frozen=True)
class WorkspaceHandle:
    repository_path: str
    git_common_dir: str
    remote: str
    base_ref: str
    base_sha: str
    branch_name: str
    path: str


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _default_run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), check=False, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not cleaned:
        raise WorkspaceError("workspace identity has no safe path characters")
    return cleaned


class GitWorkspaceProvider:
    def __init__(self, run: CommandRunner = _default_run) -> None:
        self.run = run

    def _git(self, repository: Path, *arguments: str) -> str:
        result = self.run(("git", "-C", str(repository), *arguments))
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
            raise WorkspaceError(detail)
        return result.stdout.strip()

    def _common_dir(self, repository: Path) -> Path:
        value = Path(self._git(repository, "rev-parse", "--git-common-dir"))
        if not value.is_absolute():
            value = repository / value
        return value.resolve()

    def _require_ignored_local_root(
        self, repository: Path, workspace_root: Path,
    ) -> None:
        try:
            relative = workspace_root.relative_to(repository)
        except ValueError:
            return
        if not relative.parts:
            raise WorkspaceConflict("workspace root cannot be the repository root")
        sentinel = relative / ".dotfactory-ignore-check"
        probe = self.run((
            "git", "-C", str(repository), "check-ignore", "--quiet", "--no-index",
            "--", str(sentinel),
        ))
        if probe.returncode == 1:
            raise WorkspaceConflict(
                f"repository-local workspace root is not ignored: {workspace_root}; "
                f"add /{relative.as_posix()}/ to .gitignore"
            )
        if probe.returncode != 0:
            raise WorkspaceError(
                probe.stderr.strip() or "cannot verify workspace ignore policy"
            )

    def materialize(
        self, *, repository_path: str, root: str, remote: str, base_ref: str,
        issue_identifier: str, execution_number: int,
    ) -> WorkspaceHandle:
        repository = Path(repository_path).expanduser().resolve()
        workspace_root = Path(root).expanduser().resolve()
        self._require_ignored_local_root(repository, workspace_root)
        identity = f"{_slug(issue_identifier)}-{execution_number}"
        branch = f"factory/{identity.lower()}"
        path = workspace_root / identity
        if path.exists():
            raise WorkspaceConflict(f"workspace path already exists: {path}")
        branch_probe = self.run((
            "git", "-C", str(repository), "show-ref", "--verify", "--quiet",
            f"refs/heads/{branch}",
        ))
        if branch_probe.returncode == 0:
            raise WorkspaceConflict(f"workspace branch already exists: {branch}")
        if branch_probe.returncode not in (0, 1):
            raise WorkspaceError(branch_probe.stderr.strip() or "cannot inspect branch")
        workspace_root.mkdir(parents=True, exist_ok=True)
        self._git(repository, "fetch", remote, base_ref)
        remote_ref = f"refs/remotes/{remote}/{base_ref}"
        base_sha = self._git(repository, "rev-parse", "--verify", remote_ref)
        self._git(
            repository, "worktree", "add", "-b", branch, str(path), remote_ref,
        )
        return WorkspaceHandle(
            repository_path=str(repository),
            git_common_dir=str(self._common_dir(repository)), remote=remote,
            base_ref=base_ref, base_sha=base_sha, branch_name=branch, path=str(path),
        )

    def reconcile(self, handle: WorkspaceHandle) -> WorkspaceHandle:
        path = Path(handle.path).resolve()
        if not path.is_dir():
            raise WorkspaceConflict("recorded workspace is missing")
        common = self._common_dir(path)
        if str(common) != str(Path(handle.git_common_dir).resolve()):
            raise WorkspaceConflict("workspace Git common directory changed")
        branch = self._git(path, "branch", "--show-current")
        if branch != handle.branch_name:
            raise WorkspaceConflict("workspace branch changed")
        origin = self._git(path, "rev-list", "--max-parents=0", "HEAD")
        if not origin:
            raise WorkspaceConflict("workspace history is unreadable")
        return handle

    def cleanup(self, handle: WorkspaceHandle) -> None:
        self.reconcile(handle)
        path = Path(handle.path).resolve()
        status = self._git(path, "status", "--porcelain=v1", "--untracked-files=all")
        if status:
            raise WorkspaceUnsafeCleanup("workspace is dirty; retain and quarantine it")
        repository = Path(handle.repository_path).resolve()
        self._git(repository, "worktree", "remove", str(path))
