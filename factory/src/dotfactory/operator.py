"""Owner-only local operator transport; the runtime remains the sole writer."""

from __future__ import annotations

import getpass
import json
import os
import socket
import stat
from pathlib import Path
from typing import Any

from .control import ObservationService, Principal


MAX_MESSAGE = 65536


def socket_path(ledger_path: Path) -> Path:
    return ledger_path.with_suffix(".control.sock")


def _read(connection: socket.socket) -> dict[str, Any]:
    payload = bytearray()
    while b"\n" not in payload:
        chunk = connection.recv(min(4096, MAX_MESSAGE + 1 - len(payload)))
        if not chunk:
            raise ValueError("incomplete operator request")
        payload.extend(chunk)
        if len(payload) > MAX_MESSAGE:
            raise ValueError("operator message exceeds 65536 bytes")
    raw, trailing = payload.split(b"\n", 1)
    if trailing:
        raise ValueError("only one operator request is allowed")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("operator message must be an object")
    return value


class OperatorServer:
    """Call pump on the ledger thread, including from the runner heartbeat."""

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.path = socket_path(runtime.ledger.path)
        if self.path.exists() or self.path.is_symlink():
            existing = self.path.lstat()
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                raise ValueError("operator path is not an owned socket")
            # The caller already holds the exclusive ledger lock.
            self.path.unlink()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.path))
            self.path.chmod(0o600)
            self.inode = self.path.stat().st_ino
            self.listener.listen(8)
            self.listener.setblocking(False)
        except Exception:
            self.listener.close()
            raise

    def dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("operation") == "http":
            from .control_server import dispatch_http
            return dispatch_http(self.runtime, message)
        project = message.get("project")
        if project not in self.runtime.kernels:
            raise ValueError("project is not selected by this runtime")
        observation = ObservationService(self.runtime.ledger, self.runtime.kernels[project])
        operation = message.get("operation")
        execution = message.get("execution")
        if execution:
            current = self.runtime.ledger.current(str(execution))
            if current["project_key"] != project:
                raise ValueError("execution is outside this project")
        if operation == "status":
            return observation.run(str(execution)) if execution else observation.runs(project_key=project)
        if operation == "artifacts" and execution:
            return observation.artifacts(str(execution))
        if operation == "delivery" and execution:
            return observation.delivery(str(execution))
        if operation == "drain":
            self.runtime.drain_requested = True
            return {"draining": True}
        if operation == "command" and execution:
            # Filesystem identity is authority. Never accept a caller-supplied role.
            return self.runtime.control_service(project).execute(
                str(execution), command_id=str(message.get("command_id", "")),
                principal=Principal(getpass.getuser(), "approver", "local-socket"),
                request=message.get("request", {}),
            )
        raise ValueError("unsupported operator operation")

    def pump(self) -> None:
        for _ in range(4):
            try:
                connection, _address = self.listener.accept()
            except BlockingIOError:
                return
            with connection:
                connection.settimeout(0.1)
                try:
                    response = {"ok": True, "data": self.dispatch(_read(connection))}
                except Exception as error:
                    response = {"ok": False, "error": str(error)}
                try:
                    encoded = json.dumps(response, sort_keys=True).encode() + b"\n"
                    if len(encoded) > MAX_MESSAGE:
                        encoded = b'{"ok":false,"error":"response too large; narrow the query"}\n'
                    connection.sendall(encoded)
                except OSError:
                    # An accepted command stays committed if its client disconnects.
                    pass

    def close(self) -> None:
        self.listener.close()
        if self.path.exists() and self.path.lstat().st_ino == self.inode:
            self.path.unlink()


def send(path: Path, message: dict[str, Any], *, timeout: float = 10) -> dict[str, Any]:
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ValueError("operator endpoint must be an owned socket")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("operator socket must have mode 0600")
    encoded = json.dumps(message).encode() + b"\n"
    if len(encoded) > MAX_MESSAGE:
        raise ValueError("operator request exceeds 65536 bytes")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(encoded)
        return _read(connection)
