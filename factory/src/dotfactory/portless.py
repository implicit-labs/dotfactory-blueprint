"""Owned Portless process provider for stable loopback development URLs."""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .resources import (
    CapabilityPlan, PreparationNeedsAttention, ProviderActivation,
)
from .workspace import WorkspaceHandle


PORT_PATTERN = re.compile(r"\bPORT=(\d+)\b")
URL_PATTERN = re.compile(r"\bPORTLESS_URL=(https?://[^\s]+)")
LIST_PID_PATTERN = re.compile(r"\bpid\s+(\d+)\b", re.IGNORECASE)
BANNED_FLAGS = {
    "--force", "--lan", "--tailscale", "--funnel", "--ngrok",
    "--tunnel", "--wildcard", "--tld", "--cert", "--key", "--no-tls",
}
RESERVED_NAMES = {
    "run", "get", "alias", "hosts", "list", "doctor", "trust", "clean",
    "prune", "proxy", "service",
}


@dataclass
class LivePortlessHandle:
    process: subprocess.Popen[str] | None
    pid: int
    process_identity: str
    owner_token: str
    resource_id: str


def _process_identity(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


class PortlessProvider:
    def __init__(
        self, *, command: str = "portless", version: str = "0.15.6",
        node_minimum: int = 24,
        preflight_command: str = "dotfactory-portless-preflight",
        startup_timeout_seconds: float = 15.0,
        process_identity: Callable[[int], str | None] = _process_identity,
        route_inspector: Callable[[str], Mapping[str, Any] | None] | None = None,
        kill_process_group: Callable[[int, int], None] = os.killpg,
    ) -> None:
        self.command = command
        self.version = version
        self.node_minimum = node_minimum
        self.preflight_command = preflight_command
        self.startup_timeout_seconds = startup_timeout_seconds
        self.process_identity = process_identity
        self.route_inspector = route_inspector or self._inspect_route
        self.kill_process_group = kill_process_group
        self._active: dict[str, ProviderActivation] = {}

    def _inspect_route(self, url: str) -> Mapping[str, Any] | None:
        result = subprocess.run(
            [self.command, "list"], check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "CI": "1"},
        )
        if result.returncode != 0:
            raise PreparationNeedsAttention(
                "Portless routes cannot be inspected", category="unhealthy",
                detail={"last_safe_step": "route inspection",
                        "allowed_actions": ["retry", "retain", "quarantine", "cancel"]},
                provider="portless",
            )
        for line in result.stdout.splitlines():
            if url not in line:
                continue
            pid = LIST_PID_PATTERN.search(line)
            return {"url": url, "pid": int(pid.group(1)) if pid else None}
        return None

    def plan(
        self, *, capability: str, config: Mapping[str, Any],
        workspace: WorkspaceHandle,
    ) -> CapabilityPlan:
        service = config.get("service_name")
        command = config.get("command")
        if not isinstance(service, str) or not service.strip():
            raise PreparationNeedsAttention(
                "Portless service_name is missing", category="unavailable",
                detail={"last_safe_step": "provider plan",
                        "allowed_actions": ["cancel"]},
                capability=capability, provider="portless",
            )
        if service in RESERVED_NAMES:
            raise PreparationNeedsAttention(
                "Portless service name is reserved", category="conflict",
                detail={"last_safe_step": "provider plan",
                        "allowed_actions": ["cancel"]},
                capability=capability, provider="portless",
            )
        if (
            not isinstance(command, list) or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise PreparationNeedsAttention(
                "Portless launch command is missing", category="unavailable",
                detail={"last_safe_step": "provider plan",
                        "allowed_actions": ["cancel"]},
                capability=capability, provider="portless",
            )
        forbidden = sorted(
            item for item in command
            if any(item == flag or item.startswith(flag + "=") for flag in BANNED_FLAGS)
        )
        if forbidden:
            raise PreparationNeedsAttention(
                "Portless exposure or takeover flags are not allowed",
                category="unauthorized",
                detail={"last_safe_step": "provider plan", "rejected_flags": forbidden,
                        "allowed_actions": ["cancel"]},
                capability=capability, provider="portless",
            )
        resource_id = f"portless:{workspace.branch_name}:{service}"
        return CapabilityPlan(
            provider="portless", capability=capability, scope="attempt",
            resource_id=resource_id, target=service,
            config={"service_name": service, "command": tuple(command)},
        )

    def _preflight(self) -> None:
        result = subprocess.run(
            [self.preflight_command, "--portless", self.command,
             "--version", self.version, "--node-minimum", str(self.node_minimum)],
            check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "CI": "1"},
        )
        if result.returncode != 0:
            raise PreparationNeedsAttention(
                "Portless preflight failed", category="unhealthy",
                detail={"last_safe_step": "preflight",
                        "allowed_actions": ["retry", "cancel"],
                        "remediation_command": self.preflight_command},
                provider="portless",
            )

    def activate(
        self, plan: CapabilityPlan, *, workspace: WorkspaceHandle,
        owner_token: str,
    ) -> ProviderActivation:
        self._preflight()
        service = str(plan.config["service_name"])
        child_command = tuple(str(item) for item in plan.config["command"])
        environment = dict(os.environ)
        environment.update({
            "CI": "1", "PORTLESS_LAN": "0", "PORTLESS_WILDCARD": "0",
            "PORTLESS_TAILSCALE": "0", "PORTLESS_FUNNEL": "0",
            "PORTLESS_NGROK": "0", "PORTLESS_TLD": "localhost",
        })
        process = subprocess.Popen(
            [self.command, "run", "--name", service, *child_command],
            cwd=workspace.path, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        output = ""
        selector = selectors.DefaultSelector()
        if process.stdout is None:
            process.terminate()
            raise RuntimeError("Portless output pipe was not created")
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + self.startup_timeout_seconds
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise PreparationNeedsAttention(
                        "Portless child exited during launch", category="unhealthy",
                        detail={"last_safe_step": "child launch",
                                "allowed_actions": ["retry", "cancel"]},
                        capability=plan.capability, provider="portless",
                    )
                events = selector.select(timeout=min(0.25, deadline - time.monotonic()))
                for key, _ in events:
                    line = key.fileobj.readline()
                    output = (output + line)[-8192:]
                port_match = PORT_PATTERN.search(output)
                url_match = URL_PATTERN.search(output)
                if port_match and url_match:
                    url = url_match.group(1).rstrip(".,;)")
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme not in ("http", "https")
                        or not parsed.hostname
                        or not (
                            parsed.hostname == "localhost"
                            or parsed.hostname.endswith(".localhost")
                        )
                    ):
                        raise PreparationNeedsAttention(
                            "Portless returned a non-loopback route", category="unauthorized",
                            detail={"last_safe_step": "route validation",
                                    "allowed_actions": ["cancel"]},
                            capability=plan.capability, provider="portless",
                        )
                    identity = self.process_identity(process.pid)
                    if not identity:
                        raise PreparationNeedsAttention(
                            "Portless process identity cannot be recorded",
                            category="unsafe-cleanup",
                            detail={"last_safe_step": "process identity",
                                    "allowed_actions": ["retain", "quarantine", "cancel"]},
                            capability=plan.capability, provider="portless",
                        )
                    handle = LivePortlessHandle(
                        process, process.pid, identity, owner_token, plan.resource_id,
                    )
                    activation = ProviderActivation(
                        resource_id=plan.resource_id,
                        environment=(("PORT", port_match.group(1)),
                                     ("PORTLESS_URL", url)),
                        commands=((self.command, "run", "--name", service, *child_command),),
                        urls=(url,), metadata={"pid": process.pid,
                                               "process_identity": identity,
                                               "service_name": service,
                                               "app_port": int(port_match.group(1)),
                                               "route_url": url},
                        handle=handle,
                    )
                    self._active[plan.resource_id] = activation
                    return activation
            raise PreparationNeedsAttention(
                "Portless did not report a route before the startup deadline",
                category="unhealthy",
                detail={"last_safe_step": "route wait",
                        "allowed_actions": ["retry", "cancel"]},
                capability=plan.capability, provider="portless",
            )
        except Exception:
            if process.poll() is None:
                self.kill_process_group(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.kill_process_group(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            raise
        finally:
            selector.close()

    def reconcile(
        self, allocation: Mapping[str, Any], *, workspace: WorkspaceHandle,
        owner_token: str,
    ) -> ProviderActivation:
        resource_id = str(allocation["resource_id"])
        activation = self._active.get(resource_id)
        if activation:
            handle = activation.handle
            if (
                isinstance(handle, LivePortlessHandle)
                and handle.owner_token == owner_token
                and self.process_identity(handle.pid) == handle.process_identity
            ):
                return activation
        metadata = allocation.get("metadata", {})
        pid_value = metadata.get("pid") if isinstance(metadata, Mapping) else None
        identity = metadata.get("process_identity") if isinstance(metadata, Mapping) else None
        url = metadata.get("route_url") if isinstance(metadata, Mapping) else None
        app_port = metadata.get("app_port") if isinstance(metadata, Mapping) else None
        if not isinstance(pid_value, int) or not isinstance(identity, str) or not isinstance(url, str):
            raise PreparationNeedsAttention(
                "Portless allocation lacks a durable process identity",
                category="unsafe-cleanup",
                detail={"last_safe_step": "process reconciliation",
                        "allowed_actions": ["retry", "retain", "quarantine", "cancel"]},
                capability=str(allocation["capability"]), provider="portless",
            )
        observed_identity = self.process_identity(pid_value)
        route = self.route_inspector(url)
        if observed_identity is None and route is None:
            raise PreparationNeedsAttention(
                "Portless process and route are no longer present",
                category="unhealthy",
                detail={"last_safe_step": "process reconciliation",
                        "orphan_state": "absent",
                        "allowed_actions": ["retry", "release", "cancel"]},
                capability=str(allocation["capability"]), provider="portless",
            )
        if observed_identity is None and route is not None:
            state = "route-without-process"
        elif observed_identity != identity:
            state = "pid-identity-mismatch"
        elif route is None:
            state = "process-without-route"
        elif route.get("pid") != pid_value:
            state = "route-owner-mismatch"
        else:
            handle = LivePortlessHandle(
                None, pid_value, identity, owner_token, resource_id,
            )
            activation = ProviderActivation(
                resource_id=resource_id,
                environment=(("PORT", str(app_port)), ("PORTLESS_URL", url)),
                urls=(url,), metadata=dict(metadata), handle=handle,
            )
            self._active[resource_id] = activation
            return activation
        raise PreparationNeedsAttention(
            f"Portless orphan state requires operator attention: {state}",
            category="unsafe-cleanup",
            detail={"last_safe_step": "process and route reconciliation",
                    "orphan_state": state,
                    "allowed_actions": ["retain", "quarantine", "release", "cancel"]},
            capability=str(allocation["capability"]), provider="portless",
        )

    def cleanup(
        self, activation: ProviderActivation, *, owner_token: str,
    ) -> Mapping[str, Any]:
        handle = activation.handle
        process = handle.process if isinstance(handle, LivePortlessHandle) else None
        if (
            isinstance(handle, LivePortlessHandle)
            and handle.owner_token == owner_token
            and handle.resource_id == activation.resource_id
            and process is not None and process.poll() is not None
        ):
            route = self.route_inspector(activation.urls[0]) if activation.urls else None
            if route is not None:
                raise PreparationNeedsAttention(
                    "Portless route remained after its process exited",
                    category="unsafe-cleanup",
                    detail={"last_safe_step": "cleanup ownership check",
                            "orphan_state": "route-without-process",
                            "allowed_actions": ["retain", "quarantine", "release"]},
                    provider="portless",
                )
            if process.stdout is not None:
                process.stdout.close()
            self._active.pop(activation.resource_id, None)
            return {"pid": handle.pid, "returncode": process.returncode}
        if (
            not isinstance(handle, LivePortlessHandle)
            or handle.owner_token != owner_token
            or handle.resource_id != activation.resource_id
            or self.process_identity(handle.pid) != handle.process_identity
        ):
            raise PreparationNeedsAttention(
                "refusing to stop an unowned Portless process",
                category="unsafe-cleanup",
                detail={"last_safe_step": "cleanup ownership check",
                        "allowed_actions": ["retain", "quarantine"]},
                provider="portless",
            )
        process = handle.process
        if self.process_identity(handle.pid) == handle.process_identity:
            self.kill_process_group(handle.pid, signal.SIGTERM)
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.kill_process_group(handle.pid, signal.SIGKILL)
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
        else:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.process_identity(handle.pid) != handle.process_identity:
                    break
                time.sleep(0.05)
            else:
                self.kill_process_group(handle.pid, signal.SIGKILL)
        route = self.route_inspector(activation.urls[0]) if activation.urls else None
        if route is not None and route.get("pid") in (None, handle.pid):
            raise PreparationNeedsAttention(
                "Portless route remained after the owned process stopped",
                category="unsafe-cleanup",
                detail={"last_safe_step": "route cleanup",
                        "orphan_state": "route-without-process",
                        "allowed_actions": ["retain", "quarantine", "release"]},
                provider="portless",
            )
        self._active.pop(activation.resource_id, None)
        return {
            "pid": handle.pid,
            "returncode": process.returncode if process is not None else None,
        }
