"""Fail-soft delivery of committed events to external views."""

from __future__ import annotations

import json
from typing import Any, Callable

from .ledger import SQLiteLedger


def projection_envelope(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_seq": record["event_seq"],
        "event_id": record["event_id"],
        "event_type": record["event_type"],
        "schema_version": record["schema_version"],
        "occurred_at": record["occurred_at"],
        "event_command_id": record["event_command_id"],
        "execution_id": record["execution_id"],
        "execution_key": record["execution_key"],
        "project_key": record["project_key"],
        "work_item_identifier": record["work_item_identifier"],
        "intent": json.loads(record["intent_snapshot_json"]),
        "policy": {
            "workflow_name": record["workflow_name"],
            "workflow_version": record["workflow_version"],
        },
        "payload": json.loads(record["payload_json"]),
    }


class RunProjection:
    """Provider-neutral, idempotent current-state and run-history projection."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}

    def apply(self, envelope: dict[str, Any]) -> None:
        event_id = str(envelope["event_id"])
        self.events.setdefault(event_id, dict(envelope))

    def run(self, execution_id: str) -> dict[str, Any]:
        events = sorted(
            (
                item for item in self.events.values()
                if item["execution_id"] == execution_id
            ),
            key=lambda item: int(item["event_seq"]),
        )
        if not events:
            raise KeyError(execution_id)
        state = None
        evidence = []
        feedback = []
        outcomes = []
        work_by_id: dict[str, dict[str, Any]] = {}
        for event in events:
            payload = event["payload"]
            if event["event_type"] == "execution_started":
                state = payload["state"]
            elif event["event_type"] == "transition_accepted":
                state = payload["to"]
                evidence.extend(payload.get("evidence") or [])
                feedback.extend(payload.get("feedback") or [])
                if payload.get("outcome") is not None:
                    outcomes.append(payload["outcome"])
                completed = payload.get("completed_attempt")
                if completed:
                    attempt_id = str(completed["attempt_id"])
                    item = work_by_id.setdefault(attempt_id, {})
                    item.update(completed)
                    item["status"] = "completed"
                entered = payload.get("entered_attempt")
                if entered:
                    attempt_id = str(entered["attempt_id"])
                    item = work_by_id.setdefault(attempt_id, {})
                    item.update(entered)
                    item["status"] = "active"
        first = events[0]
        return {
            "project_key": first["project_key"],
            "work_item_identifier": first["work_item_identifier"],
            "execution_key": first["execution_key"],
            "intent": first["intent"],
            "policy": first["policy"],
            "current_state": state,
            "evidence": evidence,
            "outcomes": outcomes,
            "feedback": feedback,
            "work": sorted(
                work_by_id.values(), key=lambda item: int(item["ordinal"])
            ),
            "events": events,
        }


class ProjectionWorker:
    def __init__(self, ledger: SQLiteLedger, destination: str,
                 sink: Callable[[dict[str, Any]], None]) -> None:
        self.ledger = ledger
        self.destination = destination
        self.sink = sink

    def drain(self, limit: int = 100) -> int:
        delivered = 0
        for record in self.ledger.pending(self.destination, limit):
            envelope = projection_envelope(record)
            try:
                self.sink(envelope)
            except Exception as error:
                self.ledger.mark_failed(record["id"], str(error))
                break
            self.ledger.mark_delivered(record["id"])
            delivered += 1
        return delivered

    def rebuild(
        self, *, command_id: str, requested_by: str,
        from_event_seq: int = 1, batch_size: int = 100
    ) -> dict[str, Any]:
        if batch_size < 1:
            raise ValueError("projection rebuild batch size must be positive")
        replay = self.ledger.start_projection_rebuild(
            self.destination, command_id=command_id, requested_by=requested_by,
            from_event_seq=from_event_seq,
        )
        replay_id = str(replay["id"])
        self.ledger.resume_projection_rebuild(replay_id)
        delivered_this_call = 0
        while True:
            records = self.ledger.pending_rebuild(replay_id, batch_size)
            if not records:
                break
            stopped = False
            for record in records:
                envelope = projection_envelope(record)
                try:
                    self.sink(envelope)
                except Exception as error:
                    self.ledger.mark_rebuild_failed(
                        replay_id, str(record["outbox_id"]), str(error)
                    )
                    stopped = True
                    break
                self.ledger.mark_rebuild_delivered(
                    replay_id, str(record["outbox_id"])
                )
                delivered_this_call += 1
            if stopped:
                break
        result = self.ledger.projection_rebuild(replay_id)
        result["delivered_this_call"] = delivered_this_call
        return result
