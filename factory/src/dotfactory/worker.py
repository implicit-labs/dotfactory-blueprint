"""Worker-side, stdlib-only protocol. Run on a personal SSH host or locally."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024
SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,160}$")
API_ENV = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class WorkerError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def command(argv, *, cwd=None, env=None, timeout=60):
    result = subprocess.run(argv, cwd=cwd, env=env, input=None, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise WorkerError("command failed: " + Path(argv[0]).name)
    return result.stdout.strip()


def git(path, *args, env=None):
    return command(["git", "-C", str(path), *args], env=env)


def snapshot(path):
    """Commit nonignored task files without changing the caller's index or HEAD."""
    with tempfile.TemporaryDirectory() as temporary:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / "index"),
                   GIT_AUTHOR_NAME="dotfactory", GIT_AUTHOR_EMAIL="worker@dotfactory.local",
                   GIT_COMMITTER_NAME="dotfactory", GIT_COMMITTER_EMAIL="worker@dotfactory.local")
        parent = git(path, "rev-parse", "HEAD")
        git(path, "read-tree", "HEAD", env=env)
        git(path, "add", "-A", "--", ".", env=env)
        tree = git(path, "write-tree", env=env)
        if tree == git(path, "rev-parse", "HEAD^{tree}"):
            return parent
        return git(path, "commit-tree", tree, "-p", parent, "-m", "Factory stage snapshot", env=env)


def bundle(path, sha):
    with tempfile.TemporaryDirectory() as temporary:
        ref = "refs/dotfactory/export-" + sha
        git(path, "update-ref", ref, sha)
        try:
            target = Path(temporary) / "source.bundle"
            git(path, "bundle", "create", str(target), ref)
            data = target.read_bytes()
            if len(data) > MAX_BYTES // 2:
                raise WorkerError("source bundle exceeds transfer limit")
            return base64.b64encode(data).decode()
        finally:
            git(path, "update-ref", "-d", ref, sha)


def import_bundle(path, encoded, sha):
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise WorkerError("invalid source SHA")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > MAX_BYTES // 2:
        raise WorkerError("source bundle exceeds transfer limit")
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "source.bundle"
        source.write_bytes(data)
        git(path, "bundle", "verify", str(source))
        git(path, "fetch", str(source), "refs/dotfactory/export-" + sha)
        if git(path, "rev-parse", "FETCH_HEAD") != sha:
            raise WorkerError("source SHA mismatch")


def environment(billing, kind, names=()):
    if billing not in ("subscription", "api"):
        raise WorkerError("billing method must be explicit")
    env = {key: value for key, value in os.environ.items()
           if key in ("HOME", "PATH", "USER", "LANG", "LC_ALL", "TMPDIR", "SSH_AUTH_SOCK",
                      "CODEX_HOME", "CLAUDE_CONFIG_DIR")}
    for name in names:
        if name not in os.environ:
            raise WorkerError("missing worker environment reference: " + name)
        env[name] = os.environ[name]
    if billing == "subscription":
        for name in API_ENV:
            env.pop(name, None)
        if kind == "claude-code" and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    else:
        name = "OPENAI_API_KEY" if kind == "codex" else "ANTHROPIC_API_KEY"
        if not os.environ.get(name):
            raise WorkerError("missing worker API key for selected billing")
        env[name] = os.environ[name]
    return env


def probe(spec):
    kind = spec["kind"]
    if kind not in ("codex", "claude-code"):
        raise WorkerError("worker supports Codex and Claude Code")
    executable = spec["command"]
    env = environment(spec["billing"], kind, spec.get("environment_envs", []))
    facts = ["os:" + platform.system().lower(), "arch:" + platform.machine().lower()]
    missing = []
    for requirement in spec.get("requires", []):
        if requirement.startswith("tool:"):
            if shutil.which(requirement[5:], path=env.get("PATH")):
                facts.append(requirement)
            else:
                missing.append(requirement)
        elif requirement not in facts:
            missing.append(requirement)
    version = command([executable, "--version"], env=env, timeout=15)
    if kind == "codex":
        login = subprocess.run([executable, "login", "status"], env=env,
                               capture_output=True, text=True, timeout=15)
        expected = "chatgpt" if spec["billing"] == "subscription" else "api key"
        if login.returncode or expected not in (login.stdout + login.stderr).lower():
            missing.append("auth:" + expected.replace(" ", "-"))
    else:
        login = subprocess.run([executable, "auth", "status", "--json"], env=env,
                               capture_output=True, text=True, timeout=15)
        try:
            status = json.loads(login.stdout)
        except ValueError:
            status = {}
        methods = ("oauth_token", "claude.ai") if spec["billing"] == "subscription" else ("api_key",)
        if login.returncode or not status.get("loggedIn") or status.get("authMethod") not in methods:
            missing.append("auth:claude-" + spec["billing"])
    return {"available": not missing, "missing": missing, "facts": sorted(facts), "version": version[:160]}


