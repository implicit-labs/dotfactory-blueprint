"""Provider-neutral runner request and deterministic fake adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .kernel import DurableKernel, KernelError


@dataclass(frozen=True)
class RunnerRequest:
    execution_id: str
    attempt_id: str
    fence_token: str
    state_id: str
    workflow_digest: str
    owner: str
    config: dict[str, Any]


@dataclass(frozen=True)
class RunnerResult:
    outcome: str
    preferred_label: str
    evidence: tuple[dict[str, Any], ...]


def runner_request(kernel: DurableKernel, execution_id: str) -> RunnerRequest:
    current = kernel.ledger.current(execution_id)
    attempt = current.get("attempt")
    if not attempt:
        raise KernelError("current workflow node has no active attempt")
    binding = attempt.get("binding")
    if not binding:
        raise KernelError("active attempt has no resolved workflow binding")
    return RunnerRequest(
        execution_id=execution_id,
        attempt_id=str(attempt["id"]),
        fence_token=str(attempt["fence_token"]),
        state_id=str(current["current_state_id"]),
        workflow_digest=str(binding["workflow_digest"]),
        owner=str(attempt["owner"]),
        config=dict(binding["resolved"]),
    )


class FakeRunner:
    def __init__(self, results: list[RunnerResult]) -> None:
        self.results = list(results)
        self.requests: list[RunnerRequest] = []

    def run(self, request: RunnerRequest) -> RunnerResult:
        self.requests.append(request)
        if not self.results:
            raise KernelError("fake runner has no result")
        return self.results.pop(0)


def run_fake_attempt(
    kernel: DurableKernel, execution_id: str, runner: FakeRunner, *, command_id: str
) -> dict[str, Any]:
    request = runner_request(kernel, execution_id)
    result = runner.run(request)
    return kernel.complete_attempt(
        execution_id,
        preferred_label=result.preferred_label,
        outcome=result.outcome,
        evidence=list(result.evidence),
        attempt_id=request.attempt_id,
        fence_token=request.fence_token,
        owner=request.owner,
        command_id=command_id,
    )
