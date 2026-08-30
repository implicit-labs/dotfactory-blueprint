"""Versioned routing and protocol contracts for live agent runners."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .ledger import SQLiteLedger, StaleAttempt
from .resources import PreparationError, PreparedLaunch
from .runner import RunnerNeedsAttention, RunnerResult


RUNNER_KINDS = {"codex", "claude-code", "omp-rpc"}
EVENT_KINDS = {
    "session", "assistant", "tool_call", "tool_result", "usage",
    "approval", "input", "warning", "error", "terminal", "protocol",
}
RESULT_MARKER = "dotfactory_result"


class RunnerProtocolError(PreparationError):
    pass


@dataclass(frozen=True)
class RunnerRoute:
    name: str
    kind: str
    command: str
    minimum_version: str
    permission_mode: str
    capabilities: tuple[str, ...] = ()
    profile: str | None = None
    environment_envs: tuple[str, ...] = ()
    silence_timeout_seconds: int = 300
    termination_grace_seconds: int = 5
    maximum_frame_bytes: int = 1024 * 1024
    maximum_reassembled_frame_bytes: int = 64 * 1024 * 1024
    maximum_events: int = 10000
    maximum_payload_bytes: int = 256 * 1024


@dataclass(frozen=True)
class RunnerCapabilityReport:
    runner: str
    kind: str
    available: bool
    version: str | None
    capabilities: tuple[str, ...]
    reason: str | None = None

    def supports(self, capability: str) -> bool:
        return self.available and capability in self.capabilities


@dataclass(frozen=True)
class RunnerEvent:
    kind: str
    protocol_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source_occurred_at: str | None = None
    observed_at: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    origin: str = "provider"
    trust_class: str = "untrusted-provider"
    stream: str = "stdout"
    sequence: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise RunnerProtocolError(f"unknown normalized runner event: {self.kind}")


@dataclass(frozen=True)
class RunnerReceipt:
    protocol_version: int
    runner: str
    adapter_kind: str
    session_id: str | None
    terminal_type: str
    events: tuple[RunnerEvent, ...]
    result: RunnerResult
    runner_run_id: str | None = None
    execution_id: str | None = None
    attempt_id: str | None = None
    execution_trace_id: str | None = None
    trace_id: str | None = None
    root_span_id: str | None = None
    adapter_version: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    exit_code: int | None = None


class RunnerExecutionError(RunnerProtocolError):
    pass


class RunnerProviderError(RunnerExecutionError):
    pass


class RunnerCanceled(RunnerExecutionError):
    pass


class RunnerTimedOut(RunnerExecutionError):
    pass


def _strict_object(line: str, *, maximum_bytes: int = 1024 * 1024) -> dict[str, Any]:
    encoded = line.encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise RunnerProtocolError("runner protocol frame exceeds the byte limit")
    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        raise RunnerProtocolError("runner emitted malformed JSON") from error
    if not isinstance(value, dict):
        raise RunnerProtocolError("runner protocol frame must be an object")
    return value


def _session_id(value: Mapping[str, Any]) -> str | None:
    for key in (
        "session_file", "sessionFile", "session_id", "sessionId",
        "thread_id", "conversation_id",
    ):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _source_timestamp(value: Mapping[str, Any]) -> str | None:
    for key in ("occurred_at", "created_at", "timestamp", "time"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _replace_sensitive(value: Any, sensitive: Sequence[str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_sensitive(item, sensitive) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_sensitive(item, sensitive) for item in value]
    if isinstance(value, str):
        redacted = value
        for item in sensitive:
            if len(item) >= 4:
                redacted = redacted.replace(item, "[REDACTED]")
        return redacted
    return value


def _durable_event_payload(
    frame: Mapping[str, Any], event: RunnerEvent, *, sensitive: Sequence[str]
) -> dict[str, Any]:
    encoded = json.dumps(frame, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload: dict[str, Any] = {
        "protocol_type": event.protocol_type,
        "original_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    for key in (
        "id", "command", "success", "code", "subtype", "is_error",
        "isTerminal", "toolCallId", "toolName", "method",
    ):
        item = frame.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            payload[key] = item
    session = _session_id(frame)
    if session:
        payload["session"] = session
    usage = frame.get("usage")
    if isinstance(usage, dict):
        payload["usage"] = usage
    if event.kind == "error":
        for key in ("error", "message"):
            item = frame.get(key)
            if isinstance(item, str):
                payload["excerpt"] = item[:2048]
                break
    item = frame.get("item")
    if isinstance(item, dict):
        for key in ("id", "type", "status", "name"):
            if isinstance(item.get(key), (str, int, float, bool)):
                payload[f"item_{key}"] = item[key]
    return _replace_sensitive(payload, sensitive)


def _operation_id(frame: Mapping[str, Any]) -> str | None:
    item = frame.get("item")
    if isinstance(item, dict):
        for key in ("id", "call_id", "tool_call_id", "toolCallId"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("toolCallId", "tool_call_id", "call_id", "id"):
        value = frame.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _duration_seconds(value: Any) -> float:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if not isinstance(value, str):
        raise RunnerProtocolError("runner timeout must be a positive duration")
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", value.strip())
    if not match:
        raise RunnerProtocolError("runner timeout must look like 30s, 15m, 2h, or 1d")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return float(int(match.group(1)) * units[match.group(2)])


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"[0-9]+(?:\.[0-9]+)+", value)
    if not match:
        raise RunnerProtocolError(f"runner version is not parseable: {value}")
    return tuple(int(item) for item in match.group(0).split("."))


class OmpRpcFrameDecoder:
    def __init__(
        self, *, maximum_frame_bytes: int = 1024 * 1024,
        maximum_reassembled_frame_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.maximum_frame_bytes = maximum_frame_bytes
        self.maximum_reassembled_frame_bytes = maximum_reassembled_frame_bytes
        self.chunk_id: str | None = None
        self.count = 0
        self.byte_length = 0
        self.parts: list[bytes] = []
        self.total_bytes = 0

    def feed(self, line: str) -> tuple[dict[str, Any], ...]:
        frame = _strict_object(line, maximum_bytes=self.maximum_frame_bytes)
        if self.chunk_id is not None and frame.get("type") != "rpc_chunk":
            raise RunnerProtocolError("omp-rpc chunk sequence was interrupted")
        if frame.get("type") != "rpc_chunk":
            return (frame,)
        chunk_id = frame.get("chunkId")
        index = frame.get("index")
        count = frame.get("count")
        byte_length = frame.get("byteLength")
        data = frame.get("data")
        if (
            not isinstance(chunk_id, str) or not chunk_id
            or not isinstance(index, int) or index < 0
            or not isinstance(count, int) or count < 1 or count > 65536
            or not isinstance(byte_length, int) or byte_length < 0
            or not isinstance(data, str)
        ):
            raise RunnerProtocolError("omp-rpc chunk metadata is invalid")
        if byte_length > self.maximum_reassembled_frame_bytes:
            raise RunnerProtocolError("omp-rpc reassembled frame exceeds the byte limit")
        if self.chunk_id is None:
            if index != 0:
                raise RunnerProtocolError("omp-rpc chunk sequence does not start at zero")
            self.chunk_id = chunk_id
            self.count = count
            self.byte_length = byte_length
        if (
            chunk_id != self.chunk_id or count != self.count
            or byte_length != self.byte_length or index != len(self.parts)
        ):
            raise RunnerProtocolError("omp-rpc chunk sequence is interleaved or out of order")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as error:
            raise RunnerProtocolError("omp-rpc chunk data is not valid base64") from error
        self.parts.append(decoded)
        self.total_bytes += len(decoded)
        if self.total_bytes > self.maximum_reassembled_frame_bytes:
            raise RunnerProtocolError("omp-rpc reassembled frame exceeds the byte limit")
        if len(self.parts) < self.count:
            return ()
        if len(self.parts) != self.count or self.total_bytes != self.byte_length:
            raise RunnerProtocolError("omp-rpc chunk byte length does not match")
        raw = b"".join(self.parts)
        try:
            logical = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise RunnerProtocolError("omp-rpc chunk payload is not strict UTF-8") from error
        self.chunk_id = None
        self.count = 0
        self.byte_length = 0
        self.parts = []
        self.total_bytes = 0
        return (_strict_object(
            logical, maximum_bytes=self.maximum_reassembled_frame_bytes
        ),)

    def finish(self) -> None:
        if self.chunk_id is not None:
            raise RunnerProtocolError("omp-rpc chunk sequence ended incomplete")


def _text_candidates(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _text_candidates(item)
    elif isinstance(value, dict):
        for key in ("structured_output", "result", "text", "output_text"):
            if key in value:
                yield from _text_candidates(value[key])
        for key in ("message", "messages", "content", "item"):
            if key in value:
                yield from _text_candidates(value[key])


def _result_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and value.get(RESULT_MARKER) == 1:
        return value
    if isinstance(value, dict):
        for item in value.values():
            found = _result_object(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _result_object(item)
            if found:
                return found
    if isinstance(value, str):
        candidates = [value.strip()]
        candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", value, re.S))
        for candidate in candidates:
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            found = _result_object(decoded)
            if found:
                return found
    return None


def _validated_result(
    frames: Iterable[Mapping[str, Any]], *, required_evidence: Iterable[str],
) -> RunnerResult:
    envelope = None
    for frame in reversed(list(frames)):
        envelope = _result_object(frame)
        if envelope:
            break
    if not envelope:
        raise RunnerProtocolError("terminal runner stream has no result proof")
    outcome = envelope.get("outcome")
    label = envelope.get("preferred_label")
    evidence = envelope.get("evidence")
    if not isinstance(outcome, str) or not outcome:
        raise RunnerProtocolError("runner result proof requires outcome")
    if not isinstance(label, str) or not label:
        raise RunnerProtocolError("runner result proof requires preferred_label")
    if not isinstance(evidence, list) or any(not isinstance(item, dict) for item in evidence):
        raise RunnerProtocolError("runner result proof requires evidence objects")
    kinds = {
        str(item.get("kind")) for item in evidence
        if isinstance(item.get("kind"), str)
    }
    missing = set(required_evidence) - kinds
    if missing:
        raise RunnerProtocolError(
            "runner result proof is missing evidence: " + ", ".join(sorted(missing))
        )
    return RunnerResult(outcome, label, tuple(dict(item) for item in evidence))


class RunnerAdapter:
    protocol_version = 1
    kind = ""

    def command(
        self, route: RunnerRoute, launch: PreparedLaunch, *, session_id: str | None,
    ) -> tuple[str, ...]:
        raise NotImplementedError

    def stdin(self, launch: PreparedLaunch, *, prompt_text: str) -> str:
        if not isinstance(prompt_text, str) or not prompt_text.strip():
            raise RunnerProtocolError("live runner requires immutable prompt text")
        contract = (
            "\n\nReturn a final JSON object with dotfactory_result=1, outcome, "
            "preferred_label, and evidence. Process exit alone is not success."
        )
        return prompt_text + contract

    def input_payload(
        self, launch: PreparedLaunch, *, prompt_text: str,
        session_id: str | None,
    ) -> bytes:
        del session_id
        return (self.stdin(launch, prompt_text=prompt_text) + "\n").encode("utf-8")

    def keep_stdin_open(self) -> bool:
        return False

    def cancel_payload(self, runner_run_id: str) -> bytes | None:
        del runner_run_id
        return None

    def frame_event(self, frame: Mapping[str, Any]) -> RunnerEvent:
        raise NotImplementedError

    def parse(
        self, lines: Iterable[str], *, exit_code: int = 0,
        required_evidence: Iterable[str] = (),
    ) -> RunnerReceipt:
        raise NotImplementedError


class CodexAdapter(RunnerAdapter):
    kind = "codex"

    def command(self, route, launch, *, session_id=None):
        if session_id:
            command = [route.command, "exec", "resume", "--json", session_id]
        else:
            command = [route.command, "exec", "--json", "--approve-for-me"]
        config = launch.request.config
        if config.get("model"):
            command.extend(["-m", str(config["model"])])
        if config.get("reasoning_effort"):
            command.extend([
                "-c", f'model_reasoning_effort="{config["reasoning_effort"]}"',
            ])
        command.append("-")
        return tuple(command)

    def frame_event(self, frame):
        raw = str(frame.get("type", ""))
        if raw in ("thread.started", "turn.started"):
            kind = "session"
        elif raw in ("item.started", "exec_command.started", "tool_call.started"):
            kind = "tool_call"
        elif raw in ("item.completed", "exec_command.completed", "tool_call.completed"):
            kind = (
                "assistant" if frame.get("item", {}).get("type") == "agent_message"
                else "tool_result"
            )
        elif raw == "turn.completed":
            kind = "terminal"
        elif raw in ("turn.failed", "error"):
            kind = "error"
        else:
            kind = "usage" if frame.get("usage") else "protocol"
        return RunnerEvent(kind, raw, frame, source_occurred_at=_source_timestamp(frame))

    def parse(self, lines, *, exit_code=0, required_evidence=()):
        frames = [_strict_object(line) for line in lines if line.strip()]
        events = []
        result_frames = []
        session = None
        terminal = None
        for frame in frames:
            event = self.frame_event(frame)
            raw = event.protocol_type
            session = session or _session_id(frame)
            if raw in ("item.completed", "exec_command.completed", "tool_call.completed"):
                if frame.get("item", {}).get("type") == "agent_message":
                    result_frames.append(frame)
            elif raw == "turn.completed":
                terminal = raw
            events.append(event)
        if exit_code != 0:
            raise RunnerProtocolError(f"codex exited with code {exit_code}")
        if not terminal:
            raise RunnerProtocolError("codex stream has no terminal turn.completed")
        result = _validated_result(result_frames, required_evidence=required_evidence)
        return RunnerReceipt(1, "codex", self.kind, session, terminal, tuple(events), result)


class ClaudeCodeAdapter(RunnerAdapter):
    kind = "claude-code"

    def command(self, route, launch, *, session_id=None):
        command = [
            route.command, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", route.permission_mode,
        ]
        if session_id:
            command.extend(["--resume", session_id])
        else:
            command.extend(["--session-id", launch.request.attempt_id])
        config = launch.request.config
        if config.get("model"):
            command.extend(["--model", str(config["model"])])
        if config.get("reasoning_effort"):
            command.extend(["--effort", str(config["reasoning_effort"])])
        return tuple(command)

    def frame_event(self, frame):
        raw = str(frame.get("type", ""))
        if raw == "assistant":
            kind = "assistant"
        elif raw == "user":
            kind = "tool_result"
        elif raw == "result":
            kind = "error" if frame.get("is_error") else "terminal"
        elif raw == "system":
            kind = "session"
        else:
            kind = "protocol"
        return RunnerEvent(kind, raw, frame, source_occurred_at=_source_timestamp(frame))

    def parse(self, lines, *, exit_code=0, required_evidence=()):
        frames = [_strict_object(line) for line in lines if line.strip()]
        events = []
        session = None
        terminal = None
        for frame in frames:
            event = self.frame_event(frame)
            raw = event.protocol_type
            session = session or _session_id(frame)
            if event.kind == "terminal":
                terminal = raw
            events.append(event)
        if exit_code != 0:
            raise RunnerProtocolError(f"claude-code exited with code {exit_code}")
        if not terminal:
            raise RunnerProtocolError("claude-code stream has no successful result")
        result_frames = [frame for frame in frames if frame.get("type") == "result"]
        result = _validated_result(result_frames, required_evidence=required_evidence)
        return RunnerReceipt(1, "claude", self.kind, session, terminal, tuple(events), result)


class OmpRpcAdapter(RunnerAdapter):
    protocol_version = 2
    kind = "omp-rpc"

    def command(self, route, launch, *, session_id=None):
        command = [route.command, "--mode", "rpc", "--cwd", launch.workspace_path]
        if route.profile:
            command.extend(["--profile", route.profile])
        command.extend(["--approval-mode", route.permission_mode])
        config = launch.request.config
        if config.get("model"):
            command.extend(["--model", str(config["model"])])
        if config.get("reasoning_effort"):
            command.extend(["--thinking", str(config["reasoning_effort"])])
        return tuple(command)

    def input_payload(self, launch, *, prompt_text, session_id):
        frames = [{
            "id": "protocol-1", "type": "negotiate_protocol",
            "protocolVersion": 2,
        }]
        if session_id:
            frames.append({
                "id": f"resume-{launch.request.attempt_id}",
                "type": "switch_session", "sessionPath": session_id,
            })
        frames.append(json.loads(self.prompt_frame(launch, prompt_text=prompt_text)))
        return ("\n".join(json.dumps(frame, sort_keys=True) for frame in frames) + "\n").encode(
            "utf-8"
        )

    def keep_stdin_open(self) -> bool:
        return True

    def cancel_payload(self, runner_run_id: str) -> bytes | None:
        return (json.dumps({
            "id": f"abort-{runner_run_id}", "type": "abort",
        }, sort_keys=True) + "\n").encode("utf-8")

    def prompt_frame(self, launch: PreparedLaunch, *, prompt_text: str) -> str:
        return json.dumps({
            "id": f"attempt-{launch.request.attempt_id}",
            "type": "prompt", "message": self.stdin(launch, prompt_text=prompt_text),
        }, sort_keys=True)

    def frame_event(self, frame):
        raw = str(frame.get("type", ""))
        if raw == "agent_end" and frame.get("isTerminal") is not False:
            kind = "terminal"
        elif raw in ("agent_start", "turn_start", "turn_end"):
            kind = "session"
        elif raw in ("message_start", "message_update", "message_end"):
            kind = "assistant"
        elif raw.startswith("tool_execution_"):
            kind = "tool_call" if raw.endswith("start") else "tool_result"
        elif raw == "extension_ui_request":
            method = frame.get("method")
            kind = "approval" if method in ("confirm", "select") else "input"
            if method in (
                "notify", "setStatus", "setWidget", "setTitle",
                "set_editor_text",
            ):
                kind = "protocol"
        elif raw == "extension_error":
            kind = "error"
        else:
            kind = "protocol"
        return RunnerEvent(kind, raw, frame, source_occurred_at=_source_timestamp(frame))

    def parse(self, lines, *, exit_code=0, required_evidence=()):
        decoder = OmpRpcFrameDecoder()
        frames = []
        for line in lines:
            if line.strip():
                frames.extend(decoder.feed(line))
        decoder.finish()
        if not frames or frames[0].get("type") != "ready":
            raise RunnerProtocolError("omp-rpc stream does not start with ready")
        supported = frames[0].get("supportedProtocolVersions", [1])
        if 2 not in supported:
            raise RunnerProtocolError("omp-rpc protocol v2 is unavailable")
        negotiated = any(
            frame.get("type") == "response"
            and frame.get("command") == "negotiate_protocol"
            and frame.get("success") is True
            and frame.get("data", {}).get("protocolVersion") == 2
            for frame in frames[1:]
        )
        if not negotiated:
            raise RunnerProtocolError("omp-rpc protocol v2 negotiation failed")
        events = [RunnerEvent("protocol", "ready", frames[0])]
        session = None
        terminal = None
        for frame in frames[1:]:
            event = self.frame_event(frame)
            raw = event.protocol_type
            session = session or _session_id(frame)
            if event.kind == "terminal":
                terminal = raw
            events.append(event)
        if exit_code != 0:
            raise RunnerProtocolError(f"omp exited with code {exit_code}")
        if not terminal:
            raise RunnerProtocolError("omp-rpc stream has no terminal agent_end")
        terminal_frames = [
            frame for frame in frames
            if frame.get("type") == "agent_end" and frame.get("isTerminal") is not False
        ]
        for frame in terminal_frames:
            messages = frame.get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                if message.get("stopReason") != "error" and not message.get(
                    "errorMessage"
                ):
                    continue
                detail = message.get("errorMessage")
                if not isinstance(detail, str) or not detail:
                    detail = "provider returned stopReason=error"
                raise RunnerProviderError(
                    f"omp-rpc provider failed: {detail[:1024]}"
                )
        result = _validated_result(
            terminal_frames, required_evidence=required_evidence
        )
        return RunnerReceipt(2, "omp", self.kind, session, terminal, tuple(events), result)


ADAPTERS = {
    "codex": CodexAdapter(),
    "claude-code": ClaudeCodeAdapter(),
    "omp-rpc": OmpRpcAdapter(),
}


class LiveRunnerRouter:
    def __init__(self, ledger: SQLiteLedger, routes: Mapping[str, RunnerRoute]) -> None:
        self.ledger = ledger
        self.routes = dict(routes)

    def route(self, launch: PreparedLaunch) -> tuple[RunnerRoute, RunnerAdapter]:
        if not isinstance(launch, PreparedLaunch):
            raise PreparationError("live runner requires PreparedLaunch")
        self.ledger.assert_attempt_active(
            launch.request.attempt_id, launch.request.fence_token,
        )
        name = launch.request.config.get("runner")
        if not isinstance(name, str) or name not in self.routes:
            raise RunnerProtocolError(f"runner is not active: {name}")
        route = self.routes[name]
        adapter = ADAPTERS.get(route.kind)
        if not adapter:
            raise RunnerProtocolError(f"runner adapter is unavailable: {route.kind}")
        return route, adapter

    def capability_report(
        self, name: str, *, available: bool, version: str | None,
        reason: str | None = None,
    ) -> RunnerCapabilityReport:
        route = self.routes[name]
        return RunnerCapabilityReport(
            name, route.kind, available, version,
            route.capabilities if available else (), reason,
        )

    def preflight(
        self, name: str, *,
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> RunnerCapabilityReport:
        if name not in self.routes:
            raise RunnerProtocolError(f"runner is not configured: {name}")
        route = self.routes[name]
        executable = shutil.which(route.command)
        if not executable:
            return self.capability_report(
                name, available=False, version=None,
                reason=f"runner executable is unavailable: {route.command}",
            )
        result = run_command(
            [executable, "--version"], check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        output = "\n".join((result.stdout or "", result.stderr or "")).strip()
        if result.returncode != 0:
            return self.capability_report(
                name, available=False, version=None,
                reason=f"runner version preflight exited {result.returncode}",
            )
        try:
            observed = _version_tuple(output)
            minimum = _version_tuple(route.minimum_version)
        except RunnerProtocolError as error:
            return self.capability_report(
                name, available=False, version=output[:120] or None, reason=str(error),
            )
        width = max(len(observed), len(minimum))
        observed = observed + (0,) * (width - len(observed))
        minimum = minimum + (0,) * (width - len(minimum))
        version = ".".join(str(item) for item in observed)
        if observed < minimum:
            return self.capability_report(
                name, available=False, version=version,
                reason=f"runner {version} is older than required {route.minimum_version}",
            )
        return self.capability_report(name, available=True, version=version)

    def preflight_all(self) -> dict[str, RunnerCapabilityReport]:
        return {name: self.preflight(name) for name in sorted(self.routes)}


class LiveRunner:
    BASE_ENVIRONMENT = (
        "HOME", "LANG", "LC_ALL", "PATH", "SHELL", "SSH_AUTH_SOCK",
        "TMPDIR", "USER",
    )

    def __init__(
        self, ledger: SQLiteLedger, *, routes: Mapping[str, RunnerRoute],
        observed_versions: Mapping[str, str] | None = None,
        environment: Mapping[str, str] | None = None,
        cancel_requested: Callable[[str], bool] | None = None,
        observer: Callable[[RunnerEvent], None] | None = None,
        fault_hook: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        kill_process_group: Callable[[int, int], None] = os.killpg,
        host_id: str | None = None, boot_id: str | None = None,
    ) -> None:
        self.ledger = ledger
        self.router = LiveRunnerRouter(ledger, routes)
        self.observed_versions = dict(observed_versions or {})
        self.environment = dict(os.environ if environment is None else environment)
        self.cancel_requested = cancel_requested or (lambda _runner_run_id: False)
        self.observer = observer
        self.fault_hook = fault_hook
        self.monotonic = monotonic
        self.popen = popen
        self.kill_process_group = kill_process_group
        self.host_id = host_id or socket.gethostname()
        boot_epoch = int(time.time() - monotonic())
        self.boot_id = boot_id or f"{self.host_id}:{boot_epoch}"

    def _fault(self, boundary: str) -> None:
        if self.fault_hook:
            self.fault_hook(boundary)

    def _observe(self, event: RunnerEvent) -> None:
        if not self.observer:
            return
        try:
            self.observer(event)
        except Exception:
            return

    def _version(self, route: RunnerRoute) -> str:
        if route.name in self.observed_versions:
            version = self.observed_versions[route.name]
            observed = _version_tuple(version)
            minimum = _version_tuple(route.minimum_version)
            width = max(len(observed), len(minimum))
            if observed + (0,) * (width - len(observed)) < minimum + (0,) * (
                width - len(minimum)
            ):
                raise RunnerExecutionError(
                    f"runner {version} is older than required {route.minimum_version}"
                )
            return version
        report = self.router.preflight(route.name)
        if not report.available or not report.version:
            raise RunnerExecutionError(report.reason or "runner preflight failed")
        return report.version

    def _child_environment(
        self, route: RunnerRoute, launch: PreparedLaunch
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        child = {
            key: self.environment[key]
            for key in self.BASE_ENVIRONMENT if self.environment.get(key)
        }
        sensitive = []
        for key in route.environment_envs:
            value = self.environment.get(key)
            if not value:
                raise RunnerExecutionError(
                    f"runner requires environment variable {key}"
                )
            child[key] = value
            sensitive.append(value)
        child.update(launch.environment_dict())
        return child, tuple(sensitive)

    def _error_fact(
        self, error: Exception, *, phase: str, route: RunnerRoute,
        version: str, run: Mapping[str, Any], last_span_id: str | None,
        exit_code: int | None = None, sensitive: Sequence[str] = (),
    ) -> dict[str, Any]:
        message = str(_replace_sensitive(str(error), sensitive))[:2048]
        stable_message = re.sub(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
            "<id>", message,
        )
        stable_message = re.sub(r"\b[0-9]+\b", "<n>", stable_message)
        identity = "|".join((
            error.__class__.__name__, phase, route.kind, stable_message,
        ))
        if isinstance(error, RunnerTimedOut):
            category = "timeout"
            retryable = True
        elif isinstance(error, RunnerCanceled):
            category = "canceled"
            retryable = False
        elif isinstance(error, StaleAttempt):
            category = "stale_attempt"
            retryable = False
        elif isinstance(error, RunnerProviderError):
            category = "provider"
            retryable = True
        elif isinstance(error, RunnerProtocolError):
            category = "protocol"
            retryable = True
        else:
            category = "infrastructure"
            retryable = True
        return {
            "class": category,
            "exception": error.__class__.__name__,
            "fingerprint": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            "fingerprint_version": 1,
            "phase": phase,
            "runner": route.name,
            "runner_kind": route.kind,
            "runner_version": version,
            "retryable": retryable,
            "exit_code": exit_code,
            "last_good_span_id": last_span_id,
            "trace_id": run["trace_id"],
            "runner_run_id": run["id"],
            "excerpt": message,
            "evidence_uri": f"ledger://runner-runs/{run['id']}",
            "origin": "dotfactory-supervisor",
            "trust_class": "trusted-runtime",
        }

    def _terminate(
        self, process: subprocess.Popen[bytes] | None, *, adapter: RunnerAdapter,
        route: RunnerRoute, runner_run_id: str,
    ) -> None:
        if not process or process.poll() is not None:
            return
        payload = adapter.cancel_payload(runner_run_id)
        if payload and process.stdin and not process.stdin.closed:
            try:
                process.stdin.write(payload)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        if process.stdin and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            self.kill_process_group(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=route.termination_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            self.kill_process_group(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=route.termination_grace_seconds)
        except subprocess.TimeoutExpired as error:
            raise RunnerExecutionError("owned runner process group did not terminate") from error

    def _persist_frame(
        self, *, run: Mapping[str, Any], launch: PreparedLaunch,
        route: RunnerRoute, adapter: RunnerAdapter, frame: Mapping[str, Any],
        sensitive: Sequence[str], parent_span_id: str,
    ) -> tuple[RunnerEvent, str]:
        event = adapter.frame_event(frame)
        span_id = secrets.token_hex(8)
        observed_at = self.ledger.clock()
        payload = _durable_event_payload(frame, event, sensitive=sensitive)
        stored = self.ledger.append_runner_event(
            str(run["id"]), fence_token=launch.request.fence_token,
            kind=event.kind, protocol_type=event.protocol_type, stream="stdout",
            payload=payload, span_id=span_id, parent_span_id=parent_span_id,
            source_occurred_at=event.source_occurred_at, observed_at=observed_at,
            origin="provider", trust_class="untrusted-provider",
            session_id=_session_id(frame),
            maximum_payload_bytes=route.maximum_payload_bytes,
        )
        normalized = RunnerEvent(
            event.kind, event.protocol_type, payload,
            source_occurred_at=event.source_occurred_at, observed_at=observed_at,
            span_id=span_id, parent_span_id=parent_span_id,
            origin="provider", trust_class="untrusted-provider", stream="stdout",
            sequence=int(stored["sequence"]),
        )
        self._observe(normalized)
        return normalized, span_id

    def _persist_stderr(
        self, *, run: Mapping[str, Any], launch: PreparedLaunch,
        route: RunnerRoute, line: bytes, sensitive: Sequence[str],
        parent_span_id: str,
    ) -> str:
        try:
            text = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            text = "[non-UTF-8 stderr]"
        payload = _replace_sensitive({
            "excerpt": text[:2048], "original_bytes": len(line),
            "sha256": hashlib.sha256(line).hexdigest(),
        }, sensitive)
        span_id = secrets.token_hex(8)
        observed_at = self.ledger.clock()
        stored = self.ledger.append_runner_event(
            str(run["id"]), fence_token=launch.request.fence_token,
            kind="warning", protocol_type="process.stderr", stream="stderr",
            payload=payload, span_id=span_id, parent_span_id=parent_span_id,
            source_occurred_at=None, observed_at=observed_at,
            origin="provider-process", trust_class="untrusted-provider",
            maximum_payload_bytes=route.maximum_payload_bytes,
        )
        self._observe(RunnerEvent(
            "warning", "process.stderr", payload, observed_at=observed_at,
            span_id=span_id, parent_span_id=parent_span_id,
            origin="provider-process", trust_class="untrusted-provider",
            stream="stderr", sequence=int(stored["sequence"]),
        ))
        return span_id

    def _return_stored_result(self, run: Mapping[str, Any]) -> RunnerResult:
        result = run.get("result")
        if not isinstance(result, dict):
            raise RunnerExecutionError("durable runner result is missing")
        evidence = result.get("evidence")
        if not isinstance(evidence, list):
            raise RunnerExecutionError("durable runner evidence is missing")
        return RunnerResult(
            outcome=str(result["outcome"]),
            preferred_label=str(result["preferred_label"]),
            evidence=tuple(dict(item) for item in evidence),
        )

    def run(self, launch: PreparedLaunch) -> RunnerResult:
        if not isinstance(launch, PreparedLaunch):
            raise PreparationError("live runner requires PreparedLaunch")
        route, adapter = self.router.route(launch)
        version = self._version(route)
        prompt = launch.request.config.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise RunnerProtocolError("live runner requires immutable prompt text")
        existing = self.ledger.runner_run_for_attempt(launch.request.attempt_id)
        if existing and existing["status"] == "result_ready":
            self.ledger.assert_attempt_active(
                launch.request.attempt_id, launch.request.fence_token
            )
            return self._return_stored_result(existing)
        if existing and existing["status"] == "waiting_input":
            raise RunnerNeedsAttention(
                "runner is waiting for explicit input",
                attention_id=str(existing["attention_id"]),
                runner_run_id=str(existing["id"]),
                resume_phase="preparing",
            )
        if existing and existing["status"] not in ("planned", "resume_authorized"):
            raise RunnerExecutionError(
                f"runner attempt already has non-resumable status {existing['status']}"
            )
        session_id = (
            str(existing["session_id"])
            if existing and existing.get("session_id")
            and (
                existing["status"] == "resume_authorized"
                or int(existing.get("resume_count", 0)) > 0
            ) else None
        )
        command = list(adapter.command(route, launch, session_id=session_id))
        command_digest = hashlib.sha256(
            json.dumps(command, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if existing and existing["status"] == "resume_authorized":
            run = self.ledger.plan_runner_resume(
                str(existing["id"]), fence_token=launch.request.fence_token,
                command=command, command_digest=command_digest,
            )
        elif existing:
            if (
                existing["command_digest"] != command_digest
                or existing["prompt_digest"] != prompt_digest
            ):
                raise RunnerExecutionError("durable runner plan no longer matches launch")
            run = existing
        else:
            run = self.ledger.plan_runner_run(
                execution_id=launch.request.execution_id,
                attempt_id=launch.request.attempt_id,
                preparation_id=launch.preparation_id,
                preparation_digest=launch.preparation_digest,
                fence_token=launch.request.fence_token,
                runner_key=route.name, adapter_kind=route.kind,
                adapter_version=version, protocol_version=adapter.protocol_version,
                execution_trace_id=hashlib.sha256(
                    f"execution:{launch.request.execution_id}".encode("utf-8")
                ).hexdigest()[:32],
                trace_id=secrets.token_hex(16), root_span_id=secrets.token_hex(8),
                parent_trace_id=None, command=command, command_digest=command_digest,
                prompt_digest=prompt_digest, host_id=self.host_id, boot_id=self.boot_id,
            )
        return self._execute(
            launch=launch, route=route, adapter=adapter, version=version,
            run=run, command=command, prompt=prompt, session_id=session_id,
        )

    def remedy_attention(
        self, execution_id: str, *, attention_id: str, remedy: str,
        command_id: str, expected_attempt_id: str | None,
    ) -> dict[str, Any]:
        return self.ledger.remedy_runner_attention(
            execution_id=execution_id, attention_id=attention_id, remedy=remedy,
            command_id=command_id, expected_attempt_id=expected_attempt_id,
        )

    def _execute(
        self, *, launch: PreparedLaunch, route: RunnerRoute,
        adapter: RunnerAdapter, version: str, run: Mapping[str, Any],
        command: list[str], prompt: str, session_id: str | None,
    ) -> RunnerResult:
        process: subprocess.Popen[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        sensitive: tuple[str, ...] = ()
        last_span_id: str | None = str(run["root_span_id"])
        phase = "starting"
        exit_code: int | None = None
        try:
            run = self.ledger.mark_runner_starting(
                str(run["id"]), fence_token=launch.request.fence_token
            )
            self._fault("after_runner_start_intent")
            self.ledger.assert_attempt_active(
                launch.request.attempt_id, launch.request.fence_token
            )
            child_environment, sensitive = self._child_environment(route, launch)
            process = self.popen(
                command, cwd=launch.workspace_path, env=child_environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, close_fds=True, bufsize=0,
            )
            self._fault("after_runner_spawn")
            run = self.ledger.mark_runner_running(
                str(run["id"]), fence_token=launch.request.fence_token,
                pid=process.pid, process_group_id=process.pid,
            )
            phase = "running"
            payload = adapter.input_payload(
                launch, prompt_text=prompt, session_id=session_id
            )
            if len(payload) > route.maximum_reassembled_frame_bytes:
                raise RunnerProtocolError("runner input exceeds the byte limit")
            if not process.stdin:
                raise RunnerExecutionError("runner stdin is unavailable")
            process.stdin.write(payload)
            process.stdin.flush()
            if not adapter.keep_stdin_open():
                process.stdin.close()
            if not process.stdout or not process.stderr:
                raise RunnerExecutionError("runner output pipes are unavailable")
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            buffers = {"stdout": bytearray(), "stderr": bytearray()}
            decoder = (
                OmpRpcFrameDecoder(
                    maximum_frame_bytes=route.maximum_frame_bytes,
                    maximum_reassembled_frame_bytes=(
                        route.maximum_reassembled_frame_bytes
                    ),
                ) if isinstance(adapter, OmpRpcAdapter) else None
            )
            logical_lines: list[str] = []
            normalized_events: list[RunnerEvent] = []
            captured_events = 0
            operation_spans: dict[str, str] = {}
            terminal_seen = False
            started_monotonic = self.monotonic()
            last_protocol_activity = started_monotonic

            def accept_stdout(line: bytes) -> None:
                nonlocal captured_events, terminal_seen
                nonlocal last_protocol_activity, last_span_id
                if not line:
                    return
                try:
                    text = line.decode("utf-8", errors="strict")
                except UnicodeDecodeError as error:
                    raise RunnerProtocolError(
                        "runner stdout is not strict UTF-8"
                    ) from error
                frames = decoder.feed(text) if decoder else (
                    _strict_object(text, maximum_bytes=route.maximum_frame_bytes),
                )
                for frame in frames:
                    if captured_events >= route.maximum_events:
                        self.ledger.append_runner_event(
                            str(run["id"]), fence_token=launch.request.fence_token,
                            kind="warning", protocol_type="dotfactory.capture_dropped",
                            stream="supervisor", payload={
                                "reason": "maximum_events", "dropped": 1,
                            }, span_id=secrets.token_hex(8),
                            parent_span_id=str(run["root_span_id"]),
                            source_occurred_at=None, observed_at=self.ledger.clock(),
                            origin="dotfactory-supervisor", trust_class="trusted-runtime",
                            maximum_payload_bytes=route.maximum_payload_bytes,
                        )
                        self.ledger.add_runner_dropped_events(
                            str(run["id"]), fence_token=launch.request.fence_token,
                            count=1,
                        )
                        raise RunnerProtocolError("runner event limit exceeded")
                    logical_lines.append(json.dumps(frame, separators=(",", ":")))
                    preview = adapter.frame_event(frame)
                    operation_id = _operation_id(frame)
                    parent_span_id = str(run["root_span_id"])
                    if preview.kind == "tool_result" and operation_id:
                        parent_span_id = operation_spans.get(
                            operation_id, parent_span_id
                        )
                    normalized, span_id = self._persist_frame(
                        run=run, launch=launch, route=route, adapter=adapter,
                        frame=frame, sensitive=sensitive,
                        parent_span_id=parent_span_id,
                    )
                    if normalized.kind == "tool_call" and operation_id:
                        operation_spans[operation_id] = span_id
                    normalized_events.append(normalized)
                    captured_events += 1
                    last_span_id = span_id
                    last_protocol_activity = self.monotonic()
                    if normalized.kind in ("approval", "input"):
                        request_id = str(frame.get("id", "unknown"))
                        attention = self.ledger.open_attention(
                            execution_id=launch.request.execution_id,
                            attempt_id=launch.request.attempt_id,
                            preparation_id=launch.preparation_id,
                            dedupe_key=(
                                f"runner:{run['id']}:input:{request_id}"
                            ),
                            category=f"runner-{normalized.kind}",
                            provider=route.name,
                            detail={
                                "runner_run_id": run["id"],
                                "request_id": request_id,
                                "method": frame.get("method"),
                                "allowed_actions": ["retry", "cancel"],
                                "retry_meaning": "resume the durable runner session",
                                "trace_id": run["trace_id"],
                                "span_id": span_id,
                            },
                        )
                        self.ledger.mark_runner_waiting_input(
                            str(run["id"]), fence_token=launch.request.fence_token,
                            attention_id=str(attention["id"]),
                        )
                        raise RunnerNeedsAttention(
                            "runner requires explicit input",
                            attention_id=str(attention["id"]),
                            runner_run_id=str(run["id"]),
                            resume_phase="preparing",
                        )
                    if normalized.kind == "terminal":
                        terminal_seen = True
                        if process and process.stdin and not process.stdin.closed:
                            process.stdin.close()

            def accept_stderr(line: bytes) -> None:
                nonlocal captured_events, last_span_id
                if line:
                    if captured_events >= route.maximum_events:
                        self.ledger.append_runner_event(
                            str(run["id"]), fence_token=launch.request.fence_token,
                            kind="warning", protocol_type="dotfactory.capture_dropped",
                            stream="supervisor", payload={
                                "reason": "maximum_events", "dropped": 1,
                            }, span_id=secrets.token_hex(8),
                            parent_span_id=str(run["root_span_id"]),
                            source_occurred_at=None, observed_at=self.ledger.clock(),
                            origin="dotfactory-supervisor", trust_class="trusted-runtime",
                            maximum_payload_bytes=route.maximum_payload_bytes,
                        )
                        self.ledger.add_runner_dropped_events(
                            str(run["id"]), fence_token=launch.request.fence_token,
                            count=1,
                        )
                        raise RunnerProtocolError("runner event limit exceeded")
                    last_span_id = self._persist_stderr(
                        run=run, launch=launch, route=route, line=line,
                        sensitive=sensitive,
                        parent_span_id=str(run["root_span_id"]),
                    )
                    captured_events += 1

            while True:
                now = self.monotonic()
                if self.cancel_requested(str(run["id"])):
                    raise RunnerCanceled("runner cancellation was requested")
                wall_seconds = _duration_seconds(
                    launch.request.config.get("timeout", "30m")
                )
                if now - started_monotonic >= wall_seconds:
                    raise RunnerTimedOut("runner wall timeout expired")
                if now - last_protocol_activity >= route.silence_timeout_seconds:
                    raise RunnerTimedOut("runner silence timeout expired")
                timeout = min(
                    0.25, wall_seconds - (now - started_monotonic),
                    route.silence_timeout_seconds - (now - last_protocol_activity),
                )
                for key, _mask in selector.select(timeout=max(0.0, timeout)):
                    stream = str(key.data)
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        remainder = bytes(buffers[stream])
                        buffers[stream].clear()
                        if remainder:
                            if stream == "stdout":
                                accept_stdout(remainder)
                            else:
                                accept_stderr(remainder)
                        continue
                    buffers[stream].extend(data)
                    while b"\n" in buffers[stream]:
                        line, _, rest = buffers[stream].partition(b"\n")
                        buffers[stream] = bytearray(rest)
                        if stream == "stdout":
                            accept_stdout(line)
                        else:
                            accept_stderr(line)
                    if (
                        stream == "stdout"
                        and len(buffers[stream]) > route.maximum_frame_bytes
                    ):
                        raise RunnerProtocolError(
                            "runner protocol frame exceeds the byte limit"
                        )
                    if (
                        stream == "stderr"
                        and len(buffers[stream]) > route.maximum_payload_bytes
                    ):
                        accept_stderr(bytes(buffers[stream]))
                        buffers[stream].clear()
                if process.poll() is not None and not selector.get_map():
                    break
            exit_code = process.wait()
            if decoder:
                decoder.finish()
            phase = "validating"
            required_evidence = launch.request.config.get("evidence", ())
            if not isinstance(required_evidence, (list, tuple)) or any(
                not isinstance(item, str) for item in required_evidence
            ):
                raise RunnerProtocolError("runner evidence contract is invalid")
            receipt = adapter.parse(
                logical_lines, exit_code=exit_code,
                required_evidence=required_evidence,
            )
            if not terminal_seen:
                raise RunnerProtocolError("runner stream has no terminal event")
            self.ledger.assert_attempt_active(
                launch.request.attempt_id, launch.request.fence_token
            )
            self._fault("before_runner_result_commit")
            completed_at = self.ledger.clock()
            result_payload = _replace_sensitive({
                "outcome": receipt.result.outcome,
                "preferred_label": receipt.result.preferred_label,
                "evidence": list(receipt.result.evidence),
            }, sensitive)
            durable_run = self.ledger.runner_run(str(run["id"]))
            receipt_payload = {
                "protocol_version": receipt.protocol_version,
                "runner": route.name, "adapter_kind": route.kind,
                "adapter_version": version, "session_id": receipt.session_id,
                "terminal_type": receipt.terminal_type,
                "event_count": durable_run["event_count"],
                "dropped_event_count": durable_run["dropped_event_count"],
                "event_kinds": sorted({
                    str(event["kind"]) for event in durable_run["events"]
                }),
                "exit_code": exit_code,
                "execution_id": launch.request.execution_id,
                "attempt_id": launch.request.attempt_id,
                "runner_run_id": run["id"],
                "execution_trace_id": run["execution_trace_id"],
                "trace_id": run["trace_id"],
                "root_span_id": run["root_span_id"],
                "started_at": run["started_at"], "completed_at": completed_at,
                "evidence_uri": f"ledger://runner-runs/{run['id']}",
            }
            self.ledger.record_runner_result(
                str(run["id"]), fence_token=launch.request.fence_token,
                result=result_payload, receipt=receipt_payload,
                session_id=receipt.session_id,
            )
            self._fault("after_runner_result_commit")
            return receipt.result
        except RunnerNeedsAttention:
            self._terminate(
                process, adapter=adapter, route=route,
                runner_run_id=str(run["id"]),
            )
            raise
        except StaleAttempt as error:
            self._terminate(
                process, adapter=adapter, route=route,
                runner_run_id=str(run["id"]),
            )
            try:
                self.ledger.supersede_runner_run(
                    str(run["id"]), reason=str(error)
                )
            except Exception:
                pass
            raise
        except Exception as error:
            self._terminate(
                process, adapter=adapter, route=route,
                runner_run_id=str(run["id"]),
            )
            exit_code = process.poll() if process else exit_code
            fact = self._error_fact(
                error, phase=phase, route=route, version=version, run=run,
                last_span_id=last_span_id, exit_code=exit_code,
                sensitive=sensitive,
            )
            status = "canceled" if isinstance(error, RunnerCanceled) else "failed"
            try:
                self.ledger.finish_runner_run(
                    str(run["id"]), fence_token=launch.request.fence_token,
                    status=status, error=fact,
                )
            except StaleAttempt:
                try:
                    self.ledger.supersede_runner_run(
                        str(run["id"]), reason="attempt changed during runner failure"
                    )
                except Exception:
                    pass
            safe_message = str(_replace_sensitive(str(error), sensitive))
            if isinstance(error, RunnerExecutionError) and safe_message == str(error):
                raise
            raise RunnerExecutionError(safe_message) from error
        finally:
            if selector is not None:
                selector.close()
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        try:
                            stream.close()
                        except OSError:
                            pass