def workspace(spec):
    identity = spec["identity"]
    if not SAFE_ID.fullmatch(identity):
        raise WorkerError("invalid attempt identity")
    root = Path(spec["root"]).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = root / identity
    if directory.is_symlink():
        raise WorkerError("worker directory must not be a symlink")
    return directory


def write_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def prepare(spec):
    directory = workspace(spec)
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / "prepare.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = directory / "manifest.json"
        identity = {key: spec[key] for key in ("identity", "source_sha", "binding")}
        if record.exists():
            if json.loads(record.read_text()) != identity:
                raise WorkerError("existing worker allocation has a different binding")
            return {"workspace": str(directory / "repo")}
        repo = directory / "repo"
        if repo.exists():
            raise WorkerError("incomplete allocation retained; inspect before retry")
        command(["git", "init", str(repo)])
        import_bundle(repo, spec["bundle"], spec["source_sha"])
        git(repo, "checkout", "--detach", spec["source_sha"])
        (directory / "result.schema.json").write_text(json.dumps(spec["schema"]))
        # Only explicit, resolved skill packages cross the boundary.
        for item in spec.get("skills", []):
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise WorkerError("invalid skill path")
            data = base64.b64decode(item["data"], validate=True)
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise WorkerError("skill content hash mismatch")
            dest = directory / "skills" / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(item["mode"] & 0o777)
        write_json(record, identity)
        return {"workspace": str(repo)}


def validate_binding(spec):
    directory = workspace(spec)
    stored = json.loads((directory / "manifest.json").read_text())
    if stored["binding"] != spec["binding"]:
        raise WorkerError("worker binding mismatch")
    return directory


def execute(spec):
    directory = validate_binding(spec)
    env = environment(spec["billing"], spec["kind"], spec.get("environment_envs", []))
    readiness = probe(dict(spec, command=spec["argv"][0], requires=[]))
    if not readiness["available"]:
        raise WorkerError("worker authentication changed after preparation")
    with (directory / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkerError("attempt is already running")
        marker = directory / "started.json"
        if marker.exists():
            raise WorkerError("attempt already started; reconcile retained work before retry")
        write_json(marker, {"input_digest": digest(spec), "started_at": time.time()})
        payload = base64.b64decode(spec["input"], validate=True)
        process = subprocess.Popen(spec["argv"], cwd=directory / "repo", env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        write_json(directory / "lease.json", {"renewed_at": time.time(), "cancel": False})
        def terminate(_signal=None, _frame=None):
            # Descendants can retain pipes after the native parent exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(signum, terminate)
        try:
            code = stream_process(process, payload, directory, env, spec["timeout_seconds"], terminate)
            write_json(directory / "exit.json", {"exit_code": code})
            return code
        finally:
            terminate()


def stream_process(process, payload, directory, env, timeout, terminate):
    """Enforce the watchdog during blocked input, output and inherited-pipe drain."""
    streams = []
    failed = False
    drain_deadline = None
    started = time.monotonic()
    offset = 0
    secrets = [value.encode() for key, value in env.items()
               if any(word in key for word in ("TOKEN", "KEY", "SECRET", "PASSWORD")) and value]
    os.set_blocking(process.stdin.fileno(), False)
    for source, target, name in ((process.stdout, sys.stdout.buffer, "stdout"),
                                 (process.stderr, sys.stderr.buffer, "stderr")):
        os.set_blocking(source.fileno(), False)
        fd = target.fileno()
        blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
        streams.append({"source": source, "fd": fd, "blocking": blocking,
                        "raw": bytearray(), "pending": bytearray(), "total": 0,
                        "log": (directory / name).open("wb")})
    try:
        while process.poll() is None or any(item["source"] or item["pending"] for item in streams):
            now = time.monotonic()
            lease = json.loads((directory / "lease.json").read_text())
            expired = lease["cancel"] or time.time() - lease["renewed_at"] > 60 or now - started > timeout
            if (expired or failed) and drain_deadline is None:
                failed = True
                terminate()
                drain_deadline = now + 1
            if drain_deadline is not None and now >= drain_deadline:
                break
            if not process.stdin.closed:
                try:
                    if offset < len(payload):
                        offset += os.write(process.stdin.fileno(), payload[offset:offset + 65536])
                    if offset == len(payload):
                        process.stdin.close()
                except BlockingIOError:
                    pass
                except BrokenPipeError:
                    failed = failed or offset != len(payload)
                    process.stdin.close()
            for item in streams:
                source = item["source"]
                if source is not None:
                    try:
                        data = os.read(source.fileno(), 65536)
                    except BlockingIOError:
                        data = None
                    if data is not None:
                        item["total"] += len(data)
                        if item["total"] > MAX_BYTES:
                            failed = True
                            source.close()
                            item["source"] = None
                            item["raw"].clear()
                        else:
                            item["raw"].extend(data)
                            boundary = item["raw"].rfind(b"\n") + 1 if data else len(item["raw"])
                            if boundary:
                                safe = bytes(item["raw"][:boundary])
                                del item["raw"][:boundary]
                                for secret in secrets:
                                    safe = safe.replace(secret, b"[REDACTED]")
                                item["log"].write(safe)
                                item["pending"].extend(safe)
                            if not data:
                                source.close()
                                item["source"] = None
                if item["pending"]:
                    try:
                        sent = os.write(item["fd"], item["pending"][:65536])
                        del item["pending"][:sent]
                    except BlockingIOError:
                        pass
                    except BrokenPipeError:
                        failed = True
                        item["pending"].clear()
            time.sleep(0.01)
        code = process.wait(timeout=2)
        return code if code else (1 if failed else 0)
    finally:
        terminate()
        process.stdin.close()
        for item in streams:
            if item["source"] is not None:
                item["source"].close()
            item["log"].close()
            os.set_blocking(item["fd"], item["blocking"])


def collect(spec):
    directory = validate_binding(spec)
    result = directory / "handoff.json"
    if result.exists():
        return json.loads(result.read_text())
    exit_code = json.loads((directory / "exit.json").read_text())["exit_code"]
    if exit_code != 0:
        raise WorkerError("worker did not exit successfully; retain workspace")
    repo = directory / "repo"
    env = environment(spec["billing"], spec["kind"], spec.get("environment_envs", []))
    checks = []
    for argv in spec["checks"]:
        result_command = subprocess.run(argv, cwd=repo, env=env, capture_output=True,
                                        timeout=spec["check_timeout_seconds"])
        checks.append({"argv": argv, "exit_code": result_command.returncode})
        if result_command.returncode:
            raise WorkerError("required verification command failed: " + argv[0])
    sha = snapshot(repo)
    output = {"binding": spec["binding"], "source_sha": spec["source_sha"],
              "output_sha": sha, "bundle": bundle(repo, sha), "checks": checks}
    write_json(directory / "handoff.json", output)
    return output


def dispatch(spec):
    op = spec["op"]
    if op == "probe":
        return probe(spec)
    if op == "prepare":
        return prepare(spec)
    if op == "collect":
        return collect(spec)
    if op in ("renew", "cancel"):
        directory = validate_binding(spec)
        write_json(directory / "lease.json", {"renewed_at": time.time(), "cancel": op == "cancel"})
        return {"ok": True}
    if op == "command":
        directory = validate_binding(spec)
        env = environment(spec["billing"], spec["kind"], spec.get("environment_envs", []))
        return {"stdout": command(spec["argv"], cwd=directory / "repo", env=env, timeout=20)}
    raise WorkerError("unknown worker operation")


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise WorkerError("worker request exceeds byte limit")
        spec = json.loads(raw)
        if spec["op"] == "exec":
            return execute(spec)
        print(json.dumps(dispatch(spec)))
        return 0
    except Exception as error:
        # Never echo transport payloads, native CLI stderr, or credentials.
        print(json.dumps({"error": str(error) if isinstance(error, WorkerError) else type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
