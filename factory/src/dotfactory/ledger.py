"""SQLite authority for factory state and rebuildable projections."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .observability import (
    ErrorFactV1, ProjectionReceiptV1, TraceRecordV1, canonical_json,
    error_fingerprint, stable_record_id, stable_span_id, stable_trace_id,
)


SCHEMA_VERSION = 10

LEGACY_STATE_IDS = {
    "todo": "Todo",
    "auto_planning": "Autoplanning",
    "planning": "Planning",
    "ready": "Ready",
    "implementing": "Implementing",
    "verifying": "Verifying",
    "review": "Review",
    "reworking": "Reworking",
    "investigating": "Investigating",
    "blocked": "Blocked",
    "done": "Done",
    "canceled": "Canceled",
    "duplicate": "Duplicate",
}


class LedgerError(RuntimeError):
    pass


class StaleAttempt(LedgerError):
    pass


class ResourceBusy(LedgerError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise LedgerError("timestamps must include a timezone")
    return parsed


def validate_feedback(
    feedback: list[dict[str, Any]], *, expected_kind: str | None = None
) -> None:
    required = ("source", "kind", "author", "body", "url")
    for item in feedback:
        if not isinstance(item, dict) or any(
            not isinstance(item.get(field), str) or not item[field].strip()
            for field in required
        ):
            raise LedgerError(
                "feedback requires source, kind, author, body, and url"
            )
    if expected_kind and not any(item.get("kind") == expected_kind for item in feedback):
        raise LedgerError(f"transition requires {expected_kind} feedback")


validate_review_feedback = validate_feedback


SECRET_MARKERS = ("authorization", "cookie", "password", "secret", "token", "api_key")


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if any(marker in key.lower() for marker in SECRET_MARKERS)
            else redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def new_id() -> str:
    """Return a UUIDv7-compatible time-sortable identifier."""
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


class SQLiteLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], str] = utc_now,
        id_factory: Callable[[], str] = new_id,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.id_factory = id_factory
        self.fault_hook = fault_hook
        self.connection = sqlite3.connect(str(path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._migrate()

    def _fault(self, boundary: str) -> None:
        if self.fault_hook:
            self.fault_hook(boundary)

    def close(self) -> None:
        self.connection.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _migrate(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise LedgerError(f"ledger schema {version} is newer than {SCHEMA_VERSION}")
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            with self.transaction() as db:
                db.executescript(
                    """
                CREATE TABLE factory_identity (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    factory_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
                );
                CREATE TABLE projects (
                    project_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    tracker_kind TEXT NOT NULL, tracker_project_id TEXT NOT NULL,
                    tracker_project_slug TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(tracker_kind, tracker_project_id)
                );
                CREATE TABLE work_items (
                    id TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL REFERENCES projects(project_key),
                    identifier TEXT NOT NULL, intent_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(project_key, identifier)
                );
                CREATE TABLE workflow_executions (
                    id TEXT PRIMARY KEY, work_item_id TEXT NOT NULL REFERENCES work_items(id),
                    workflow_name TEXT NOT NULL, workflow_version INTEGER NOT NULL,
                    execution_number INTEGER NOT NULL, execution_key TEXT NOT NULL,
                    intent_snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL, current_state_id TEXT NOT NULL,
                    desired_linear_status TEXT NOT NULL, observed_linear_status TEXT,
                    current_state_run_id TEXT, created_at TEXT NOT NULL, completed_at TEXT,
                    UNIQUE(work_item_id, execution_number), UNIQUE(work_item_id, execution_key)
                );
                CREATE TABLE state_runs (
                    id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES workflow_executions(id),
                    state_id TEXT NOT NULL, state_kind TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL, resume_state_id TEXT, started_at TEXT NOT NULL,
                    completed_at TEXT, UNIQUE(execution_id, ordinal)
                );
                CREATE TABLE attempts (
                    id TEXT PRIMARY KEY, state_run_id TEXT NOT NULL REFERENCES state_runs(id),
                    owner TEXT NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL,
                    fence_token TEXT NOT NULL UNIQUE, started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL, completed_at TEXT, outcome TEXT
                );
                CREATE TABLE events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    execution_id TEXT NOT NULL REFERENCES workflow_executions(id),
                    state_run_id TEXT, attempt_id TEXT, event_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL, occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE transition_decisions (
                    id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES workflow_executions(id),
                    edge_id TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL,
                    actor TEXT NOT NULL, signal TEXT NOT NULL, desired_linear_status TEXT NOT NULL,
                    event_seq INTEGER NOT NULL UNIQUE REFERENCES events(seq), decided_at TEXT NOT NULL
                );
                CREATE TABLE artifacts (
                    id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES workflow_executions(id),
                    attempt_id TEXT, kind TEXT NOT NULL, uri TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE feedback (
                    id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES workflow_executions(id),
                    source TEXT NOT NULL, target_id TEXT NOT NULL, body_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE transition_requests (
                    id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL REFERENCES workflow_executions(id),
                    edge_id TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL,
                    actor TEXT NOT NULL, signal TEXT NOT NULL,
                    observed_linear_status TEXT, source_event_id TEXT,
                    status TEXT NOT NULL, event_seq INTEGER NOT NULL UNIQUE REFERENCES events(seq),
                    requested_at TEXT NOT NULL, consumed_at TEXT
                );
                CREATE UNIQUE INDEX pending_transition_request
                    ON transition_requests(execution_id) WHERE status='pending';
                CREATE TABLE resource_leases (
                    id TEXT PRIMARY KEY, resource_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL REFERENCES attempts(id), fence_token TEXT NOT NULL,
                    status TEXT NOT NULL, acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, released_at TEXT
                );
                CREATE UNIQUE INDEX active_resource_lease
                    ON resource_leases(resource_id) WHERE status='active';
                CREATE TABLE outbox (
                    id TEXT PRIMARY KEY, destination TEXT NOT NULL,
                    event_seq INTEGER NOT NULL REFERENCES events(seq), status TEXT NOT NULL,
                    delivery_attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                    created_at TEXT NOT NULL, delivered_at TEXT,
                    UNIQUE(destination, event_seq)
                );
                CREATE INDEX outbox_pending ON outbox(destination, status, event_seq);
                CREATE TABLE projection_replays (
                    id TEXT PRIMARY KEY, destination TEXT NOT NULL,
                    command_id TEXT NOT NULL, requested_by TEXT NOT NULL,
                    from_event_seq INTEGER NOT NULL,
                    through_event_seq INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, completed_at TEXT,
                    UNIQUE(destination, command_id)
                );
                CREATE TABLE projection_replay_items (
                    replay_id TEXT NOT NULL REFERENCES projection_replays(id),
                    outbox_id TEXT NOT NULL REFERENCES outbox(id),
                    event_seq INTEGER NOT NULL, status TEXT NOT NULL,
                    delivery_attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                    delivered_at TEXT,
                    PRIMARY KEY(replay_id, outbox_id)
                );
                CREATE INDEX projection_replay_pending
                    ON projection_replay_items(replay_id, status, event_seq);
                CREATE TABLE control_commands (
                    command_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL REFERENCES workflow_executions(id),
                    action TEXT NOT NULL, principal_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL, request_json TEXT NOT NULL,
                    status TEXT NOT NULL, authorization_decision TEXT,
                    authorization_reason TEXT, result_json TEXT, error_json TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE control_command_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    command_id TEXT NOT NULL REFERENCES control_commands(command_id),
                    event_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(command_id, event_type)
                );
                """
                )
                self._create_schema_six_additions(db)
                self._create_schema_seven_additions(db)
                self._create_schema_eight_additions(db)
                self._create_schema_nine_additions(db)
                self._create_schema_ten_additions(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if version == 1:
            self.connection.execute("PRAGMA foreign_keys=OFF")
            legacy_alter_table = int(
                self.connection.execute("PRAGMA legacy_alter_table").fetchone()[0]
            )
            self.connection.execute("PRAGMA legacy_alter_table=ON")
            try:
                with self.transaction() as db:
                    db.execute(
                        "CREATE TABLE factory_identity ("
                        "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                        "factory_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)"
                    )
                    db.execute(
                        "CREATE TABLE projects (project_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,"
                        "tracker_kind TEXT NOT NULL, tracker_project_id TEXT NOT NULL,"
                        "tracker_project_slug TEXT,"
                        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                        "UNIQUE(tracker_kind,tracker_project_id))"
                    )
                    now = self.clock()
                    db.execute(
                        "INSERT INTO projects VALUES(?,?,?,?,?,?,?)",
                        ("legacy", "Migrated project", "unknown", "legacy", None, now, now),
                    )
                    db.execute("ALTER TABLE work_items RENAME TO work_items_v1")
                    db.execute(
                        "CREATE TABLE work_items (id TEXT PRIMARY KEY,"
                        "project_key TEXT NOT NULL REFERENCES projects(project_key),"
                        "identifier TEXT NOT NULL,intent_json TEXT NOT NULL,created_at TEXT NOT NULL,"
                        "UNIQUE(project_key,identifier))"
                    )
                    db.execute(
                        "INSERT INTO work_items(id,project_key,identifier,intent_json,created_at) "
                        "SELECT id,'legacy',identifier,intent_json,created_at FROM work_items_v1"
                    )
                    db.execute("DROP TABLE work_items_v1")
                    db.execute(
                        "ALTER TABLE workflow_executions ADD COLUMN execution_number INTEGER"
                    )
                    db.execute("ALTER TABLE workflow_executions ADD COLUMN execution_key TEXT")
                    db.execute(
                        "ALTER TABLE workflow_executions ADD COLUMN intent_snapshot_json TEXT"
                    )
                    counts: dict[str, int] = {}
                    rows = db.execute(
                        "SELECT we.id,we.work_item_id,wi.identifier FROM workflow_executions we "
                        "JOIN work_items wi ON wi.id=we.work_item_id ORDER BY we.created_at,we.id"
                    ).fetchall()
                    for row in rows:
                        work_item_id = str(row["work_item_id"])
                        number = counts.get(work_item_id, 0) + 1
                        counts[work_item_id] = number
                        key = (
                            str(row["identifier"])
                            if number == 1 else f"{row['identifier']}-{number}"
                        )
                        db.execute(
                            "UPDATE workflow_executions SET execution_number=?,execution_key=?,"
                            "intent_snapshot_json=(SELECT intent_json FROM work_items WHERE id=?) "
                            "WHERE id=?",
                            (number, key, work_item_id, row["id"]),
                        )
                    db.execute(
                        "CREATE UNIQUE INDEX workflow_execution_numbers "
                        "ON workflow_executions(work_item_id,execution_number)"
                    )
                    db.execute(
                        "CREATE UNIQUE INDEX workflow_execution_keys "
                        "ON workflow_executions(work_item_id,execution_key)"
                    )
                    db.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS active_resource_lease "
                        "ON resource_leases(resource_id) WHERE status='active'"
                    )
                    for old, new in LEGACY_STATE_IDS.items():
                        db.execute(
                            "UPDATE workflow_executions SET current_state_id=? "
                            "WHERE current_state_id=?", (new, old),
                        )
                        db.execute(
                            "UPDATE state_runs SET state_id=? WHERE state_id=?", (new, old)
                        )
                        db.execute(
                            "UPDATE state_runs SET resume_state_id=? WHERE resume_state_id=?",
                            (new, old),
                        )
                        db.execute(
                            "UPDATE transition_decisions SET from_state=? WHERE from_state=?",
                            (new, old),
                        )
                        db.execute(
                            "UPDATE transition_decisions SET to_state=? WHERE to_state=?",
                            (new, old),
                        )
                    self._create_schema_three_additions(db)
                    self._create_schema_four_additions(db)
                    self._create_schema_five_additions(db)
                    self._create_schema_six_additions(db)
                    self._create_schema_seven_additions(db)
                    self._create_schema_eight_additions(db)
                    self._create_schema_nine_additions(db)
                    self._create_schema_ten_additions(db)
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            finally:
                self.connection.execute(
                    f"PRAGMA legacy_alter_table={legacy_alter_table}"
                )
                self.connection.execute("PRAGMA foreign_keys=ON")
            violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise LedgerError("schema migration left invalid foreign keys")
            return
        if version == 2:
            with self.transaction() as db:
                self._create_schema_three_additions(db)
                self._create_schema_four_additions(db)
                self._create_schema_five_additions(db)
                self._create_schema_six_additions(db)
                self._create_schema_seven_additions(db)
                self._create_schema_eight_additions(db)
                self._create_schema_nine_additions(db)
                self._create_schema_ten_additions(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if version == 3:
            with self.transaction() as db:
                self._create_schema_four_additions(db)
                self._create_schema_five_additions(db)
                self._create_schema_six_additions(db)
                self._create_schema_seven_additions(db)
                self._create_schema_eight_additions(db)
                self._create_schema_nine_additions(db)
                self._create_schema_ten_additions(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if version == 4:
            with self.transaction() as db:
                self._create_schema_five_additions(db)
                self._create_schema_six_additions(db)
                self._create_schema_seven_additions(db)
                self._create_schema_eight_additions(db)
                self._create_schema_nine_additions(db)
                self._create_schema_ten_additions(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if version == 5:
            with self.transaction() as db:
                self._create_schema_six_additions(db)
                self._create_schema_seven_additions(db)
                self._create_schema_eight_additions(db)
                self._create_schema_nine_additions(db)
                self._create_schema_ten_additions(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if version == 6:
            with self.transaction() as db:
                self._create_schema_seven_additions(db)
                self._create_schema_eight_additions(db)
                self._create_schema_nine_additions(db)
                self._create_schema_ten_additions(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if version == 7:
            with self.transaction() as db:
                self._create_schema_eight_additions(db)
                self._create_schema_nine_additions(db)
                self._create_schema_ten_additions(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if version == 8:
            with self.transaction() as db:
                self._create_schema_nine_additions(db)
                self._create_schema_ten_additions(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if version == 9:
            with self.transaction() as db:
                self._create_schema_ten_additions(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        raise LedgerError(f"no migration from ledger schema {version}")

    def _create_schema_three_additions(self, db: sqlite3.Connection) -> None:
        try:
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS projects_tracker_identity "
                "ON projects(tracker_kind,tracker_project_id)"
            )
        except sqlite3.IntegrityError as error:
            raise LedgerError("duplicate tracker project identities require manual repair") from error
        db.execute(
            "CREATE TABLE IF NOT EXISTS transition_requests ("
            "id TEXT PRIMARY KEY,"
            "execution_id TEXT NOT NULL REFERENCES workflow_executions(id),"
            "edge_id TEXT NOT NULL,from_state TEXT NOT NULL,to_state TEXT NOT NULL,"
            "actor TEXT NOT NULL,signal TEXT NOT NULL,observed_linear_status TEXT,"
            "source_event_id TEXT,status TEXT NOT NULL,"
            "event_seq INTEGER NOT NULL UNIQUE REFERENCES events(seq),"
            "requested_at TEXT NOT NULL,consumed_at TEXT)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS pending_transition_request "
            "ON transition_requests(execution_id) WHERE status='pending'"
        )

    def _create_schema_four_additions(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS projection_replays ("
            "id TEXT PRIMARY KEY,destination TEXT NOT NULL,command_id TEXT NOT NULL,"
            "requested_by TEXT NOT NULL,from_event_seq INTEGER NOT NULL,"
            "through_event_seq INTEGER NOT NULL,"
            "status TEXT NOT NULL,created_at TEXT NOT NULL,completed_at TEXT,"
            "UNIQUE(destination,command_id))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS projection_replay_items ("
            "replay_id TEXT NOT NULL REFERENCES projection_replays(id),"
            "outbox_id TEXT NOT NULL REFERENCES outbox(id),event_seq INTEGER NOT NULL,"
            "status TEXT NOT NULL,delivery_attempts INTEGER NOT NULL DEFAULT 0,"
            "last_error TEXT,delivered_at TEXT,PRIMARY KEY(replay_id,outbox_id))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS projection_replay_pending "
            "ON projection_replay_items(replay_id,status,event_seq)"
        )

    def _create_schema_five_additions(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS control_commands ("
            "command_id TEXT PRIMARY KEY,"
            "execution_id TEXT NOT NULL REFERENCES workflow_executions(id),"
            "action TEXT NOT NULL,principal_json TEXT NOT NULL,"
            "request_hash TEXT NOT NULL,request_json TEXT NOT NULL,"
            "status TEXT NOT NULL,authorization_decision TEXT,"
            "authorization_reason TEXT,result_json TEXT,error_json TEXT,"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS control_command_events ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,"
            "command_id TEXT NOT NULL REFERENCES control_commands(command_id),"
            "event_type TEXT NOT NULL,occurred_at TEXT NOT NULL,payload_json TEXT NOT NULL,"
            "UNIQUE(command_id,event_type))"
        )

    def _create_schema_six_additions(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS workflow_snapshots ("
            "digest TEXT PRIMARY KEY,workflow_name TEXT NOT NULL,"
            "schema_version INTEGER NOT NULL,source_format TEXT NOT NULL,"
            "source_text TEXT NOT NULL,normalized_json TEXT NOT NULL,"
            "created_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS execution_workflow_snapshots ("
            "execution_id TEXT PRIMARY KEY REFERENCES workflow_executions(id),"
            "workflow_digest TEXT NOT NULL REFERENCES workflow_snapshots(digest))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS attempt_bindings ("
            "attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),"
            "workflow_digest TEXT NOT NULL REFERENCES workflow_snapshots(digest),"
            "state_id TEXT NOT NULL,resolved_json TEXT NOT NULL,created_at TEXT NOT NULL)"
        )

    def _create_schema_seven_additions(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS execution_workspaces ("
            "id TEXT PRIMARY KEY,execution_id TEXT NOT NULL UNIQUE "
            "REFERENCES workflow_executions(id),project_key TEXT NOT NULL,"
            "owner_token TEXT NOT NULL,"
            "repository_path TEXT NOT NULL,git_common_dir TEXT NOT NULL,"
            "remote TEXT NOT NULL,base_ref TEXT NOT NULL,base_sha TEXT NOT NULL,"
            "branch_name TEXT NOT NULL,path TEXT NOT NULL UNIQUE,status TEXT NOT NULL,"
            "metadata_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
            "cleaned_at TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS preparations ("
            "id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),"
            "execution_id TEXT NOT NULL REFERENCES workflow_executions(id),"
            "fence_token TEXT NOT NULL,request_digest TEXT NOT NULL,status TEXT NOT NULL,"
            "result_digest TEXT,prepared_json TEXT,error_json TEXT,"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS resource_allocations ("
            "id TEXT PRIMARY KEY,preparation_id TEXT NOT NULL REFERENCES preparations(id),"
            "execution_id TEXT NOT NULL REFERENCES workflow_executions(id),"
            "attempt_id TEXT REFERENCES attempts(id),scope TEXT NOT NULL,"
            "provider TEXT NOT NULL,capability TEXT NOT NULL,resource_id TEXT NOT NULL,"
            "fence_token TEXT NOT NULL,status TEXT NOT NULL,metadata_json TEXT NOT NULL,"
            "acquired_at TEXT NOT NULL,heartbeat_at TEXT NOT NULL,expires_at TEXT,"
            "released_at TEXT)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS active_resource_allocation "
            "ON resource_allocations(resource_id) "
            "WHERE status IN ('active','release_pending')"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS resource_mutations ("
            "id TEXT PRIMARY KEY,preparation_id TEXT NOT NULL REFERENCES preparations(id),"
            "allocation_id TEXT REFERENCES resource_allocations(id),provider TEXT NOT NULL,"
            "step_key TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,"
            "status TEXT NOT NULL,intent_json TEXT NOT NULL,result_json TEXT,error_json TEXT,"
            "planned_at TEXT NOT NULL,started_at TEXT,completed_at TEXT,"
            "UNIQUE(preparation_id,provider,step_key))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS attention_requests ("
            "id TEXT PRIMARY KEY,execution_id TEXT NOT NULL REFERENCES workflow_executions(id),"
            "attempt_id TEXT REFERENCES attempts(id),preparation_id TEXT "
            "REFERENCES preparations(id),dedupe_key TEXT NOT NULL,category TEXT NOT NULL,"
            "capability TEXT,provider TEXT,status TEXT NOT NULL,detail_json TEXT NOT NULL,"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,resolved_at TEXT)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS open_attention_dedupe "
            "ON attention_requests(dedupe_key) WHERE status='open'"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS cleanup_plans ("
            "id TEXT PRIMARY KEY,execution_id TEXT NOT NULL REFERENCES workflow_executions(id),"
            "attempt_id TEXT REFERENCES attempts(id),status TEXT NOT NULL,plan_json TEXT NOT NULL,"
            "result_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT)"
        )

    def _create_schema_eight_additions(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS scheduler_dispatches ("
            "id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),"
            "execution_id TEXT NOT NULL REFERENCES workflow_executions(id),"
            "project_key TEXT NOT NULL,runner_key TEXT NOT NULL,"
            "scheduler_owner TEXT NOT NULL,claim_token TEXT NOT NULL UNIQUE,"
            "attempt_fence_token TEXT NOT NULL,status TEXT NOT NULL,"
            "available_at TEXT,heartbeat_at TEXT NOT NULL,expires_at TEXT,"
            "preparation_id TEXT REFERENCES preparations(id),preparation_digest TEXT,"
            "result_json TEXT,error_json TEXT,attention_id TEXT REFERENCES attention_requests(id),"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS scheduler_dispatch_status "
            "ON scheduler_dispatches(status,available_at,expires_at)"
        )

    def _create_schema_nine_additions(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS runner_runs ("
            "id TEXT PRIMARY KEY,"
            "execution_id TEXT NOT NULL REFERENCES workflow_executions(id),"
            "attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),"
            "preparation_id TEXT NOT NULL REFERENCES preparations(id),"
            "fence_token TEXT NOT NULL,runner_key TEXT NOT NULL,"
            "adapter_kind TEXT NOT NULL,adapter_version TEXT NOT NULL,"
            "protocol_version INTEGER NOT NULL,"
            "execution_trace_id TEXT NOT NULL,trace_id TEXT NOT NULL,"
            "root_span_id TEXT NOT NULL,parent_trace_id TEXT,"
            "status TEXT NOT NULL,command_json TEXT NOT NULL,"
            "command_digest TEXT NOT NULL,prompt_digest TEXT NOT NULL,"
            "host_id TEXT NOT NULL,boot_id TEXT NOT NULL,"
            "pid INTEGER,process_group_id INTEGER,session_id TEXT,"
            "attention_id TEXT REFERENCES attention_requests(id),"
            "resume_count INTEGER NOT NULL DEFAULT 0,"
            "event_count INTEGER NOT NULL DEFAULT 0,"
            "dropped_event_count INTEGER NOT NULL DEFAULT 0,"
            "result_json TEXT,receipt_json TEXT,error_json TEXT,"
            "created_at TEXT NOT NULL,started_at TEXT,last_activity_at TEXT,"
            "completed_at TEXT)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS runner_runs_status "
            "ON runner_runs(status,created_at)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS runner_events ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,"
            "runner_run_id TEXT NOT NULL REFERENCES runner_runs(id),"
            "sequence INTEGER NOT NULL,execution_id TEXT NOT NULL,"
            "attempt_id TEXT NOT NULL,trace_id TEXT NOT NULL,span_id TEXT NOT NULL,"
            "parent_span_id TEXT,kind TEXT NOT NULL,protocol_type TEXT NOT NULL,"
            "stream TEXT NOT NULL,source_occurred_at TEXT,observed_at TEXT NOT NULL,"
            "origin TEXT NOT NULL,trust_class TEXT NOT NULL,payload_json TEXT NOT NULL,"
            "payload_bytes INTEGER NOT NULL,truncated INTEGER NOT NULL DEFAULT 0,"
            "UNIQUE(runner_run_id,sequence))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS runner_events_trace "
            "ON runner_events(trace_id,sequence)"
        )

    def _create_schema_ten_additions(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS trace_records ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "record_id TEXT NOT NULL UNIQUE,"
            "source_kind TEXT NOT NULL,source_id TEXT NOT NULL,source_seq INTEGER,"
            "execution_id TEXT NOT NULL REFERENCES workflow_executions(id),"
            "state_run_id TEXT,attempt_id TEXT,runner_run_id TEXT,"
            "trace_id TEXT NOT NULL,span_id TEXT NOT NULL,parent_span_id TEXT,"
            "record_kind TEXT NOT NULL,domain TEXT NOT NULL,phase TEXT NOT NULL,"
            "name TEXT NOT NULL,status TEXT NOT NULL,"
            "entity_kind TEXT NOT NULL,entity_id TEXT NOT NULL,"
            "started_at TEXT,ended_at TEXT,source_occurred_at TEXT,"
            "observed_at TEXT NOT NULL,origin TEXT NOT NULL,trust_class TEXT NOT NULL,"
            "ordering_quality TEXT NOT NULL,schema_version INTEGER NOT NULL,"
            "links_json TEXT NOT NULL,completeness_json TEXT NOT NULL,"
            "payload_json TEXT NOT NULL,created_at TEXT NOT NULL,"
            "UNIQUE(source_kind,source_id))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS trace_records_execution "
            "ON trace_records(execution_id,seq)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS trace_records_trace "
            "ON trace_records(trace_id,seq)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS trace_records_entity "
            "ON trace_records(entity_kind,entity_id,seq)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS error_facts ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,error_id TEXT NOT NULL UNIQUE,"
            "execution_id TEXT NOT NULL REFERENCES workflow_executions(id),"
            "trace_record_id TEXT NOT NULL REFERENCES trace_records(record_id),"
            "domain TEXT NOT NULL,phase TEXT NOT NULL,code TEXT NOT NULL,"
            "category TEXT NOT NULL,severity TEXT NOT NULL,retryable INTEGER NOT NULL,"
            "ambiguous_side_effect INTEGER NOT NULL,fingerprint TEXT NOT NULL,"
            "fingerprint_version INTEGER NOT NULL,message TEXT NOT NULL,"
            "safe_remedy TEXT NOT NULL,responsible_span_id TEXT,last_good_span_id TEXT,"
            "first_failed_span_id TEXT,occurred_at TEXT NOT NULL,origin TEXT NOT NULL,"
            "trust_class TEXT NOT NULL,capture_complete INTEGER NOT NULL,"
            "completeness_json TEXT NOT NULL,schema_version INTEGER NOT NULL,"
            "created_at TEXT NOT NULL,UNIQUE(trace_record_id))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS error_facts_execution "
            "ON error_facts(execution_id,seq)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS error_facts_fingerprint "
            "ON error_facts(fingerprint,seq)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS projection_attempts ("
            "id TEXT PRIMARY KEY,destination TEXT NOT NULL,command_id TEXT NOT NULL,"
            "source_kind TEXT NOT NULL,from_source_seq INTEGER NOT NULL,"
            "through_source_seq INTEGER NOT NULL,idempotency_key TEXT NOT NULL,"
            "status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
            "completed_at TEXT,UNIQUE(destination,command_id),"
            "UNIQUE(destination,idempotency_key))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS projection_attempts_status "
            "ON projection_attempts(destination,status,created_at)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS projection_receipts ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,receipt_id TEXT NOT NULL UNIQUE,"
            "attempt_id TEXT NOT NULL REFERENCES projection_attempts(id),"
            "destination TEXT NOT NULL,source_record_id TEXT NOT NULL,"
            "status TEXT NOT NULL,idempotency_key TEXT NOT NULL,external_id TEXT,"
            "error_code TEXT,detail_json TEXT NOT NULL,schema_version INTEGER NOT NULL,"
            "recorded_at TEXT NOT NULL,UNIQUE(attempt_id,idempotency_key))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS projection_receipts_source "
            "ON projection_receipts(destination,source_record_id,seq)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS projection_rejections ("
            "receipt_id TEXT PRIMARY KEY REFERENCES projection_receipts(receipt_id),"
            "attempt_id TEXT NOT NULL,destination TEXT NOT NULL,"
            "source_record_id TEXT NOT NULL,error_code TEXT NOT NULL,"
            "message TEXT NOT NULL,retryable INTEGER NOT NULL,created_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS projection_watermarks ("
            "destination TEXT NOT NULL,source_kind TEXT NOT NULL,"
            "through_source_seq INTEGER NOT NULL,attempt_id TEXT NOT NULL "
            "REFERENCES projection_attempts(id),updated_at TEXT NOT NULL,"
            "PRIMARY KEY(destination,source_kind))"
        )
        self._backfill_schema_ten(db)

    @staticmethod
    def _table_exists(db: sqlite3.Connection, table: str) -> bool:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    def _backfill_schema_ten(self, db: sqlite3.Connection) -> None:
        if not self._table_exists(db, "events"):
            return
        existing = int(db.execute(
            "SELECT COUNT(*) FROM trace_records"
        ).fetchone()[0])
        if existing:
            return
        affected: dict[str, dict[str, int]] = {}
        for row in db.execute("SELECT * FROM events ORDER BY seq"):
            payload = json.loads(row["payload_json"])
            self._mirror_lifecycle_trace(
                db, event_id=str(row["event_id"]), source_seq=int(row["seq"]),
                execution_id=str(row["execution_id"]),
                state_run_id=row["state_run_id"], attempt_id=row["attempt_id"],
                event_type=str(row["event_type"]), payload=payload,
                occurred_at=str(row["occurred_at"]), ordering_quality="reconstructed",
                trust_class="reconstructed",
            )
            counts = affected.setdefault(str(row["execution_id"]), {"events": 0, "runner": 0})
            counts["events"] += 1
        if self._table_exists(db, "runner_events"):
            for row in db.execute("SELECT * FROM runner_events ORDER BY seq"):
                run = db.execute(
                    "SELECT rr.*,a.state_run_id FROM runner_runs rr JOIN attempts a "
                    "ON a.id=rr.attempt_id WHERE rr.id=?", (row["runner_run_id"],),
                ).fetchone()
                if not run:
                    continue
                self._mirror_runner_trace(db, row=row, run=run, reconstructed=True)
                counts = affected.setdefault(
                    str(row["execution_id"]), {"events": 0, "runner": 0}
                )
                counts["runner"] += 1
        for execution_id in sorted(affected):
            counts = affected[execution_id]
            now = self.clock()
            source_id = f"schema-9:{execution_id}"
            record = TraceRecordV1(
                record_id=stable_record_id("migration", source_id),
                execution_id=execution_id, source_kind="migration", source_id=source_id,
                record_kind="completeness", domain="observability", phase="migration",
                name="schema_9_trace_reconstruction", status="incomplete",
                entity_kind="execution", entity_id=execution_id,
                trace_id=stable_trace_id(execution_id),
                span_id=stable_span_id("migration", source_id),
                origin="dotfactory-migration", trust_class="reconstructed",
                observed_at=now, ordering_quality="reconstructed",
                completeness={
                    "complete": False,
                    "reasons": ["reconstructed_order", "raw_stream_coverage_unknown"],
                    "lifecycle_records": counts["events"],
                    "runner_records": counts["runner"],
                },
                payload={"from_schema": 9, "to_schema": 10},
            )
            self._insert_trace_record(db, record)

    @staticmethod
    def _event_domain(event_type: str) -> str:
        prefix = event_type.split("_", 1)[0]
        return {
            "execution": "workflow", "transition": "workflow", "linear": "workflow",
            "scheduler": "scheduler", "preparation": "preparation",
            "workspace": "workspace", "resource": "resource",
            "runner": "runner", "cleanup": "cleanup", "attention": "attention",
            "control": "control",
        }.get(prefix, "kernel")

    @staticmethod
    def _event_status(event_type: str) -> str:
        suffix = event_type.rsplit("_", 1)[-1]
        if suffix in (
            "failed", "error", "denied", "canceled", "superseded",
            "quarantined", "busy", "attention",
        ):
            return "failed"
        if suffix in (
            "completed", "ready", "released", "resolved", "accepted",
            "observed", "expired", "skipped", "result",
        ):
            return "completed"
        if suffix in ("waiting", "requested"):
            return "waiting"
        return suffix

    @staticmethod
    def _event_record_kind(event_type: str) -> str:
        status = SQLiteLedger._event_status(event_type)
        if status == "failed":
            return "error"
        suffix = event_type.rsplit("_", 1)[-1]
        if suffix in (
            "started", "planned", "claimed", "preparing", "dispatching",
            "completed", "ready", "released", "accepted", "failed",
            "canceled", "superseded", "quarantined", "skipped",
        ):
            return "span"
        return "event"

    @staticmethod
    def _trace_entity(
        event_type: str, payload: dict[str, Any], execution_id: str,
        state_run_id: str | None, attempt_id: str | None,
    ) -> tuple[str, str]:
        candidates = (
            ("runner_run_id", "runner_run"), ("dispatch_id", "scheduler_dispatch"),
            ("mutation_id", "resource_mutation"),
            ("allocation_id", "resource_allocation"),
            ("preparation_id", "preparation"), ("cleanup_id", "cleanup"),
            ("workspace_id", "workspace"), ("attention_id", "attention"),
            ("lease_id", "resource_lease"), ("command_id", "control_command"),
        )
        for key, kind in candidates:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return kind, value
        if event_type.startswith("execution_"):
            return "execution", execution_id
        if attempt_id:
            return "attempt", attempt_id
        if state_run_id:
            return "state_run", state_run_id
        return "execution", execution_id

    @staticmethod
    def _parent_span_id(
        entity_kind: str, *, execution_id: str, state_run_id: str | None,
        attempt_id: str | None, payload: dict[str, Any],
    ) -> str | None:
        if entity_kind == "execution":
            return None
        if entity_kind == "state_run":
            return stable_span_id("execution", execution_id)
        if entity_kind == "attempt":
            parent_id = state_run_id or execution_id
            parent_kind = "state_run" if state_run_id else "execution"
            return stable_span_id(parent_kind, parent_id)
        if entity_kind in ("resource_mutation", "resource_allocation"):
            preparation_id = payload.get("preparation_id")
            if isinstance(preparation_id, str) and preparation_id:
                return stable_span_id("preparation", preparation_id)
        if attempt_id:
            return stable_span_id("attempt", attempt_id)
        if state_run_id:
            return stable_span_id("state_run", state_run_id)
        return stable_span_id("execution", execution_id)

    @staticmethod
    def _event_times(event_type: str, occurred_at: str) -> tuple[str | None, str | None]:
        suffix = event_type.rsplit("_", 1)[-1]
        started_at = occurred_at if suffix in (
            "started", "planned", "claimed", "preparing", "dispatching"
        ) else None
        ended_at = occurred_at if suffix in (
            "completed", "ready", "released", "accepted", "failed", "canceled",
            "superseded", "quarantined", "skipped", "expired",
        ) else None
        return started_at, ended_at

    def _insert_trace_record(
        self, db: sqlite3.Connection, record: TraceRecordV1,
        *, source_seq: int | None = None,
    ) -> int:
        db.execute(
            "INSERT INTO trace_records("
            "record_id,source_kind,source_id,source_seq,execution_id,state_run_id,"
            "attempt_id,runner_run_id,trace_id,span_id,parent_span_id,record_kind,"
            "domain,phase,name,status,entity_kind,entity_id,started_at,ended_at,"
            "source_occurred_at,observed_at,origin,trust_class,ordering_quality,"
            "schema_version,links_json,completeness_json,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.record_id, record.source_kind, record.source_id, source_seq,
                record.execution_id, record.state_run_id, record.attempt_id,
                record.runner_run_id, record.trace_id or stable_trace_id(record.execution_id),
                record.span_id or stable_span_id(record.source_kind, record.source_id),
                record.parent_span_id, record.record_kind, record.domain, record.phase,
                record.name, record.status, record.entity_kind, record.entity_id,
                record.started_at, record.ended_at, record.source_occurred_at,
                record.observed_at, record.origin, record.trust_class,
                record.ordering_quality, record.schema_version,
                canonical_json(record.links), canonical_json(record.completeness),
                canonical_json(record.payload), self.clock(),
            ),
        )
        return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _insert_error_fact(
        self, db: sqlite3.Connection, record: TraceRecordV1,
        error_source: Any,
    ) -> None:
        source = error_source if isinstance(error_source, dict) else {}
        raw_message = source.get("message", source.get("excerpt", record.name))
        message = str(redact_payload(str(raw_message)))[:2048]
        raw_code = source.get("code")
        code = str(raw_code) if isinstance(raw_code, (str, int)) else (
            "DOTFACTORY_" + re.sub(r"[^A-Z0-9]+", "_", record.name.upper()).strip("_")
        )
        category = str(source.get("category", source.get("class", record.domain)))
        fact = ErrorFactV1(
            error_id=stable_record_id("error", record.record_id).replace("tr-", "err-", 1),
            execution_id=record.execution_id, trace_record_id=record.record_id,
            domain=record.domain, phase=record.phase, code=code,
            category=category, severity=str(source.get("severity", "error")),
            retryable=bool(source.get("retryable", False)),
            ambiguous_side_effect=bool(source.get("ambiguous_side_effect", False)),
            fingerprint=str(source.get("fingerprint") or error_fingerprint(
                domain=record.domain, phase=record.phase, code=code, message=message,
            )),
            fingerprint_version=int(source.get("fingerprint_version", 1)),
            message=message,
            safe_remedy=str(source.get(
                "safe_remedy", source.get(
                    "remedy", "Inspect the linked trace record before retrying."
                ),
            )),
            responsible_span_id=record.span_id,
            last_good_span_id=source.get("last_good_span_id"),
            first_failed_span_id=source.get("first_failed_span_id") or record.span_id,
            occurred_at=record.source_occurred_at or record.observed_at,
            origin=record.origin, trust_class=record.trust_class,
            capture_complete=not bool(record.completeness),
            completeness=record.completeness,
        )
        db.execute(
            "INSERT INTO error_facts("
            "error_id,execution_id,trace_record_id,domain,phase,code,category,severity,"
            "retryable,ambiguous_side_effect,fingerprint,fingerprint_version,message,"
            "safe_remedy,responsible_span_id,last_good_span_id,first_failed_span_id,"
            "occurred_at,origin,trust_class,capture_complete,completeness_json,"
            "schema_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fact.error_id, fact.execution_id, fact.trace_record_id, fact.domain,
                fact.phase, fact.code, fact.category, fact.severity, int(fact.retryable),
                int(fact.ambiguous_side_effect), fact.fingerprint,
                fact.fingerprint_version, fact.message, fact.safe_remedy,
                fact.responsible_span_id, fact.last_good_span_id,
                fact.first_failed_span_id, fact.occurred_at, fact.origin,
                fact.trust_class, int(fact.capture_complete),
                canonical_json(fact.completeness), fact.schema_version, self.clock(),
            ),
        )

    def _mirror_lifecycle_trace(
        self, db: sqlite3.Connection, *, event_id: str, source_seq: int,
        execution_id: str, state_run_id: str | None, attempt_id: str | None,
        event_type: str, payload: dict[str, Any], occurred_at: str,
        ordering_quality: str = "exact", trust_class: str = "trusted-kernel",
        source_kind: str = "event",
    ) -> None:
        payload = redact_payload(payload)
        entity_kind, entity_id = self._trace_entity(
            event_type, payload, execution_id, state_run_id, attempt_id
        )
        started_at, ended_at = self._event_times(event_type, occurred_at)
        status = self._event_status(event_type)
        record = TraceRecordV1(
            record_id=stable_record_id(source_kind, event_id), execution_id=execution_id,
            source_kind=source_kind, source_id=event_id,
            state_run_id=state_run_id, attempt_id=attempt_id,
            runner_run_id=(
                str(payload["runner_run_id"])
                if isinstance(payload.get("runner_run_id"), str) else None
            ),
            trace_id=stable_trace_id(execution_id),
            span_id=stable_span_id(entity_kind, entity_id),
            parent_span_id=self._parent_span_id(
                entity_kind, execution_id=execution_id, state_run_id=state_run_id,
                attempt_id=attempt_id, payload=payload,
            ),
            record_kind=self._event_record_kind(event_type),
            domain=self._event_domain(event_type), phase=event_type,
            name=event_type, status=status, entity_kind=entity_kind,
            entity_id=entity_id, started_at=started_at, ended_at=ended_at,
            source_occurred_at=occurred_at, observed_at=occurred_at,
            origin="dotfactory-ledger", trust_class=trust_class,
            ordering_quality=ordering_quality, payload=payload,
        )
        self._insert_trace_record(db, record, source_seq=source_seq)
        if status == "failed":
            error_source = payload.get("error", payload.get("result", payload))
            self._insert_error_fact(db, record, error_source)

    @staticmethod
    def _runner_operation_id(payload: dict[str, Any]) -> str | None:
        for key in ("toolCallId", "tool_call_id", "call_id", "id", "item_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _mirror_runner_trace(
        self, db: sqlite3.Connection, *, row: sqlite3.Row, run: sqlite3.Row,
        reconstructed: bool = False,
    ) -> None:
        payload = json.loads(row["payload_json"])
        operation_id = self._runner_operation_id(payload)
        span_id = (
            stable_span_id("runner-operation", f"{row['runner_run_id']}:{operation_id}")
            if operation_id else str(row["span_id"])
        )
        kind = str(row["kind"])
        parent_span_id = row["parent_span_id"]
        if kind == "tool_result" and operation_id and parent_span_id:
            opening = db.execute(
                "SELECT parent_span_id FROM runner_events WHERE runner_run_id=? "
                "AND span_id=? ORDER BY sequence LIMIT 1",
                (row["runner_run_id"], parent_span_id),
            ).fetchone()
            if opening:
                parent_span_id = opening["parent_span_id"]
        started_at = str(row["observed_at"]) if kind == "tool_call" else None
        ended_at = str(row["observed_at"]) if kind == "tool_result" else None
        completeness: dict[str, Any] = {}
        if int(row["truncated"]):
            completeness = {"complete": False, "reasons": ["payload_truncated"]}
        if str(row["protocol_type"]) == "dotfactory.capture_dropped":
            completeness = {
                "complete": False, "reasons": ["records_dropped"],
                "dropped_records": int(payload.get("dropped_events", 1)),
            }
        record = TraceRecordV1(
            record_id=stable_record_id("runner_event", str(row["event_id"])),
            execution_id=str(row["execution_id"]), source_kind="runner_event",
            source_id=str(row["event_id"]), state_run_id=run["state_run_id"],
            attempt_id=str(row["attempt_id"]), runner_run_id=str(row["runner_run_id"]),
            trace_id=stable_trace_id(str(row["execution_id"])), span_id=span_id,
            parent_span_id=parent_span_id,
            record_kind=(
                "error" if kind == "error" else
                "span" if kind in ("tool_call", "tool_result") else "event"
            ),
            domain="runner", phase=str(row["protocol_type"]),
            name=kind, status="failed" if kind == "error" else (
                "started" if kind == "tool_call" else
                "completed" if kind in ("tool_result", "terminal") else "observed"
            ),
            entity_kind="runner_operation" if operation_id else "runner_run",
            entity_id=operation_id or str(row["runner_run_id"]),
            started_at=started_at, ended_at=ended_at,
            source_occurred_at=row["source_occurred_at"],
            observed_at=str(row["observed_at"]), origin=str(row["origin"]),
            trust_class=("reconstructed" if reconstructed else str(row["trust_class"])),
            ordering_quality="reconstructed" if reconstructed else "exact",
            completeness=completeness, payload=payload,
        )
        self._insert_trace_record(db, record, source_seq=int(row["seq"]))
        if kind == "error":
            self._insert_error_fact(db, record, payload)

    def _event(
        self,
        db: sqlite3.Connection,
        *,
        execution_id: str,
        state_run_id: str | None,
        attempt_id: str | None,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        destinations: tuple[str, ...] = ("linear", "logfire"),
    ) -> int:
        event_id = self.id_factory()
        occurred_at = self.clock()
        redacted = redact_payload(payload)
        db.execute(
            "INSERT INTO events(event_id,execution_id,state_run_id,attempt_id,event_type,"
            "schema_version,occurred_at,payload_json,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, execution_id, state_run_id, attempt_id, event_type, 1,
             occurred_at, canonical_json(redacted), idempotency_key),
        )
        self._fault("after_trace_source_recorded")
        seq = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        self._mirror_lifecycle_trace(
            db, event_id=event_id, source_seq=seq, execution_id=execution_id,
            state_run_id=state_run_id, attempt_id=attempt_id,
            event_type=event_type, payload=redacted, occurred_at=occurred_at,
        )
        for destination in destinations:
            db.execute(
                "INSERT INTO outbox(id,destination,event_seq,status,created_at) VALUES(?,?,?,?,?)",
                (self.id_factory(), destination, seq, "pending", occurred_at),
            )
        return seq

    def configure_factory(self, factory_id: str) -> None:
        row = self.connection.execute(
            "SELECT factory_id FROM factory_identity WHERE singleton=1"
        ).fetchone()
        if row:
            if row["factory_id"] != factory_id:
                raise LedgerError(
                    f"ledger belongs to factory {row['factory_id']}, not {factory_id}"
                )
            return
        self.connection.execute(
            "INSERT INTO factory_identity VALUES(1,?,?)", (factory_id, self.clock())
        )

    def _store_workflow_snapshot(
        self, db: sqlite3.Connection, snapshot: dict[str, Any]
    ) -> str:
        required = (
            "digest", "name", "schema_version", "source_format", "source_text", "normalized"
        )
        if any(key not in snapshot for key in required):
            raise LedgerError("workflow snapshot is incomplete")
        digest = str(snapshot["digest"])
        normalized_json = json.dumps(
            redact_payload(snapshot["normalized"]), sort_keys=True, separators=(",", ":")
        )
        existing = db.execute(
            "SELECT source_format,source_text,normalized_json FROM workflow_snapshots "
            "WHERE digest=?", (digest,),
        ).fetchone()
        if existing and (
            existing["source_format"] != snapshot["source_format"]
            or existing["source_text"] != snapshot["source_text"]
            or existing["normalized_json"] != normalized_json
        ):
            raise LedgerError("workflow digest collision")
        if not existing:
            db.execute(
                "INSERT INTO workflow_snapshots VALUES(?,?,?,?,?,?,?)",
                (digest, snapshot["name"], snapshot["schema_version"],
                 snapshot["source_format"], snapshot["source_text"],
                 normalized_json, self.clock()),
            )
        return digest

    def _bind_attempt(
        self, db: sqlite3.Connection, *, attempt_id: str, workflow_digest: str,
        state_id: str, resolved_node: dict[str, Any],
    ) -> None:
        db.execute(
            "INSERT INTO attempt_bindings VALUES(?,?,?,?,?)",
            (attempt_id, workflow_digest, state_id,
             json.dumps(redact_payload(resolved_node), sort_keys=True), self.clock()),
        )

    def register_project(
        self, project_key: str, *, display_name: str,
        tracker_kind: str, tracker_project_id: str,
        tracker_project_slug: str | None = None,
    ) -> None:
        identity = self.connection.execute(
            "SELECT factory_id FROM factory_identity WHERE singleton=1"
        ).fetchone()
        if not identity:
            raise LedgerError("configure the factory identity before registering projects")
        row = self.connection.execute(
            "SELECT * FROM projects WHERE project_key=?", (project_key,)
        ).fetchone()
        now = self.clock()
        if row:
            if (
                row["tracker_kind"] != tracker_kind
                or row["tracker_project_id"] != tracker_project_id
            ):
                raise LedgerError(
                    f"project {project_key} is already bound to "
                    f"{row['tracker_kind']}:{row['tracker_project_id']}"
                )
            self.connection.execute(
                "UPDATE projects SET display_name=?,tracker_project_slug=?,updated_at=? "
                "WHERE project_key=?",
                (display_name, tracker_project_slug, now, project_key),
            )
            return
        try:
            self.connection.execute(
                "INSERT INTO projects VALUES(?,?,?,?,?,?,?)",
                (project_key, display_name, tracker_kind, tracker_project_id,
                 tracker_project_slug, now, now),
            )
        except sqlite3.IntegrityError as error:
            raise LedgerError(
                f"tracker project is already registered: {tracker_kind}:{tracker_project_id}"
            ) from error

    def begin_execution(
        self, *, project_key: str, identifier: str, intent: dict[str, Any],
        workflow_name: str, workflow_version: int, state_id: str,
        state_kind: str, linear_status: str, idempotency_key: str,
        workflow_snapshot: dict[str, Any] | None = None,
        resolved_node: dict[str, Any] | None = None,
        owner: str | None = None,
        actor: str = "agent",
    ) -> str:
        existing = self.connection.execute(
            "SELECT execution_id FROM events WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing:
            return str(existing["execution_id"])
        with self.transaction() as db:
            existing = db.execute(
                "SELECT execution_id FROM events WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                return str(existing["execution_id"])
            if not db.execute(
                "SELECT 1 FROM projects WHERE project_key=?", (project_key,)
            ).fetchone():
                raise LedgerError(f"unknown project: {project_key}")
            intent_json = json.dumps(redact_payload(intent), sort_keys=True)
            item = db.execute(
                "SELECT id FROM work_items WHERE project_key=? AND identifier=?",
                (project_key, identifier),
            ).fetchone()
            if item:
                work_item_id = str(item["id"])
                db.execute(
                    "UPDATE work_items SET intent_json=? WHERE id=?",
                    (intent_json, work_item_id),
                )
            else:
                work_item_id = self.id_factory()
                db.execute(
                    "INSERT INTO work_items(id,project_key,identifier,intent_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (work_item_id, project_key, identifier, intent_json, self.clock()),
                )
            number = int(db.execute(
                "SELECT COALESCE(MAX(execution_number),0)+1 FROM workflow_executions "
                "WHERE work_item_id=?", (work_item_id,),
            ).fetchone()[0])
            execution_key = identifier if number == 1 else f"{identifier}-{number}"
            execution_id, state_run_id = self.id_factory(), self.id_factory()
            now = self.clock()
            workflow_digest = None
            if workflow_snapshot:
                workflow_digest = self._store_workflow_snapshot(db, workflow_snapshot)
            db.execute(
                "INSERT INTO workflow_executions(id,work_item_id,workflow_name,workflow_version,"
                "execution_number,execution_key,intent_snapshot_json,status,current_state_id,"
                "desired_linear_status,observed_linear_status,current_state_run_id,created_at,"
                "completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (execution_id, work_item_id, workflow_name, workflow_version, number,
                 execution_key, intent_json, "running", state_id, linear_status, None,
                 state_run_id, now, None),
            )
            db.execute(
                "INSERT INTO state_runs VALUES(?,?,?,?,?,?,?,?,?)",
                (state_run_id, execution_id, state_id, state_kind, 1, "active", None, now, None),
            )
            if workflow_digest:
                db.execute(
                    "INSERT INTO execution_workflow_snapshots VALUES(?,?)",
                    (execution_id, workflow_digest),
                )
            initial_attempt = None
            if state_kind == "work":
                if not owner:
                    raise LedgerError("entering work requires an owner")
                initial_attempt = self.id_factory()
                fence_token = self.id_factory()
                db.execute(
                    "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (initial_attempt, state_run_id, owner, actor, "active", fence_token,
                     now, now, None, None),
                )
                if workflow_digest:
                    self._bind_attempt(
                        db, attempt_id=initial_attempt, workflow_digest=workflow_digest,
                        state_id=state_id, resolved_node=resolved_node or {},
                    )
            self._event(
                db, execution_id=execution_id, state_run_id=state_run_id,
                attempt_id=initial_attempt, event_type="execution_started",
                payload={
                    "state": state_id,
                    "execution_key": execution_key,
                    "workflow_digest": (workflow_snapshot or {}).get("digest"),
                    "resolved_node": resolved_node or {},
                },
                idempotency_key=idempotency_key,
            )
        return execution_id

    def accept_transition(
        self, *, execution_id: str, edge_id: str, from_state: str, to_state: str,
        to_kind: str, desired_linear_status: str, actor: str, signal: str,
        owner: str | None, attempt_id: str | None, fence_token: str | None,
        outcome: str | None, evidence: list[dict[str, Any]], idempotency_key: str,
        terminal: bool, feedback: list[dict[str, Any]] | None = None,
        observed_linear_status: str | None = None,
        observation: dict[str, Any] | None = None,
        stored_feedback_ids: list[str] | None = None,
        transition_request_id: str | None = None,
        requires_feedback: bool = False,
        feedback_kind: str | None = None,
        resolved_node: dict[str, Any] | None = None,
        workflow_digest: str | None = None,
    ) -> dict[str, Any]:
        prior = self.connection.execute(
            "SELECT td.* FROM transition_decisions td JOIN events e ON e.seq=td.event_seq "
            "WHERE e.idempotency_key=?", (idempotency_key,),
        ).fetchone()
        if prior:
            return dict(prior)
        with self.transaction() as db:
            execution = db.execute(
                "SELECT * FROM workflow_executions WHERE id=?", (execution_id,)
            ).fetchone()
            if not execution or execution["status"] != "running":
                raise LedgerError("execution is not running")
            if execution["current_state_id"] != from_state:
                raise LedgerError("transition source is stale")
            review_feedback = list(feedback or [])
            existing_feedback_rows: list[sqlite3.Row] = []
            if stored_feedback_ids:
                placeholders = ",".join("?" for _ in stored_feedback_ids)
                existing_feedback_rows = db.execute(
                    f"SELECT * FROM feedback WHERE execution_id=? AND id IN ({placeholders})",
                    (execution_id, *stored_feedback_ids),
                ).fetchall()
                if len(existing_feedback_rows) != len(set(stored_feedback_ids)):
                    raise LedgerError("stored review feedback is missing")
                review_feedback.extend(
                    json.loads(row["body_json"]) for row in existing_feedback_rows
                )
            if requires_feedback and not review_feedback:
                raise LedgerError("transition requires durable feedback")
            if review_feedback:
                validate_feedback(review_feedback, expected_kind=feedback_kind)
            if transition_request_id:
                request = db.execute(
                    "SELECT * FROM transition_requests WHERE id=? AND execution_id=? "
                    "AND status='pending'",
                    (transition_request_id, execution_id),
                ).fetchone()
                if (
                    not request or request["edge_id"] != edge_id
                    or request["from_state"] != from_state or request["to_state"] != to_state
                ):
                    raise LedgerError("pending transition request is stale")
            current_run = db.execute(
                "SELECT * FROM state_runs WHERE id=?", (execution["current_state_run_id"],)
            ).fetchone()
            released_lease_ids = []
            release_pending_allocation_ids = []
            completed_attempt_facts = None
            if current_run["state_kind"] == "work":
                active = db.execute(
                    "SELECT * FROM attempts WHERE state_run_id=? AND status='active'",
                    (current_run["id"],),
                ).fetchone()
                if not active or active["id"] != attempt_id or active["fence_token"] != fence_token:
                    raise StaleAttempt("attempt is missing, completed, or fenced")
                if not outcome or not evidence:
                    raise LedgerError("leaving work requires outcome and evidence")
                for item in evidence:
                    if not isinstance(item, dict) or not item.get("kind") or not item.get("uri"):
                        raise LedgerError("evidence requires kind and uri")
                attempt_completed_at = self.clock()
                db.execute(
                    "UPDATE attempts SET status='completed',completed_at=?,outcome=? WHERE id=?",
                    (attempt_completed_at, outcome, attempt_id),
                )
                completed_attempt_facts = {
                    "attempt_id": str(active["id"]),
                    "owner": str(active["owner"]),
                    "actor": str(active["actor"]),
                    "state_run_id": str(current_run["id"]),
                    "state": str(current_run["state_id"]),
                    "ordinal": int(current_run["ordinal"]),
                    "started_at": str(active["started_at"]),
                    "completed_at": attempt_completed_at,
                    "outcome": outcome,
                }
                self._fault("after_attempt_completed")
                released_lease_ids = [
                    str(row["id"]) for row in db.execute(
                        "SELECT id FROM resource_leases WHERE attempt_id=? AND status='active'",
                        (attempt_id,),
                    )
                ]
                db.execute(
                    "UPDATE resource_leases SET status='released',released_at=? "
                    "WHERE attempt_id=? AND status='active'",
                    (self.clock(), attempt_id),
                )
                release_pending_allocation_ids = [
                    str(row["id"]) for row in db.execute(
                        "SELECT id FROM resource_allocations WHERE attempt_id=? "
                        "AND status='active'", (attempt_id,),
                    )
                ]
                db.execute(
                    "UPDATE resource_allocations SET status='release_pending' "
                    "WHERE attempt_id=? AND status='active'", (attempt_id,),
                )
                self._fault("after_leases_released")
            db.execute(
                "UPDATE state_runs SET status='completed',completed_at=? WHERE id=?",
                (self.clock(), current_run["id"]),
            )
            self._fault("after_current_state_run_completed")
            next_run_id = self.id_factory()
            ordinal = int(current_run["ordinal"]) + 1
            next_run_started_at = self.clock()
            db.execute(
                "INSERT INTO state_runs VALUES(?,?,?,?,?,?,?,?,?)",
                (next_run_id, execution_id, to_state, to_kind, ordinal,
                 "completed" if terminal else "active", None, next_run_started_at,
                 self.clock() if terminal else None),
            )
            self._fault("after_next_state_run_created")
            next_attempt = None
            next_fence = None
            entered_attempt_facts = None
            if to_kind == "work" and not terminal:
                if not owner:
                    raise LedgerError("entering work requires an owner")
                next_attempt, next_fence = self.id_factory(), self.id_factory()
                next_attempt_started_at = self.clock()
                db.execute(
                    "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (next_attempt, next_run_id, owner, actor, "active", next_fence,
                     next_attempt_started_at, next_attempt_started_at, None, None),
                )
                if workflow_digest:
                    self._bind_attempt(
                        db, attempt_id=next_attempt, workflow_digest=workflow_digest,
                        state_id=to_state, resolved_node=resolved_node or {},
                    )
                entered_attempt_facts = {
                    "attempt_id": next_attempt,
                    "owner": owner,
                    "actor": actor,
                    "state_run_id": next_run_id,
                    "state": to_state,
                    "ordinal": ordinal,
                    "started_at": next_attempt_started_at,
                }
                self._fault("after_next_attempt_created")
            decision_id = self.id_factory()
            artifact_ids = []
            for item in evidence:
                if not isinstance(item, dict) or not item.get("kind") or not item.get("uri"):
                    raise LedgerError("evidence requires kind and uri")
                artifact_id = self.id_factory()
                metadata = {key: value for key, value in item.items() if key not in ("kind", "uri")}
                db.execute(
                    "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                    (artifact_id, execution_id, attempt_id, item["kind"], item["uri"],
                     json.dumps(redact_payload(metadata), sort_keys=True), self.clock()),
                )
                artifact_ids.append(artifact_id)
            self._fault("after_artifacts_recorded")
            feedback_ids = [str(row["id"]) for row in existing_feedback_rows]
            feedback_target = next_attempt or decision_id
            for item in feedback or []:
                source = item.get("source")
                if not isinstance(source, str) or not source.strip():
                    raise LedgerError("feedback requires a source")
                feedback_id = self.id_factory()
                db.execute(
                    "INSERT INTO feedback VALUES(?,?,?,?,?,?)",
                    (feedback_id, execution_id, source, feedback_target,
                     json.dumps(redact_payload(item), sort_keys=True), self.clock()),
                )
                feedback_ids.append(feedback_id)
            if feedback_ids:
                placeholders = ",".join("?" for _ in feedback_ids)
                db.execute(
                    f"UPDATE feedback SET target_id=? WHERE id IN ({placeholders})",
                    (feedback_target, *feedback_ids),
                )
            if completed_attempt_facts is not None:
                completed_attempt_facts["evidence"] = evidence
                completed_attempt_facts["artifact_ids"] = artifact_ids
            if entered_attempt_facts is not None:
                entered_attempt_facts["feedback"] = review_feedback
                entered_attempt_facts["feedback_ids"] = feedback_ids
            pending_parameters: list[Any] = [execution_id]
            pending_query = (
                "SELECT id FROM transition_requests WHERE execution_id=? AND status='pending'"
            )
            if transition_request_id:
                pending_query += " AND id<>?"
                pending_parameters.append(transition_request_id)
            superseded_request_ids = [
                str(row["id"]) for row in db.execute(
                    pending_query, tuple(pending_parameters)
                ).fetchall()
            ]
            if superseded_request_ids:
                placeholders = ",".join("?" for _ in superseded_request_ids)
                db.execute(
                    f"UPDATE transition_requests SET status='superseded',consumed_at=? "
                    f"WHERE id IN ({placeholders})",
                    (self.clock(), *superseded_request_ids),
                )
            payload = {"edge_id": edge_id, "from": from_state, "to": to_state,
                       "actor": actor, "signal": signal, "outcome": outcome,
                       "evidence": evidence, "attempt_id": next_attempt,
                       "artifact_ids": artifact_ids,
                       "released_lease_ids": released_lease_ids,
                       "release_pending_allocation_ids": release_pending_allocation_ids,
                       "feedback": review_feedback, "feedback_ids": feedback_ids,
                       "completed_attempt": completed_attempt_facts,
                       "entered_attempt": entered_attempt_facts,
                       "superseded_transition_request_ids": superseded_request_ids,
                       "observation": observation,
                       "workflow_digest": workflow_digest,
                       "resolved_node": resolved_node or {}}
            seq = self._event(db, execution_id=execution_id, state_run_id=next_run_id,
                              attempt_id=next_attempt, event_type="transition_accepted",
                              payload=payload, idempotency_key=idempotency_key)
            self._fault("after_event_recorded")
            db.execute(
                "INSERT INTO transition_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (decision_id, execution_id, edge_id, from_state, to_state, actor, signal,
                 desired_linear_status, seq, self.clock()),
            )
            self._fault("after_transition_decision_recorded")
            db.execute(
                "UPDATE workflow_executions SET status=?,current_state_id=?,"
                "desired_linear_status=?,observed_linear_status=COALESCE(?,observed_linear_status),"
                "current_state_run_id=?,completed_at=? WHERE id=?",
                ("completed" if terminal else "running", to_state, desired_linear_status,
                 observed_linear_status, next_run_id,
                 self.clock() if terminal else None, execution_id),
            )
            if transition_request_id:
                db.execute(
                    "UPDATE transition_requests SET status='consumed',consumed_at=? WHERE id=?",
                    (self.clock(), transition_request_id),
                )
            self._fault("after_execution_updated")
        self._fault("transition_committed")
        return {"id": decision_id, "event_seq": seq, "attempt_id": next_attempt,
                "fence_token": next_fence, "to_state": to_state,
                "feedback_ids": feedback_ids}

    def acquire_resource(
        self, resource_id: str, *, attempt_id: str, fence_token: str,
        expires_at: str, idempotency_key: str,
    ) -> str:
        prior = self.event_for_command(idempotency_key)
        if prior:
            return str(prior["payload"]["lease_id"])
        self.reap_expired_leases(resource_id=resource_id)
        if parse_timestamp(expires_at) <= parse_timestamp(self.clock()):
            raise LedgerError("resource lease expiry must be in the future")
        with self.transaction() as db:
            attempt = db.execute(
                "SELECT a.*,sr.execution_id,sr.id AS current_state_run_id "
                "FROM attempts a JOIN state_runs sr ON sr.id=a.state_run_id WHERE a.id=?",
                (attempt_id,),
            ).fetchone()
            if (
                not attempt or attempt["status"] != "active"
                or attempt["fence_token"] != fence_token
            ):
                raise StaleAttempt("attempt is missing, completed, or fenced")
            lease_id = self.id_factory()
            now = self.clock()
            try:
                db.execute(
                    "INSERT INTO resource_leases VALUES(?,?,?,?,?,?,?,?,?)",
                    (lease_id, resource_id, attempt_id, fence_token, "active", now, now,
                     expires_at, None),
                )
            except sqlite3.IntegrityError as error:
                raise LedgerError(f"resource is already leased: {resource_id}") from error
            self._event(
                db, execution_id=attempt["execution_id"],
                state_run_id=attempt["current_state_run_id"], attempt_id=attempt_id,
                event_type="resource_lease_acquired",
                payload={"lease_id": lease_id, "resource_id": resource_id,
                         "expires_at": expires_at},
                idempotency_key=idempotency_key,
            )
        return lease_id

    def _active_attempt(
        self, db: sqlite3.Connection, attempt_id: str, fence_token: str
    ) -> sqlite3.Row:
        attempt = db.execute(
            "SELECT a.*,sr.execution_id,sr.id AS state_run_id "
            "FROM attempts a JOIN state_runs sr ON sr.id=a.state_run_id WHERE a.id=?",
            (attempt_id,),
        ).fetchone()
        if (
            not attempt or attempt["status"] != "active"
            or attempt["fence_token"] != fence_token
        ):
            raise StaleAttempt("attempt is missing, completed, or fenced")
        return attempt

    def assert_attempt_active(self, attempt_id: str, fence_token: str) -> dict[str, Any]:
        return dict(self._active_attempt(self.connection, attempt_id, fence_token))

    def _runner_run_dict(
        self, row: sqlite3.Row, *, include_events: bool = True
    ) -> dict[str, Any]:
        item = dict(row)
        for source, target in (
            ("command_json", "command"), ("result_json", "result"),
            ("receipt_json", "receipt"), ("error_json", "error"),
        ):
            value = item.pop(source)
            item[target] = json.loads(value) if value else None
        if include_events:
            item["events"] = [
                {
                    **dict(event),
                    "payload": json.loads(event["payload_json"]),
                }
                for event in self.connection.execute(
                    "SELECT * FROM runner_events WHERE runner_run_id=? ORDER BY sequence",
                    (item["id"],),
                )
            ]
            for event in item["events"]:
                event.pop("payload_json", None)
        return item

    def runner_run(self, runner_run_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM runner_runs WHERE id=?", (runner_run_id,)
        ).fetchone()
        if not row:
            raise LedgerError("runner run not found")
        return self._runner_run_dict(row)

    def runner_run_for_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM runner_runs WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        return self._runner_run_dict(row) if row else None

    def _active_runner_run(
        self, db: sqlite3.Connection, runner_run_id: str, fence_token: str,
        statuses: tuple[str, ...],
    ) -> sqlite3.Row:
        placeholders = ",".join("?" for _ in statuses)
        run = db.execute(
            "SELECT rr.*,a.status AS attempt_status,a.fence_token AS active_fence,"
            "a.state_run_id FROM runner_runs rr JOIN attempts a ON a.id=rr.attempt_id "
            f"WHERE rr.id=? AND rr.status IN ({placeholders})",
            (runner_run_id, *statuses),
        ).fetchone()
        if (
            not run or run["fence_token"] != fence_token
            or run["attempt_status"] != "active" or run["active_fence"] != fence_token
        ):
            raise StaleAttempt("runner run is missing, terminal, or fenced")
        return run

    def plan_runner_run(
        self, *, execution_id: str, attempt_id: str, preparation_id: str,
        preparation_digest: str, fence_token: str, runner_key: str,
        adapter_kind: str, adapter_version: str, protocol_version: int,
        execution_trace_id: str, trace_id: str, root_span_id: str,
        parent_trace_id: str | None, command: list[str], command_digest: str,
        prompt_digest: str, host_id: str, boot_id: str,
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT * FROM runner_runs WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if existing:
            expected = (
                execution_id, preparation_id, fence_token, runner_key, adapter_kind,
                adapter_version, protocol_version, command_digest, prompt_digest,
            )
            observed = tuple(existing[key] for key in (
                "execution_id", "preparation_id", "fence_token", "runner_key",
                "adapter_kind", "adapter_version", "protocol_version",
                "command_digest", "prompt_digest",
            ))
            if observed != expected:
                raise LedgerError("runner plan changed after durable creation")
            self.assert_attempt_active(attempt_id, fence_token)
            return self._runner_run_dict(existing)
        with self.transaction() as db:
            attempt = self._active_attempt(db, attempt_id, fence_token)
            if attempt["execution_id"] != execution_id:
                raise StaleAttempt("runner execution does not own the active attempt")
            preparation = db.execute(
                "SELECT * FROM preparations WHERE id=? AND attempt_id=?",
                (preparation_id, attempt_id),
            ).fetchone()
            if (
                not preparation or preparation["status"] != "ready"
                or preparation["fence_token"] != fence_token
                or preparation["result_digest"] != preparation_digest
            ):
                raise StaleAttempt("runner launch does not match ready preparation")
            runner_run_id = self.id_factory()
            now = self.clock()
            db.execute(
                "INSERT INTO runner_runs("
                "id,execution_id,attempt_id,preparation_id,fence_token,runner_key,"
                "adapter_kind,adapter_version,protocol_version,execution_trace_id,"
                "trace_id,root_span_id,parent_trace_id,status,command_json,command_digest,"
                "prompt_digest,host_id,boot_id,created_at,last_activity_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'planned',?,?,?,?,?,?,?)",
                (runner_run_id, execution_id, attempt_id, preparation_id, fence_token,
                 runner_key, adapter_kind, adapter_version, protocol_version,
                 execution_trace_id, trace_id, root_span_id, parent_trace_id,
                 json.dumps(redact_payload(command), sort_keys=True), command_digest,
                 prompt_digest, host_id, boot_id, now, now),
            )
            self._event(
                db, execution_id=execution_id, state_run_id=attempt["state_run_id"],
                attempt_id=attempt_id, event_type="runner_planned",
                payload={
                    "runner_run_id": runner_run_id, "runner": runner_key,
                    "adapter_kind": adapter_kind, "adapter_version": adapter_version,
                    "protocol_version": protocol_version, "trace_id": trace_id,
                    "root_span_id": root_span_id, "command_digest": command_digest,
                    "prompt_digest": prompt_digest,
                },
                idempotency_key=f"runner:{runner_run_id}:planned",
            )
        return self.runner_run(runner_run_id)

    def mark_runner_starting(
        self, runner_run_id: str, *, fence_token: str
    ) -> dict[str, Any]:
        current = self.runner_run(runner_run_id)
        if current["status"] == "starting":
            return current
        with self.transaction() as db:
            run = self._active_runner_run(
                db, runner_run_id, fence_token, ("planned",)
            )
            now = self.clock()
            db.execute(
                "UPDATE runner_runs SET status='starting',started_at=?,last_activity_at=? "
                "WHERE id=?", (now, now, runner_run_id),
            )
            self._event(
                db, execution_id=run["execution_id"], state_run_id=run["state_run_id"],
                attempt_id=run["attempt_id"], event_type="runner_starting",
                payload={"runner_run_id": runner_run_id, "trace_id": run["trace_id"]},
                idempotency_key=(
                    f"runner:{runner_run_id}:starting:{run['resume_count']}"
                ),
            )
        return self.runner_run(runner_run_id)

    def mark_runner_running(
        self, runner_run_id: str, *, fence_token: str, pid: int,
        process_group_id: int,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            run = self._active_runner_run(
                db, runner_run_id, fence_token, ("starting",)
            )
            now = self.clock()
            db.execute(
                "UPDATE runner_runs SET status='running',pid=?,process_group_id=?,"
                "last_activity_at=? WHERE id=?",
                (pid, process_group_id, now, runner_run_id),
            )
            self._event(
                db, execution_id=run["execution_id"], state_run_id=run["state_run_id"],
                attempt_id=run["attempt_id"], event_type="runner_started",
                payload={"runner_run_id": runner_run_id, "pid": pid,
                         "process_group_id": process_group_id,
                         "trace_id": run["trace_id"]},
                idempotency_key=(
                    f"runner:{runner_run_id}:running:{run['resume_count']}"
                ),
            )
        return self.runner_run(runner_run_id)

    def append_runner_event(
        self, runner_run_id: str, *, fence_token: str, kind: str,
        protocol_type: str, stream: str, payload: dict[str, Any],
        span_id: str, parent_span_id: str | None, source_occurred_at: str | None,
        observed_at: str, origin: str, trust_class: str,
        session_id: str | None = None, maximum_payload_bytes: int = 262144,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            run = self._active_runner_run(
                db, runner_run_id, fence_token,
                ("starting", "running", "waiting_input"),
            )
            redacted = redact_payload(payload)
            encoded = json.dumps(
                redacted, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            payload_bytes = len(encoded)
            truncated = 0
            if payload_bytes > maximum_payload_bytes:
                redacted = {
                    "capture": "truncated",
                    "original_bytes": payload_bytes,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "top_level_keys": sorted(redacted) if isinstance(redacted, dict) else [],
                }
                encoded = json.dumps(redacted, sort_keys=True).encode("utf-8")
                truncated = 1
            sequence = int(run["event_count"]) + 1
            event_id = self.id_factory()
            db.execute(
                "INSERT INTO runner_events("
                "event_id,runner_run_id,sequence,execution_id,attempt_id,trace_id,span_id,"
                "parent_span_id,kind,protocol_type,stream,source_occurred_at,observed_at,"
                "origin,trust_class,payload_json,payload_bytes,truncated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, runner_run_id, sequence, run["execution_id"],
                 run["attempt_id"], run["trace_id"], span_id, parent_span_id,
                 kind, protocol_type, stream, source_occurred_at, observed_at,
                 origin, trust_class, encoded.decode("utf-8"), payload_bytes, truncated),
            )
            self._fault("after_runner_trace_source_recorded")
            db.execute(
                "UPDATE runner_runs SET event_count=?,last_activity_at=?,"
                "session_id=COALESCE(?,session_id) WHERE id=?",
                (sequence, observed_at, session_id, runner_run_id),
            )
            stored = db.execute(
                "SELECT * FROM runner_events WHERE event_id=?", (event_id,)
            ).fetchone()
            self._mirror_runner_trace(db, row=stored, run=run)
        return {
            "event_id": event_id, "runner_run_id": runner_run_id,
            "sequence": sequence, "truncated": bool(truncated),
        }

    def mark_runner_waiting_input(
        self, runner_run_id: str, *, fence_token: str, attention_id: str
    ) -> dict[str, Any]:
        with self.transaction() as db:
            run = self._active_runner_run(
                db, runner_run_id, fence_token, ("starting", "running")
            )
            db.execute(
                "UPDATE runner_runs SET status='waiting_input',attention_id=?,"
                "last_activity_at=? WHERE id=?",
                (attention_id, self.clock(), runner_run_id),
            )
            self._event(
                db, execution_id=run["execution_id"], state_run_id=run["state_run_id"],
                attempt_id=run["attempt_id"], event_type="runner_waiting_input",
                payload={"runner_run_id": runner_run_id,
                         "attention_id": attention_id,
                         "trace_id": run["trace_id"]},
                idempotency_key=(
                    f"runner:{runner_run_id}:waiting-input:{run['resume_count']}"
                ),
            )
        return self.runner_run(runner_run_id)

    def add_runner_dropped_events(
        self, runner_run_id: str, *, fence_token: str, count: int
    ) -> dict[str, Any]:
        if count < 1:
            raise LedgerError("dropped runner event count must be positive")
        with self.transaction() as db:
            run = self._active_runner_run(
                db, runner_run_id, fence_token,
                ("starting", "running", "waiting_input"),
            )
            db.execute(
                "UPDATE runner_runs SET dropped_event_count=dropped_event_count+?,"
                "last_activity_at=? WHERE id=?",
                (count, self.clock(), runner_run_id),
            )
        return self.runner_run(runner_run_id)

    def remedy_runner_attention(
        self, *, execution_id: str, attention_id: str, remedy: str,
        command_id: str, expected_attempt_id: str | None,
    ) -> dict[str, Any]:
        if remedy not in ("retry", "cancel"):
            raise LedgerError("runner attention supports only retry or cancel")
        with self.transaction() as db:
            attention = db.execute(
                "SELECT * FROM attention_requests WHERE id=? AND execution_id=?",
                (attention_id, execution_id),
            ).fetchone()
            if not attention or attention["status"] != "open":
                raise LedgerError("runner attention is missing or already resolved")
            detail = json.loads(attention["detail_json"])
            if remedy not in detail.get("allowed_actions", []):
                raise LedgerError(f"{remedy} is not allowed for this attention request")
            attempt_id = str(attention["attempt_id"] or "")
            if not attempt_id or expected_attempt_id != attempt_id:
                raise StaleAttempt("runner attention does not match the expected attempt")
            run = db.execute(
                "SELECT rr.*,a.state_run_id FROM runner_runs rr JOIN attempts a "
                "ON a.id=rr.attempt_id WHERE rr.attempt_id=? AND rr.attention_id=?",
                (attempt_id, attention_id),
            ).fetchone()
            if not run or run["status"] != "waiting_input":
                raise LedgerError("runner is not waiting on this attention request")
            self._active_attempt(db, attempt_id, str(run["fence_token"]))
            now = self.clock()
            resolution = "canceled" if remedy == "cancel" else "resolved"
            detail["resolution"] = {
                "remedy": remedy, "command_id": command_id,
            }
            db.execute(
                "UPDATE attention_requests SET status=?,detail_json=?,updated_at=?,"
                "resolved_at=? WHERE id=?",
                (resolution, json.dumps(redact_payload(detail), sort_keys=True),
                 now, now, attention_id),
            )
            error = None
            if remedy == "cancel":
                error = {
                    "class": "canceled", "message": "runner canceled by operator",
                    "command_id": command_id,
                }
            db.execute(
                "UPDATE runner_runs SET status=?,attention_id=NULL,error_json=?,"
                "last_activity_at=?,completed_at=? WHERE id=?",
                ("canceled" if remedy == "cancel" else "resume_authorized",
                 json.dumps(error, sort_keys=True) if error else None,
                 now, now if remedy == "cancel" else None, run["id"]),
            )
            self._event(
                db, execution_id=execution_id, state_run_id=run["state_run_id"],
                attempt_id=attempt_id, event_type="attention_resolved",
                payload={"attention_id": attention_id, "resolution": resolution,
                         "detail": detail["resolution"]},
                idempotency_key=f"attention:{attention_id}:{resolution}",
            )
            self._event(
                db, execution_id=execution_id, state_run_id=run["state_run_id"],
                attempt_id=attempt_id,
                event_type=(
                    "runner_resume_authorized" if remedy == "retry"
                    else "runner_canceled"
                ),
                payload={"runner_run_id": run["id"], "attention_id": attention_id,
                         "command_id": command_id, "trace_id": run["trace_id"]},
                idempotency_key=f"runner:{run['id']}:attention:{command_id}",
            )
        return {
            "attention": self.attention(attention_id), "remedy": remedy,
            "runner_run": self.runner_run(str(run["id"])),
        }

    def plan_runner_resume(
        self, runner_run_id: str, *, fence_token: str, command: list[str],
        command_digest: str,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            run = self._active_runner_run(
                db, runner_run_id, fence_token, ("resume_authorized",)
            )
            if not run["session_id"]:
                raise LedgerError("runner resume requires a durable session reference")
            resume_count = int(run["resume_count"]) + 1
            now = self.clock()
            db.execute(
                "UPDATE runner_runs SET status='planned',command_json=?,command_digest=?,"
                "resume_count=?,pid=NULL,process_group_id=NULL,error_json=NULL,"
                "started_at=NULL,last_activity_at=?,completed_at=NULL WHERE id=?",
                (json.dumps(redact_payload(command), sort_keys=True), command_digest,
                 resume_count, now, runner_run_id),
            )
            self._event(
                db, execution_id=run["execution_id"], state_run_id=run["state_run_id"],
                attempt_id=run["attempt_id"], event_type="runner_resume_planned",
                payload={"runner_run_id": runner_run_id, "resume_count": resume_count,
                         "trace_id": run["trace_id"], "session_id": run["session_id"],
                         "command_digest": command_digest},
                idempotency_key=f"runner:{runner_run_id}:resume:{resume_count}",
            )
        return self.runner_run(runner_run_id)

    def record_runner_result(
        self, runner_run_id: str, *, fence_token: str, result: dict[str, Any],
        receipt: dict[str, Any], session_id: str | None,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            run = self._active_runner_run(
                db, runner_run_id, fence_token, ("running",)
            )
            now = self.clock()
            db.execute(
                "UPDATE runner_runs SET status='result_ready',result_json=?,receipt_json=?,"
                "session_id=COALESCE(?,session_id),last_activity_at=?,completed_at=? "
                "WHERE id=?",
                (json.dumps(redact_payload(result), sort_keys=True),
                 json.dumps(redact_payload(receipt), sort_keys=True), session_id,
                 now, now, runner_run_id),
            )
            self._event(
                db, execution_id=run["execution_id"], state_run_id=run["state_run_id"],
                attempt_id=run["attempt_id"], event_type="runner_result_ready",
                payload={"runner_run_id": runner_run_id, "trace_id": run["trace_id"],
                         "result": result, "receipt": receipt},
                idempotency_key=f"runner:{runner_run_id}:result-ready",
            )
        return self.runner_run(runner_run_id)

    def finish_runner_run(
        self, runner_run_id: str, *, fence_token: str, status: str,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in ("failed", "canceled"):
            raise LedgerError("invalid terminal runner status")
        current = self.runner_run(runner_run_id)
        if current["status"] in ("failed", "canceled"):
            return current
        with self.transaction() as db:
            run = self._active_runner_run(
                db, runner_run_id, fence_token,
                ("planned", "starting", "running", "waiting_input"),
            )
            now = self.clock()
            db.execute(
                "UPDATE runner_runs SET status=?,error_json=?,last_activity_at=?,"
                "completed_at=? WHERE id=?",
                (status, json.dumps(redact_payload(error or {}), sort_keys=True),
                 now, now, runner_run_id),
            )
            self._event(
                db, execution_id=run["execution_id"], state_run_id=run["state_run_id"],
                attempt_id=run["attempt_id"], event_type=f"runner_{status}",
                payload={"runner_run_id": runner_run_id, "trace_id": run["trace_id"],
                         "error": error or {}},
                idempotency_key=f"runner:{runner_run_id}:{status}",
            )
        return self.runner_run(runner_run_id)

    def supersede_runner_run(
        self, runner_run_id: str, *, reason: str
    ) -> dict[str, Any]:
        current = self.runner_run(runner_run_id)
        if current["status"] == "superseded":
            return current
        if current["status"] in ("result_ready", "failed", "canceled"):
            raise LedgerError("terminal runner run cannot be superseded")
        with self.transaction() as db:
            row = db.execute(
                "SELECT rr.*,a.state_run_id FROM runner_runs rr JOIN attempts a "
                "ON a.id=rr.attempt_id WHERE rr.id=?", (runner_run_id,)
            ).fetchone()
            now = self.clock()
            error = {"class": "stale_attempt", "message": reason}
            db.execute(
                "UPDATE runner_runs SET status='superseded',error_json=?,"
                "last_activity_at=?,completed_at=? WHERE id=?",
                (json.dumps(error, sort_keys=True), now, now, runner_run_id),
            )
            self._event(
                db, execution_id=row["execution_id"], state_run_id=row["state_run_id"],
                attempt_id=row["attempt_id"], event_type="runner_superseded",
                payload={"runner_run_id": runner_run_id, "trace_id": row["trace_id"],
                         "error": error},
                idempotency_key=f"runner:{runner_run_id}:superseded",
            )
        return self.runner_run(runner_run_id)

    def begin_preparation(
        self, *, attempt_id: str, fence_token: str, request_digest: str
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT * FROM preparations WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if existing:
            if (
                existing["fence_token"] != fence_token
                or existing["request_digest"] != request_digest
            ):
                raise StaleAttempt("preparation binding does not match the active attempt")
            self.assert_attempt_active(attempt_id, fence_token)
            return self.preparation(str(existing["id"]))
        with self.transaction() as db:
            attempt = self._active_attempt(db, attempt_id, fence_token)
            preparation_id = self.id_factory()
            now = self.clock()
            db.execute(
                "INSERT INTO preparations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (preparation_id, attempt_id, attempt["execution_id"], fence_token,
                 request_digest, "preparing", None, None, None, now, now, None),
            )
            self._fault("after_preparation_created")
            self._event(
                db, execution_id=attempt["execution_id"],
                state_run_id=attempt["state_run_id"], attempt_id=attempt_id,
                event_type="preparation_started",
                payload={"preparation_id": preparation_id,
                         "request_digest": request_digest},
                idempotency_key=f"preparation:{preparation_id}:started",
            )
        return self.preparation(preparation_id)

    def preparation(self, preparation_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM preparations WHERE id=?", (preparation_id,)
        ).fetchone()
        if not row:
            raise LedgerError("preparation not found")
        item = dict(row)
        for source, target in (
            ("prepared_json", "prepared"), ("error_json", "error")
        ):
            value = item.pop(source)
            item[target] = json.loads(value) if value else None
        item["allocations"] = [
            self._allocation_dict(allocation) for allocation in self.connection.execute(
                "SELECT * FROM resource_allocations WHERE preparation_id=? "
                "ORDER BY provider,capability,resource_id", (preparation_id,)
            )
        ]
        item["mutations"] = [
            self._mutation_dict(mutation) for mutation in self.connection.execute(
                "SELECT * FROM resource_mutations WHERE preparation_id=? "
                "ORDER BY planned_at,id", (preparation_id,)
            )
        ]
        return item

    def preparation_for_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT id FROM preparations WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        return self.preparation(str(row["id"])) if row else None

    def resume_preparation(
        self, preparation_id: str, *, fence_token: str
    ) -> dict[str, Any]:
        with self.transaction() as db:
            preparation = db.execute(
                "SELECT * FROM preparations WHERE id=?", (preparation_id,)
            ).fetchone()
            if not preparation:
                raise LedgerError("preparation not found")
            self._active_attempt(db, preparation["attempt_id"], fence_token)
            if preparation["fence_token"] != fence_token:
                raise StaleAttempt("preparation is fenced")
            if preparation["status"] in ("busy", "failed"):
                db.execute(
                    "UPDATE preparations SET status='preparing',error_json=NULL,"
                    "updated_at=?,completed_at=NULL WHERE id=?",
                    (self.clock(), preparation_id),
                )
            elif preparation["status"] not in ("preparing", "ready"):
                raise LedgerError("preparation requires operator attention")
        return self.preparation(preparation_id)

    def plan_mutation(
        self, preparation_id: str, *, fence_token: str, provider: str,
        step_key: str, action: str, target: str, intent: dict[str, Any],
        allocation_id: str | None = None,
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT * FROM resource_mutations WHERE preparation_id=? AND provider=? "
            "AND step_key=?", (preparation_id, provider, step_key),
        ).fetchone()
        if existing:
            return self._mutation_dict(existing)
        with self.transaction() as db:
            preparation = db.execute(
                "SELECT * FROM preparations WHERE id=?", (preparation_id,)
            ).fetchone()
            if not preparation or preparation["status"] != "preparing":
                raise LedgerError("preparation is not mutable")
            attempt = self._active_attempt(db, preparation["attempt_id"], fence_token)
            if preparation["fence_token"] != fence_token:
                raise StaleAttempt("preparation is fenced")
            mutation_id = self.id_factory()
            db.execute(
                "INSERT INTO resource_mutations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mutation_id, preparation_id, allocation_id, provider, step_key,
                 action, target, "planned",
                 json.dumps(redact_payload(intent), sort_keys=True), None, None,
                 self.clock(), None, None),
            )
            self._fault("after_resource_mutation_planned")
            self._event(
                db, execution_id=attempt["execution_id"],
                state_run_id=attempt["state_run_id"],
                attempt_id=preparation["attempt_id"], event_type="resource_mutation_planned",
                payload={"preparation_id": preparation_id, "mutation_id": mutation_id,
                         "provider": provider, "action": action, "target": target},
                idempotency_key=f"mutation:{mutation_id}:planned",
            )
        return self._mutation_dict(self.connection.execute(
            "SELECT * FROM resource_mutations WHERE id=?", (mutation_id,)
        ).fetchone())

    def start_mutation(self, mutation_id: str, *, fence_token: str) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute(
                "SELECT rm.*,p.attempt_id,p.fence_token AS preparation_fence,"
                "p.execution_id FROM resource_mutations rm JOIN preparations p "
                "ON p.id=rm.preparation_id WHERE rm.id=?", (mutation_id,),
            ).fetchone()
            if not row:
                raise LedgerError("resource mutation not found")
            self._active_attempt(db, row["attempt_id"], fence_token)
            if row["preparation_fence"] != fence_token:
                raise StaleAttempt("resource mutation is fenced")
            if row["status"] == "planned":
                db.execute(
                    "UPDATE resource_mutations SET status='started',started_at=? WHERE id=?",
                    (self.clock(), mutation_id),
                )
        return self._mutation_dict(self.connection.execute(
            "SELECT * FROM resource_mutations WHERE id=?", (mutation_id,)
        ).fetchone())

    def finish_mutation(
        self, mutation_id: str, *, fence_token: str, status: str,
        result: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in ("completed", "failed", "compensated", "skipped", "quarantined"):
            raise LedgerError("invalid resource mutation result")
        with self.transaction() as db:
            row = db.execute(
                "SELECT rm.*,p.attempt_id,p.fence_token AS preparation_fence,"
                "p.execution_id,a.state_run_id FROM resource_mutations rm "
                "JOIN preparations p ON p.id=rm.preparation_id "
                "JOIN attempts a ON a.id=p.attempt_id WHERE rm.id=?", (mutation_id,),
            ).fetchone()
            if not row:
                raise LedgerError("resource mutation not found")
            self._active_attempt(db, row["attempt_id"], fence_token)
            if row["preparation_fence"] != fence_token:
                raise StaleAttempt("resource mutation is fenced")
            if row["status"] in ("completed", "compensated", "skipped", "quarantined"):
                return self._mutation_dict(row)
            db.execute(
                "UPDATE resource_mutations SET status=?,result_json=?,error_json=?,"
                "completed_at=? WHERE id=?",
                (status,
                 json.dumps(redact_payload(result), sort_keys=True) if result else None,
                 json.dumps(redact_payload(error), sort_keys=True) if error else None,
                 self.clock(), mutation_id),
            )
            self._event(
                db, execution_id=row["execution_id"], state_run_id=row["state_run_id"],
                attempt_id=row["attempt_id"], event_type=f"resource_mutation_{status}",
                payload={"preparation_id": row["preparation_id"],
                         "mutation_id": mutation_id, "provider": row["provider"],
                         "action": row["action"], "result": result, "error": error},
                idempotency_key=f"mutation:{mutation_id}:{status}",
            )
        return self._mutation_dict(self.connection.execute(
            "SELECT * FROM resource_mutations WHERE id=?", (mutation_id,)
        ).fetchone())

    def _mutation_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for source, target in (
            ("intent_json", "intent"), ("result_json", "result"),
            ("error_json", "error"),
        ):
            value = item.pop(source)
            item[target] = json.loads(value) if value else None
        return item

    def acquire_allocation(
        self, preparation_id: str, *, fence_token: str, scope: str,
        provider: str, capability: str, resource_id: str,
        metadata: dict[str, Any] | None = None, expires_at: str | None = None,
    ) -> dict[str, Any]:
        if scope not in ("attempt", "execution"):
            raise LedgerError("resource allocation scope must be attempt or execution")
        existing = self.connection.execute(
            "SELECT * FROM resource_allocations WHERE preparation_id=? AND provider=? "
            "AND capability=? AND resource_id=?",
            (preparation_id, provider, capability, resource_id),
        ).fetchone()
        if existing and existing["status"] in ("active", "release_pending"):
            return self._allocation_dict(existing)
        with self.transaction() as db:
            preparation = db.execute(
                "SELECT * FROM preparations WHERE id=?", (preparation_id,)
            ).fetchone()
            if not preparation or preparation["status"] != "preparing":
                raise LedgerError("preparation is not mutable")
            attempt = self._active_attempt(db, preparation["attempt_id"], fence_token)
            if preparation["fence_token"] != fence_token:
                raise StaleAttempt("preparation is fenced")
            allocation_id = self.id_factory()
            now = self.clock()
            try:
                db.execute(
                    "INSERT INTO resource_allocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (allocation_id, preparation_id, preparation["execution_id"],
                     preparation["attempt_id"] if scope == "attempt" else None,
                     scope, provider, capability, resource_id, fence_token, "active",
                     json.dumps(redact_payload(metadata or {}), sort_keys=True),
                     now, now, expires_at, None),
                )
                self._fault("after_resource_allocation_created")
            except sqlite3.IntegrityError as error:
                raise ResourceBusy(f"resource is already allocated: {resource_id}") from error
            self._event(
                db, execution_id=preparation["execution_id"],
                state_run_id=attempt["state_run_id"],
                attempt_id=preparation["attempt_id"], event_type="resource_allocated",
                payload={"preparation_id": preparation_id,
                         "allocation_id": allocation_id, "provider": provider,
                         "capability": capability, "resource_id": resource_id,
                         "scope": scope, "metadata": metadata or {}},
                idempotency_key=f"allocation:{allocation_id}:acquired",
            )
        return self._allocation_dict(self.connection.execute(
            "SELECT * FROM resource_allocations WHERE id=?", (allocation_id,)
        ).fetchone())

    def _allocation_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def record_allocation_ready(
        self, allocation_id: str, *, fence_token: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        with self.transaction() as db:
            allocation = db.execute(
                "SELECT ra.*,p.attempt_id AS preparation_attempt,a.state_run_id "
                "FROM resource_allocations ra JOIN preparations p ON p.id=ra.preparation_id "
                "JOIN attempts a ON a.id=p.attempt_id WHERE ra.id=?", (allocation_id,),
            ).fetchone()
            if not allocation or allocation["status"] != "active":
                raise LedgerError("resource allocation is not active")
            self._active_attempt(db, allocation["preparation_attempt"], fence_token)
            if allocation["fence_token"] != fence_token:
                raise StaleAttempt("resource allocation is fenced")
            merged = json.loads(allocation["metadata_json"])
            merged.update(redact_payload(metadata))
            db.execute(
                "UPDATE resource_allocations SET metadata_json=?,heartbeat_at=? WHERE id=?",
                (json.dumps(merged, sort_keys=True), self.clock(), allocation_id),
            )
            self._event(
                db, execution_id=allocation["execution_id"],
                state_run_id=allocation["state_run_id"],
                attempt_id=allocation["preparation_attempt"],
                event_type="resource_allocation_ready",
                payload={"allocation_id": allocation_id,
                         "resource_id": allocation["resource_id"],
                         "provider": allocation["provider"], "metadata": metadata},
                idempotency_key=f"allocation:{allocation_id}:ready",
            )
        return self._allocation_dict(self.connection.execute(
            "SELECT * FROM resource_allocations WHERE id=?", (allocation_id,)
        ).fetchone())

    def mark_preparation_ready(
        self, preparation_id: str, *, fence_token: str, result_digest: str,
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        with self.transaction() as db:
            preparation = db.execute(
                "SELECT * FROM preparations WHERE id=?", (preparation_id,)
            ).fetchone()
            if not preparation:
                raise LedgerError("preparation not found")
            attempt = self._active_attempt(db, preparation["attempt_id"], fence_token)
            if preparation["fence_token"] != fence_token:
                raise StaleAttempt("preparation is fenced")
            if preparation["status"] == "ready":
                if preparation["result_digest"] != result_digest:
                    raise LedgerError("prepared result changed after commit")
                return self.preparation(preparation_id)
            incomplete = int(db.execute(
                "SELECT COUNT(*) FROM resource_mutations WHERE preparation_id=? "
                "AND status NOT IN ('completed','compensated','skipped','failed')",
                (preparation_id,),
            ).fetchone()[0])
            if incomplete:
                raise LedgerError("preparation has incomplete resource mutations")
            now = self.clock()
            db.execute(
                "UPDATE preparations SET status='ready',result_digest=?,prepared_json=?,"
                "updated_at=?,completed_at=? WHERE id=?",
                (result_digest, json.dumps(redact_payload(prepared), sort_keys=True),
                 now, now, preparation_id),
            )
            self._fault("after_preparation_ready")
            self._event(
                db, execution_id=preparation["execution_id"],
                state_run_id=attempt["state_run_id"],
                attempt_id=preparation["attempt_id"], event_type="preparation_ready",
                payload={"preparation_id": preparation_id,
                         "result_digest": result_digest, "prepared": prepared},
                idempotency_key=f"preparation:{preparation_id}:ready",
            )
        return self.preparation(preparation_id)

    def fail_preparation(
        self, preparation_id: str, *, fence_token: str, status: str,
        error: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in ("failed", "busy", "needs_attention"):
            raise LedgerError("invalid preparation failure status")
        with self.transaction() as db:
            preparation = db.execute(
                "SELECT * FROM preparations WHERE id=?", (preparation_id,)
            ).fetchone()
            if not preparation:
                raise LedgerError("preparation not found")
            attempt = self._active_attempt(db, preparation["attempt_id"], fence_token)
            if preparation["fence_token"] != fence_token:
                raise StaleAttempt("preparation is fenced")
            now = self.clock()
            occurrence = 1 + int(db.execute(
                "SELECT COUNT(*) FROM events WHERE execution_id=? AND attempt_id=? "
                "AND event_type=?",
                (preparation["execution_id"], preparation["attempt_id"],
                 f"preparation_{status}"),
            ).fetchone()[0])
            db.execute(
                "UPDATE preparations SET status=?,error_json=?,updated_at=?,completed_at=? "
                "WHERE id=?",
                (status, json.dumps(redact_payload(error), sort_keys=True),
                 now, now, preparation_id),
            )
            self._event(
                db, execution_id=preparation["execution_id"],
                state_run_id=attempt["state_run_id"],
                attempt_id=preparation["attempt_id"],
                event_type=f"preparation_{status}",
                payload={"preparation_id": preparation_id, "error": error},
                idempotency_key=(
                    f"preparation:{preparation_id}:{status}:{occurrence}"
                ),
            )
        return self.preparation(preparation_id)

    def open_attention(
        self, *, execution_id: str, attempt_id: str | None,
        preparation_id: str | None, dedupe_key: str, category: str,
        detail: dict[str, Any], capability: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT * FROM attention_requests WHERE dedupe_key=? AND status='open'",
            (dedupe_key,),
        ).fetchone()
        if existing:
            return self._attention_dict(existing)
        with self.transaction() as db:
            execution = db.execute(
                "SELECT current_state_run_id FROM workflow_executions WHERE id=?",
                (execution_id,),
            ).fetchone()
            if not execution:
                raise LedgerError("execution not found")
            attention_id = self.id_factory()
            now = self.clock()
            db.execute(
                "INSERT INTO attention_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attention_id, execution_id, attempt_id, preparation_id, dedupe_key,
                 category, capability, provider, "open",
                 json.dumps(redact_payload(detail), sort_keys=True), now, now, None),
            )
            self._event(
                db, execution_id=execution_id,
                state_run_id=execution["current_state_run_id"], attempt_id=attempt_id,
                event_type="attention_requested",
                payload={"attention_id": attention_id, "category": category,
                         "capability": capability, "provider": provider,
                         "detail": detail},
                idempotency_key=f"attention:{attention_id}:opened",
            )
        return self._attention_dict(self.connection.execute(
            "SELECT * FROM attention_requests WHERE id=?", (attention_id,)
        ).fetchone())

    def _attention_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json"))
        return item

    def attention(self, attention_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM attention_requests WHERE id=?", (attention_id,)
        ).fetchone()
        if not row:
            raise LedgerError("attention request not found")
        return self._attention_dict(row)

    def authorize_preparation_retry(
        self, preparation_id: str, *, fence_token: str, command_id: str,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            preparation = db.execute(
                "SELECT * FROM preparations WHERE id=?", (preparation_id,)
            ).fetchone()
            if not preparation:
                raise LedgerError("preparation not found")
            attempt = self._active_attempt(db, preparation["attempt_id"], fence_token)
            if preparation["fence_token"] != fence_token:
                raise StaleAttempt("preparation is fenced")
            if preparation["status"] != "needs_attention":
                raise LedgerError("preparation is not awaiting attention")
            now = self.clock()
            db.execute(
                "UPDATE preparations SET status='failed',error_json=?,updated_at=?,"
                "completed_at=? WHERE id=?",
                (json.dumps({"retry_authorized_by": command_id}, sort_keys=True),
                 now, now, preparation_id),
            )
            self._event(
                db, execution_id=preparation["execution_id"],
                state_run_id=attempt["state_run_id"],
                attempt_id=preparation["attempt_id"],
                event_type="preparation_retry_authorized",
                payload={"preparation_id": preparation_id, "command_id": command_id},
                idempotency_key=f"preparation:{preparation_id}:retry:{command_id}",
            )
        return self.preparation(preparation_id)

    def cancel_preparation(
        self, preparation_id: str, *, fence_token: str, command_id: str,
        reason: str,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            preparation = db.execute(
                "SELECT * FROM preparations WHERE id=?", (preparation_id,)
            ).fetchone()
            if not preparation:
                raise LedgerError("preparation not found")
            attempt = self._active_attempt(db, preparation["attempt_id"], fence_token)
            if preparation["fence_token"] != fence_token:
                raise StaleAttempt("preparation is fenced")
            if preparation["status"] == "canceled":
                return self.preparation(preparation_id)
            if preparation["status"] != "needs_attention":
                raise LedgerError("preparation is not awaiting attention")
            now = self.clock()
            db.execute(
                "UPDATE preparations SET status='canceled',error_json=?,updated_at=?,"
                "completed_at=? WHERE id=?",
                (json.dumps(redact_payload({"reason": reason, "command_id": command_id}),
                            sort_keys=True), now, now, preparation_id),
            )
            self._event(
                db, execution_id=preparation["execution_id"],
                state_run_id=attempt["state_run_id"],
                attempt_id=preparation["attempt_id"],
                event_type="preparation_canceled",
                payload={"preparation_id": preparation_id, "reason": reason,
                         "command_id": command_id},
                idempotency_key=f"preparation:{preparation_id}:canceled:{command_id}",
            )
        return self.preparation(preparation_id)

    def quarantine_allocation(
        self, allocation_id: str, *, fence_token: str, reason: str,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            allocation = db.execute(
                "SELECT ra.*,p.attempt_id AS preparation_attempt,a.state_run_id "
                "FROM resource_allocations ra JOIN preparations p "
                "ON p.id=ra.preparation_id JOIN attempts a ON a.id=p.attempt_id "
                "WHERE ra.id=?", (allocation_id,),
            ).fetchone()
            if not allocation:
                raise LedgerError("resource allocation not found")
            if allocation["fence_token"] != fence_token:
                raise StaleAttempt("resource allocation is fenced")
            self._active_attempt(db, allocation["preparation_attempt"], fence_token)
            if allocation["status"] == "quarantined":
                return self._allocation_dict(allocation)
            if allocation["status"] != "active":
                raise LedgerError("resource allocation is not active")
            metadata = json.loads(allocation["metadata_json"])
            metadata["quarantine"] = redact_payload({"reason": reason})
            now = self.clock()
            db.execute(
                "UPDATE resource_allocations SET status='quarantined',metadata_json=?,"
                "released_at=? WHERE id=?",
                (json.dumps(metadata, sort_keys=True), now, allocation_id),
            )
            self._event(
                db, execution_id=allocation["execution_id"],
                state_run_id=allocation["state_run_id"],
                attempt_id=allocation["preparation_attempt"],
                event_type="resource_allocation_quarantined",
                payload={"allocation_id": allocation_id,
                         "resource_id": allocation["resource_id"], "reason": reason},
                idempotency_key=f"allocation:{allocation_id}:quarantined",
            )
        return self._allocation_dict(self.connection.execute(
            "SELECT * FROM resource_allocations WHERE id=?", (allocation_id,)
        ).fetchone())

    def resolve_attention(
        self, attention_id: str, *, resolution: str, detail: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if resolution not in ("resolved", "canceled"):
            raise LedgerError("invalid attention resolution")
        with self.transaction() as db:
            attention = db.execute(
                "SELECT * FROM attention_requests WHERE id=?", (attention_id,)
            ).fetchone()
            if not attention:
                raise LedgerError("attention request not found")
            if attention["status"] != "open":
                return self._attention_dict(attention)
            existing_detail = json.loads(attention["detail_json"])
            existing_detail["resolution"] = redact_payload(detail or {})
            now = self.clock()
            db.execute(
                "UPDATE attention_requests SET status=?,detail_json=?,updated_at=?,"
                "resolved_at=? WHERE id=?",
                (resolution, json.dumps(existing_detail, sort_keys=True), now, now,
                 attention_id),
            )
            execution = db.execute(
                "SELECT current_state_run_id FROM workflow_executions WHERE id=?",
                (attention["execution_id"],),
            ).fetchone()
            self._event(
                db, execution_id=attention["execution_id"],
                state_run_id=execution["current_state_run_id"],
                attempt_id=attention["attempt_id"], event_type="attention_resolved",
                payload={"attention_id": attention_id, "resolution": resolution,
                         "detail": detail or {}},
                idempotency_key=f"attention:{attention_id}:{resolution}",
            )
        return self._attention_dict(self.connection.execute(
            "SELECT * FROM attention_requests WHERE id=?", (attention_id,)
        ).fetchone())

    def begin_cleanup_plan(
        self, *, execution_id: str, attempt_id: str | None,
        plan: dict[str, Any], fence_token: str | None = None,
    ) -> dict[str, Any]:
        plan_json = json.dumps(redact_payload(plan), sort_keys=True)
        existing = self.connection.execute(
            "SELECT * FROM cleanup_plans WHERE execution_id=? AND attempt_id IS ? "
            "AND plan_json=? AND status IN ('planned','started') ORDER BY created_at LIMIT 1",
            (execution_id, attempt_id, plan_json),
        ).fetchone()
        if existing:
            return self._cleanup_plan_dict(existing)
        with self.transaction() as db:
            execution = db.execute(
                "SELECT current_state_run_id FROM workflow_executions WHERE id=?",
                (execution_id,),
            ).fetchone()
            if not execution:
                raise LedgerError("execution not found")
            if attempt_id:
                if not fence_token:
                    raise StaleAttempt("attempt cleanup requires a fence token")
                attempt = self._active_attempt(db, attempt_id, fence_token)
                if attempt["execution_id"] != execution_id:
                    raise StaleAttempt("attempt does not belong to cleanup execution")
            cleanup_id = self.id_factory()
            now = self.clock()
            db.execute(
                "INSERT INTO cleanup_plans VALUES(?,?,?,?,?,?,?,?,?)",
                (cleanup_id, execution_id, attempt_id, "planned", plan_json,
                 None, now, now, None),
            )
            self._fault("after_cleanup_planned")
            self._event(
                db, execution_id=execution_id,
                state_run_id=execution["current_state_run_id"], attempt_id=attempt_id,
                event_type="cleanup_planned",
                payload={"cleanup_id": cleanup_id, "plan": plan},
                idempotency_key=f"cleanup:{cleanup_id}:planned",
            )
        return self._cleanup_plan_dict(self.connection.execute(
            "SELECT * FROM cleanup_plans WHERE id=?", (cleanup_id,)
        ).fetchone())

    def finish_cleanup_plan(
        self, cleanup_id: str, *, status: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        if status not in ("completed", "failed", "quarantined", "skipped"):
            raise LedgerError("invalid cleanup result")
        with self.transaction() as db:
            cleanup = db.execute(
                "SELECT * FROM cleanup_plans WHERE id=?", (cleanup_id,)
            ).fetchone()
            if not cleanup:
                raise LedgerError("cleanup plan not found")
            if cleanup["status"] in ("completed", "quarantined", "skipped"):
                return self._cleanup_plan_dict(cleanup)
            now = self.clock()
            db.execute(
                "UPDATE cleanup_plans SET status=?,result_json=?,updated_at=?,"
                "completed_at=? WHERE id=?",
                (status, json.dumps(redact_payload(result), sort_keys=True), now, now,
                 cleanup_id),
            )
            execution = db.execute(
                "SELECT current_state_run_id FROM workflow_executions WHERE id=?",
                (cleanup["execution_id"],),
            ).fetchone()
            self._event(
                db, execution_id=cleanup["execution_id"],
                state_run_id=execution["current_state_run_id"],
                attempt_id=cleanup["attempt_id"], event_type=f"cleanup_{status}",
                payload={"cleanup_id": cleanup_id, "result": result},
                idempotency_key=f"cleanup:{cleanup_id}:{status}",
            )
        return self._cleanup_plan_dict(self.connection.execute(
            "SELECT * FROM cleanup_plans WHERE id=?", (cleanup_id,)
        ).fetchone())

    def _cleanup_plan_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["plan"] = json.loads(item.pop("plan_json"))
        result = item.pop("result_json")
        item["result"] = json.loads(result) if result else None
        return item

    def register_workspace(
        self, *, execution_id: str, owner_token: str, repository_path: str,
        git_common_dir: str, remote: str, base_ref: str, base_sha: str,
        branch_name: str, path: str, metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT * FROM execution_workspaces WHERE execution_id=?", (execution_id,)
        ).fetchone()
        identity = {
            "owner_token": owner_token, "repository_path": repository_path,
            "git_common_dir": git_common_dir, "remote": remote, "base_ref": base_ref,
            "base_sha": base_sha, "branch_name": branch_name, "path": path,
        }
        if existing:
            if any(str(existing[key]) != str(value) for key, value in identity.items()):
                raise LedgerError("recorded workspace provenance does not match")
            return self.workspace_for_execution(execution_id) or {}
        with self.transaction() as db:
            execution = db.execute(
                "SELECT wi.project_key,we.current_state_run_id FROM workflow_executions we "
                "JOIN work_items wi ON wi.id=we.work_item_id WHERE we.id=?",
                (execution_id,),
            ).fetchone()
            if not execution:
                raise LedgerError("execution not found")
            workspace_id = self.id_factory()
            now = self.clock()
            db.execute(
                "INSERT INTO execution_workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workspace_id, execution_id, execution["project_key"], owner_token,
                 repository_path, git_common_dir, remote, base_ref, base_sha,
                 branch_name, path, "active",
                 json.dumps(redact_payload(metadata or {}), sort_keys=True),
                 now, now, None),
            )
            self._event(
                db, execution_id=execution_id,
                state_run_id=execution["current_state_run_id"], attempt_id=None,
                event_type="workspace_registered",
                payload={"workspace_id": workspace_id, "remote": remote,
                         "base_ref": base_ref, "base_sha": base_sha,
                         "branch_name": branch_name},
                idempotency_key=f"workspace:{workspace_id}:registered",
            )
        return self.workspace_for_execution(execution_id) or {}

    def workspace_for_execution(self, execution_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM execution_workspaces WHERE execution_id=?", (execution_id,)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def set_workspace_status(
        self, execution_id: str, *, owner_token: str, status: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in ("active", "cleanup_pending", "cleaned", "quarantined"):
            raise LedgerError("invalid workspace status")
        with self.transaction() as db:
            workspace = db.execute(
                "SELECT * FROM execution_workspaces WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if not workspace or workspace["owner_token"] != owner_token:
                raise LedgerError("workspace is missing or differently owned")
            if workspace["status"] == status:
                return self.workspace_for_execution(execution_id) or {}
            now = self.clock()
            db.execute(
                "UPDATE execution_workspaces SET status=?,metadata_json=?,updated_at=?,"
                "cleaned_at=? WHERE execution_id=?",
                (status, json.dumps(redact_payload(detail or {}), sort_keys=True), now,
                 now if status == "cleaned" else workspace["cleaned_at"], execution_id),
            )
            execution = db.execute(
                "SELECT current_state_run_id FROM workflow_executions WHERE id=?",
                (execution_id,),
            ).fetchone()
            self._event(
                db, execution_id=execution_id,
                state_run_id=execution["current_state_run_id"], attempt_id=None,
                event_type=f"workspace_{status}",
                payload={"workspace_id": workspace["id"], "detail": detail or {}},
                idempotency_key=(
                    f"workspace:{workspace['id']}:{status}:"
                    f"{(detail or {}).get('cleanup_id', 'initial')}"
                ),
            )
        return self.workspace_for_execution(execution_id) or {}

    def _dispatch_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        result = item.pop("result_json")
        error = item.pop("error_json")
        item["result"] = json.loads(result) if result else None
        item["error"] = json.loads(error) if error else None
        return item

    def dispatch(self, dispatch_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM scheduler_dispatches WHERE id=?", (dispatch_id,)
        ).fetchone()
        if not row:
            raise LedgerError("scheduler dispatch not found")
        return self._dispatch_dict(row)

    def dispatch_for_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM scheduler_dispatches WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        return self._dispatch_dict(row) if row else None

    def recoverable_dispatches(
        self, *, scheduler_owner: str,
    ) -> list[dict[str, Any]]:
        return [
            self._dispatch_dict(row) for row in self.connection.execute(
                "SELECT * FROM scheduler_dispatches "
                "WHERE status IN ('preparing','dispatching','result_ready') "
                "AND scheduler_owner=? ORDER BY created_at,id",
                (scheduler_owner,),
            )
        ]

    def resumable_dispatches(self) -> list[dict[str, Any]]:
        items = []
        rows = self.connection.execute(
            "SELECT sd.*,ar.status AS attention_status,ar.detail_json "
            "FROM scheduler_dispatches sd JOIN attention_requests ar "
            "ON ar.id=sd.attention_id WHERE sd.status='attention' "
            "ORDER BY sd.created_at,sd.id"
        ).fetchall()
        for row in rows:
            detail = json.loads(row["detail_json"])
            error = json.loads(row["error_json"] or "{}")
            if (
                row["attention_status"] == "resolved"
                and detail.get("resolution", {}).get("remedy") == "retry"
                and error.get("resume_phase")
            ):
                items.append(self.dispatch(str(row["id"])))
        return items

    def resume_attention_dispatch(
        self, dispatch_id: str, *, scheduler_owner: str,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute(
                "SELECT sd.*,ar.status AS attention_status,ar.detail_json "
                "FROM scheduler_dispatches sd JOIN attention_requests ar "
                "ON ar.id=sd.attention_id WHERE sd.id=? AND sd.status='attention'",
                (dispatch_id,),
            ).fetchone()
            if not row:
                raise StaleAttempt("scheduler attention is no longer resumable")
            detail = json.loads(row["detail_json"])
            error = json.loads(row["error_json"] or "{}")
            resume_phase = error.get("resume_phase")
            if (
                row["attention_status"] != "resolved"
                or detail.get("resolution", {}).get("remedy") != "retry"
                or resume_phase not in ("claimed", "preparing", "prepared", "result_ready")
            ):
                raise LedgerError("scheduler attention was not resolved for retry")
            self._active_attempt(
                db, str(row["attempt_id"]), str(row["attempt_fence_token"])
            )
            claim_token = self.id_factory()
            now = self.clock()
            expires_at = now if resume_phase in ("claimed", "prepared") else None
            db.execute(
                "UPDATE scheduler_dispatches SET scheduler_owner=?,claim_token=?,"
                "status=?,attention_id=NULL,heartbeat_at=?,expires_at=?,updated_at=? "
                "WHERE id=?",
                (scheduler_owner, claim_token, resume_phase, now, expires_at, now,
                 dispatch_id),
            )
            execution = db.execute(
                "SELECT current_state_run_id FROM workflow_executions WHERE id=?",
                (row["execution_id"],),
            ).fetchone()
            self._event(
                db, execution_id=row["execution_id"],
                state_run_id=execution["current_state_run_id"],
                attempt_id=row["attempt_id"],
                event_type="scheduler_dispatch_resumed",
                payload={"dispatch_id": dispatch_id, "phase": resume_phase,
                         "scheduler_owner": scheduler_owner},
                idempotency_key=f"dispatch:{dispatch_id}:resumed:{claim_token}",
            )
        return self.dispatch(dispatch_id)

    def claim_dispatch(
        self, *, scheduler_owner: str, claim_ttl_seconds: int,
        limits: dict[str, Any],
    ) -> dict[str, Any]:
        now = self.clock()
        now_value = parse_timestamp(now)
        expires_at = (now_value + timedelta(seconds=claim_ttl_seconds)).isoformat()
        host_limit = int(limits["host"])
        project_limits = dict(limits.get("projects", {}))
        runner_limits = dict(limits.get("runners", {}))
        with self.transaction() as db:
            active_rows = db.execute(
                "SELECT * FROM scheduler_dispatches WHERE status IN "
                "('claimed','preparing','prepared','dispatching','result_ready')"
            ).fetchall()
            active = []
            for row in active_rows:
                if (
                    row["status"] in ("claimed", "prepared")
                    and row["expires_at"]
                    and parse_timestamp(str(row["expires_at"])) <= now_value
                ):
                    continue
                active.append(row)
            candidates = db.execute(
                "SELECT a.id AS attempt_id,a.fence_token,a.started_at,"
                "sr.execution_id,we.current_state_run_id,wi.project_key,"
                "ab.resolved_json,ab.workflow_digest "
                "FROM attempts a JOIN state_runs sr ON sr.id=a.state_run_id "
                "JOIN workflow_executions we ON we.id=sr.execution_id "
                "JOIN work_items wi ON wi.id=we.work_item_id "
                "JOIN attempt_bindings ab ON ab.attempt_id=a.id "
                "WHERE a.status='active' AND sr.status='active' "
                "AND we.status='running' AND NOT EXISTS ("
                "SELECT 1 FROM attention_requests ar "
                "WHERE ar.attempt_id=a.id AND ar.status='open') "
                "ORDER BY a.started_at,a.id"
            ).fetchall()
            blocked: list[dict[str, str]] = []
            for candidate in candidates:
                existing = db.execute(
                    "SELECT * FROM scheduler_dispatches WHERE attempt_id=?",
                    (candidate["attempt_id"],),
                ).fetchone()
                if existing:
                    status = str(existing["status"])
                    if status == "waiting":
                        available = existing["available_at"]
                        if available and parse_timestamp(str(available)) > now_value:
                            continue
                    elif status in ("claimed", "prepared"):
                        expiry = existing["expires_at"]
                        if not expiry or parse_timestamp(str(expiry)) > now_value:
                            continue
                    elif status not in ("superseded",):
                        continue
                resolved = json.loads(candidate["resolved_json"])
                configured_runner = resolved.get("runner")
                runner_key = (
                    configured_runner
                    if isinstance(configured_runner, str) else ""
                )
                project_key = str(candidate["project_key"])
                if len(active) >= host_limit:
                    blocked.append({"attempt_id": str(candidate["attempt_id"]),
                                    "limit": "host"})
                    continue
                project_count = sum(
                    row["project_key"] == project_key for row in active
                )
                if project_count >= int(project_limits.get(project_key, host_limit)):
                    blocked.append({"attempt_id": str(candidate["attempt_id"]),
                                    "limit": f"project:{project_key}"})
                    continue
                if runner_key:
                    runner_count = sum(
                        row["runner_key"] == runner_key for row in active
                    )
                    if runner_count >= int(runner_limits.get(runner_key, host_limit)):
                        blocked.append({"attempt_id": str(candidate["attempt_id"]),
                                        "limit": f"runner:{runner_key}"})
                        continue
                claim_token = self.id_factory()
                if existing:
                    dispatch_id = str(existing["id"])
                    db.execute(
                        "UPDATE scheduler_dispatches SET project_key=?,runner_key=?,"
                        "scheduler_owner=?,claim_token=?,attempt_fence_token=?,"
                        "status='claimed',available_at=NULL,heartbeat_at=?,expires_at=?,"
                        "error_json=NULL,attention_id=NULL,updated_at=?,completed_at=NULL "
                        "WHERE id=?",
                        (project_key, runner_key, scheduler_owner, claim_token,
                         candidate["fence_token"], now, expires_at, now, dispatch_id),
                    )
                else:
                    dispatch_id = self.id_factory()
                    db.execute(
                        "INSERT INTO scheduler_dispatches "
                        "(id,attempt_id,execution_id,project_key,runner_key,"
                        "scheduler_owner,claim_token,attempt_fence_token,status,"
                        "available_at,heartbeat_at,expires_at,preparation_id,"
                        "preparation_digest,result_json,error_json,attention_id,"
                        "created_at,updated_at,completed_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (dispatch_id, candidate["attempt_id"], candidate["execution_id"],
                         project_key, runner_key, scheduler_owner, claim_token,
                         candidate["fence_token"], "claimed", None, now, expires_at,
                         None, None, None, None, None, now, now, None),
                    )
                self._event(
                    db, execution_id=candidate["execution_id"],
                    state_run_id=candidate["current_state_run_id"],
                    attempt_id=candidate["attempt_id"],
                    event_type="scheduler_dispatch_claimed",
                    payload={"dispatch_id": dispatch_id,
                             "scheduler_owner": scheduler_owner,
                             "project_key": project_key,
                             "runner_key": runner_key,
                             "expires_at": expires_at},
                    idempotency_key=f"dispatch:{dispatch_id}:claimed:{claim_token}",
                )
                return {"disposition": "claimed",
                        "dispatch": self._dispatch_dict(db.execute(
                            "SELECT * FROM scheduler_dispatches WHERE id=?",
                            (dispatch_id,),
                        ).fetchone())}
            return {
                "disposition": "capacity" if blocked else "idle",
                "blocked": blocked,
            }

    def _require_dispatch(
        self, db: sqlite3.Connection, dispatch_id: str, claim_token: str,
        statuses: tuple[str, ...], *, active_attempt: bool = True,
    ) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM scheduler_dispatches WHERE id=?", (dispatch_id,)
        ).fetchone()
        if (
            not row or row["claim_token"] != claim_token
            or row["status"] not in statuses
        ):
            raise StaleAttempt("scheduler dispatch is missing, advanced, or fenced")
        if active_attempt:
            self._active_attempt(
                db, str(row["attempt_id"]), str(row["attempt_fence_token"])
            )
        return row

    def assert_dispatch(
        self, dispatch_id: str, *, claim_token: str,
        statuses: tuple[str, ...], active_attempt: bool = True,
    ) -> dict[str, Any]:
        return self._dispatch_dict(self._require_dispatch(
            self.connection, dispatch_id, claim_token, statuses,
            active_attempt=active_attempt,
        ))

    def _dispatch_phase(
        self, dispatch_id: str, *, claim_token: str,
        from_statuses: tuple[str, ...], status: str,
        values: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        active_attempt: bool = True,
    ) -> dict[str, Any]:
        values = dict(values or {})
        now = self.clock()
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM scheduler_dispatches WHERE id=?", (dispatch_id,)
            ).fetchone()
            if (
                existing and existing["claim_token"] == claim_token
                and existing["status"] == status
            ):
                return self._dispatch_dict(existing)
            row = self._require_dispatch(
                db, dispatch_id, claim_token, from_statuses,
                active_attempt=active_attempt,
            )
            assignments = ["status=?", "heartbeat_at=?", "updated_at=?"]
            parameters: list[Any] = [status, now, now]
            for key, value in values.items():
                assignments.append(f"{key}=?")
                parameters.append(value)
            parameters.append(dispatch_id)
            db.execute(
                f"UPDATE scheduler_dispatches SET {','.join(assignments)} WHERE id=?",
                tuple(parameters),
            )
            execution = db.execute(
                "SELECT current_state_run_id FROM workflow_executions WHERE id=?",
                (row["execution_id"],),
            ).fetchone()
            self._event(
                db, execution_id=row["execution_id"],
                state_run_id=execution["current_state_run_id"],
                attempt_id=row["attempt_id"],
                event_type=f"scheduler_dispatch_{status}",
                payload={"dispatch_id": dispatch_id, **(payload or {})},
                idempotency_key=f"dispatch:{dispatch_id}:{status}:{claim_token}",
            )
        return self.dispatch(dispatch_id)

    def heartbeat_dispatch(
        self, dispatch_id: str, *, claim_token: str,
        claim_ttl_seconds: int, command_id: str,
    ) -> dict[str, Any]:
        prior = self.event_for_command(
            f"dispatch:{dispatch_id}:heartbeat:{command_id}"
        )
        if prior:
            return self.dispatch(dispatch_id)
        now = self.clock()
        expires_at = (
            parse_timestamp(now) + timedelta(seconds=claim_ttl_seconds)
        ).isoformat()
        with self.transaction() as db:
            row = self._require_dispatch(
                db, dispatch_id, claim_token,
                ("claimed", "preparing", "prepared", "dispatching", "result_ready"),
            )
            db.execute(
                "UPDATE scheduler_dispatches SET heartbeat_at=?,expires_at=?,updated_at=? "
                "WHERE id=?", (now, expires_at, now, dispatch_id),
            )
            execution = db.execute(
                "SELECT current_state_run_id FROM workflow_executions WHERE id=?",
                (row["execution_id"],),
            ).fetchone()
            self._event(
                db, execution_id=row["execution_id"],
                state_run_id=execution["current_state_run_id"],
                attempt_id=row["attempt_id"],
                event_type="scheduler_dispatch_heartbeat",
                payload={"dispatch_id": dispatch_id, "expires_at": expires_at},
                idempotency_key=f"dispatch:{dispatch_id}:heartbeat:{command_id}",
            )
        return self.dispatch(dispatch_id)

    def mark_dispatch_preparing(
        self, dispatch_id: str, *, claim_token: str,
    ) -> dict[str, Any]:
        return self._dispatch_phase(
            dispatch_id, claim_token=claim_token, from_statuses=("claimed",),
            status="preparing", values={"expires_at": None},
        )

    def mark_dispatch_prepared(
        self, dispatch_id: str, *, claim_token: str, preparation_id: str,
        preparation_digest: str, claim_ttl_seconds: int,
    ) -> dict[str, Any]:
        expires_at = (
            parse_timestamp(self.clock()) + timedelta(seconds=claim_ttl_seconds)
        ).isoformat()
        return self._dispatch_phase(
            dispatch_id, claim_token=claim_token, from_statuses=("preparing",),
            status="prepared",
            values={"preparation_id": preparation_id,
                    "preparation_digest": preparation_digest,
                    "expires_at": expires_at},
            payload={"preparation_id": preparation_id,
                     "preparation_digest": preparation_digest},
        )

    def mark_dispatching(
        self, dispatch_id: str, *, claim_token: str,
    ) -> dict[str, Any]:
        return self._dispatch_phase(
            dispatch_id, claim_token=claim_token, from_statuses=("prepared",),
            status="dispatching", values={"expires_at": None},
        )

    def record_dispatch_result(
        self, dispatch_id: str, *, claim_token: str, result: dict[str, Any],
    ) -> dict[str, Any]:
        redacted = json.dumps(redact_payload(result), sort_keys=True)
        return self._dispatch_phase(
            dispatch_id, claim_token=claim_token, from_statuses=("dispatching",),
            status="result_ready", values={"result_json": redacted},
            payload={"result": result},
        )

    def defer_dispatch(
        self, dispatch_id: str, *, claim_token: str, available_at: str,
        error: dict[str, Any],
    ) -> dict[str, Any]:
        parse_timestamp(available_at)
        return self._dispatch_phase(
            dispatch_id, claim_token=claim_token, from_statuses=("preparing",),
            status="waiting",
            values={"available_at": available_at, "expires_at": None,
                    "error_json": json.dumps(redact_payload(error), sort_keys=True)},
            payload={"available_at": available_at, "error": error},
        )

    def mark_dispatch_attention(
        self, dispatch_id: str, *, claim_token: str, attention_id: str,
        error: dict[str, Any],
    ) -> dict[str, Any]:
        next_token = self.id_factory()
        return self._dispatch_phase(
            dispatch_id, claim_token=claim_token,
            from_statuses=("claimed", "preparing", "prepared", "dispatching",
                           "result_ready", "waiting"),
            status="attention", active_attempt=False,
            values={"attention_id": attention_id, "expires_at": None,
                    "claim_token": next_token,
                    "error_json": json.dumps(redact_payload(error), sort_keys=True)},
            payload={"attention_id": attention_id, "error": error},
        )

    def complete_dispatch(
        self, dispatch_id: str, *, claim_token: str,
    ) -> dict[str, Any]:
        now = self.clock()
        return self._dispatch_phase(
            dispatch_id, claim_token=claim_token,
            from_statuses=("result_ready",), status="completed",
            active_attempt=False,
            values={"expires_at": None, "completed_at": now},
        )

    def supersede_dispatch(
        self, dispatch_id: str, *, claim_token: str, reason: str,
    ) -> dict[str, Any]:
        now = self.clock()
        return self._dispatch_phase(
            dispatch_id, claim_token=claim_token,
            from_statuses=("claimed", "preparing", "prepared", "dispatching",
                           "result_ready", "waiting", "attention"),
            status="superseded", active_attempt=False,
            values={"expires_at": None, "completed_at": now,
                    "error_json": json.dumps({"message": reason}, sort_keys=True)},
            payload={"reason": reason},
        )

    def takeover_result_dispatch(
        self, dispatch_id: str, *, scheduler_owner: str,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM scheduler_dispatches WHERE id=? AND status='result_ready'",
                (dispatch_id,),
            ).fetchone()
            if not row:
                raise StaleAttempt("stored scheduler result is no longer recoverable")
            self._active_attempt(
                db, str(row["attempt_id"]), str(row["attempt_fence_token"])
            )
            claim_token = self.id_factory()
            now = self.clock()
            db.execute(
                "UPDATE scheduler_dispatches SET scheduler_owner=?,claim_token=?,"
                "heartbeat_at=?,updated_at=? WHERE id=?",
                (scheduler_owner, claim_token, now, now, dispatch_id),
            )
            execution = db.execute(
                "SELECT current_state_run_id FROM workflow_executions WHERE id=?",
                (row["execution_id"],),
            ).fetchone()
            self._event(
                db, execution_id=row["execution_id"],
                state_run_id=execution["current_state_run_id"],
                attempt_id=row["attempt_id"],
                event_type="scheduler_dispatch_recovered",
                payload={"dispatch_id": dispatch_id,
                         "scheduler_owner": scheduler_owner,
                         "phase": "result_ready"},
                idempotency_key=f"dispatch:{dispatch_id}:recovered:{claim_token}",
            )
        return self.dispatch(dispatch_id)

    def takeover_preparing_dispatch(
        self, dispatch_id: str, *, scheduler_owner: str,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM scheduler_dispatches WHERE id=? AND status='preparing'",
                (dispatch_id,),
            ).fetchone()
            if not row:
                raise StaleAttempt("scheduler preparation is no longer recoverable")
            self._active_attempt(
                db, str(row["attempt_id"]), str(row["attempt_fence_token"])
            )
            claim_token = self.id_factory()
            now = self.clock()
            db.execute(
                "UPDATE scheduler_dispatches SET scheduler_owner=?,claim_token=?,"
                "heartbeat_at=?,updated_at=? WHERE id=?",
                (scheduler_owner, claim_token, now, now, dispatch_id),
            )
            execution = db.execute(
                "SELECT current_state_run_id FROM workflow_executions WHERE id=?",
                (row["execution_id"],),
            ).fetchone()
            self._event(
                db, execution_id=row["execution_id"],
                state_run_id=execution["current_state_run_id"],
                attempt_id=row["attempt_id"],
                event_type="scheduler_dispatch_recovered",
                payload={"dispatch_id": dispatch_id,
                         "scheduler_owner": scheduler_owner,
                         "phase": "preparing"},
                idempotency_key=f"dispatch:{dispatch_id}:recovered:{claim_token}",
            )
        return self.dispatch(dispatch_id)

    def release_allocation(
        self, allocation_id: str, *, fence_token: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            allocation = db.execute(
                "SELECT ra.*,p.attempt_id AS preparation_attempt,a.state_run_id "
                "FROM resource_allocations ra JOIN preparations p ON p.id=ra.preparation_id "
                "JOIN attempts a ON a.id=p.attempt_id WHERE ra.id=?", (allocation_id,),
            ).fetchone()
            if not allocation:
                raise LedgerError("resource allocation not found")
            if allocation["fence_token"] != fence_token:
                raise StaleAttempt("resource allocation is fenced")
            if allocation["status"] == "released":
                return self._allocation_dict(allocation)
            self._active_attempt(db, allocation["preparation_attempt"], fence_token)
            now = self.clock()
            metadata = json.loads(allocation["metadata_json"])
            metadata["release"] = redact_payload(result or {})
            db.execute(
                "UPDATE resource_allocations SET status='released',metadata_json=?,"
                "released_at=? WHERE id=?",
                (json.dumps(metadata, sort_keys=True), now, allocation_id),
            )
            self._event(
                db, execution_id=allocation["execution_id"],
                state_run_id=allocation["state_run_id"],
                attempt_id=allocation["preparation_attempt"],
                event_type="resource_allocation_released",
                payload={"allocation_id": allocation_id,
                         "resource_id": allocation["resource_id"],
                         "provider": allocation["provider"], "result": result or {}},
                idempotency_key=f"allocation:{allocation_id}:released",
            )
        return self._allocation_dict(self.connection.execute(
            "SELECT * FROM resource_allocations WHERE id=?", (allocation_id,)
        ).fetchone())

    def reap_expired_leases(self, *, resource_id: str | None = None) -> list[str]:
        now = self.clock()
        query = (
            "SELECT rl.*,sr.execution_id,sr.id AS state_run_id "
            "FROM resource_leases rl JOIN attempts a ON a.id=rl.attempt_id "
            "JOIN state_runs sr ON sr.id=a.state_run_id WHERE rl.status='active'"
        )
        parameters: tuple[Any, ...] = ()
        if resource_id is not None:
            query += " AND rl.resource_id=?"
            parameters = (resource_id,)
        candidates = [
            row for row in self.connection.execute(query, parameters).fetchall()
            if parse_timestamp(str(row["expires_at"])) <= parse_timestamp(now)
        ]
        expired_ids = []
        for candidate in candidates:
            with self.transaction() as db:
                lease = db.execute(
                    "SELECT status,expires_at FROM resource_leases WHERE id=?",
                    (candidate["id"],),
                ).fetchone()
                if (
                    not lease or lease["status"] != "active"
                    or parse_timestamp(str(lease["expires_at"])) > parse_timestamp(now)
                ):
                    continue
                db.execute(
                    "UPDATE resource_leases SET status='expired',released_at=? WHERE id=?",
                    (now, candidate["id"]),
                )
                self._event(
                    db, execution_id=candidate["execution_id"],
                    state_run_id=candidate["state_run_id"],
                    attempt_id=candidate["attempt_id"], event_type="resource_lease_expired",
                    payload={"lease_id": candidate["id"],
                             "resource_id": candidate["resource_id"]},
                    idempotency_key=(
                        f"lease:{candidate['id']}:expired:{candidate['expires_at']}"
                    ),
                )
                expired_ids.append(str(candidate["id"]))
        return expired_ids

    def heartbeat_resource(
        self, lease_id: str, *, fence_token: str, expires_at: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        prior = self.event_for_command(idempotency_key)
        if prior:
            return prior
        self.reap_expired_leases()
        now = self.clock()
        if parse_timestamp(expires_at) <= parse_timestamp(now):
            raise LedgerError("resource lease expiry must be in the future")
        with self.transaction() as db:
            lease = db.execute(
                "SELECT rl.*,a.status AS attempt_status,sr.execution_id,sr.id AS state_run_id "
                "FROM resource_leases rl JOIN attempts a ON a.id=rl.attempt_id "
                "JOIN state_runs sr ON sr.id=a.state_run_id WHERE rl.id=?", (lease_id,),
            ).fetchone()
            if (
                not lease or lease["status"] != "active"
                or lease["attempt_status"] != "active"
                or lease["fence_token"] != fence_token
            ):
                raise StaleAttempt("resource lease is expired, released, or fenced")
            db.execute(
                "UPDATE resource_leases SET heartbeat_at=?,expires_at=? WHERE id=?",
                (now, expires_at, lease_id),
            )
            seq = self._event(
                db, execution_id=lease["execution_id"], state_run_id=lease["state_run_id"],
                attempt_id=lease["attempt_id"], event_type="resource_lease_heartbeat",
                payload={"lease_id": lease_id, "resource_id": lease["resource_id"],
                         "expires_at": expires_at}, idempotency_key=idempotency_key,
            )
        return {"event_seq": seq, "lease_id": lease_id, "expires_at": expires_at}

    def release_resource(
        self, lease_id: str, *, fence_token: str, idempotency_key: str,
    ) -> dict[str, Any]:
        prior = self.event_for_command(idempotency_key)
        if prior:
            return prior
        self.reap_expired_leases()
        with self.transaction() as db:
            lease = db.execute(
                "SELECT rl.*,sr.execution_id,sr.id AS state_run_id "
                "FROM resource_leases rl JOIN attempts a ON a.id=rl.attempt_id "
                "JOIN state_runs sr ON sr.id=a.state_run_id WHERE rl.id=?", (lease_id,),
            ).fetchone()
            if (
                not lease or lease["status"] != "active"
                or lease["fence_token"] != fence_token
            ):
                raise StaleAttempt("resource lease is expired, released, or fenced")
            now = self.clock()
            db.execute(
                "UPDATE resource_leases SET status='released',released_at=? WHERE id=?",
                (now, lease_id),
            )
            seq = self._event(
                db, execution_id=lease["execution_id"], state_run_id=lease["state_run_id"],
                attempt_id=lease["attempt_id"], event_type="resource_lease_released",
                payload={"lease_id": lease_id, "resource_id": lease["resource_id"]},
                idempotency_key=idempotency_key,
            )
        return {"event_seq": seq, "lease_id": lease_id}

    def current(self, execution_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT we.*,wi.project_key,wi.identifier AS work_item_identifier "
            "FROM workflow_executions we JOIN work_items wi ON wi.id=we.work_item_id "
            "WHERE we.id=?", (execution_id,)
        ).fetchone()
        if not row:
            raise LedgerError("execution not found")
        result = dict(row)
        workflow = self.connection.execute(
            "SELECT ws.digest,ws.source_format FROM execution_workflow_snapshots ews "
            "JOIN workflow_snapshots ws ON ws.digest=ews.workflow_digest "
            "WHERE ews.execution_id=?", (execution_id,),
        ).fetchone()
        result["workflow_digest"] = workflow["digest"] if workflow else None
        result["workflow_source_format"] = workflow["source_format"] if workflow else None
        attempt = self.connection.execute(
            "SELECT * FROM attempts WHERE state_run_id=? AND status='active'",
            (row["current_state_run_id"],),
        ).fetchone()
        result["attempt"] = dict(attempt) if attempt else None
        if result["attempt"]:
            binding = self.connection.execute(
                "SELECT state_id,resolved_json,workflow_digest FROM attempt_bindings "
                "WHERE attempt_id=?", (result["attempt"]["id"],),
            ).fetchone()
            result["attempt"]["binding"] = (
                {
                    "state_id": binding["state_id"],
                    "workflow_digest": binding["workflow_digest"],
                    "resolved": json.loads(binding["resolved_json"]),
                }
                if binding else None
            )
        return result

    def event_for_command(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM events WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def decision_for_command(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT td.*,e.payload_json "
            "FROM transition_decisions td JOIN events e ON e.seq=td.event_seq "
            "WHERE e.idempotency_key=?", (idempotency_key,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        payload = json.loads(result.pop("payload_json"))
        result["attempt_id"] = payload.get("attempt_id")
        result["fence_token"] = None
        if result["attempt_id"]:
            attempt = self.connection.execute(
                "SELECT fence_token FROM attempts WHERE id=?", (result["attempt_id"],)
            ).fetchone()
            result["fence_token"] = attempt["fence_token"] if attempt else None
        return result

    def current_state_feedback(self, execution_id: str) -> list[dict[str, Any]]:
        execution = self.connection.execute(
            "SELECT current_state_id,current_state_run_id FROM workflow_executions WHERE id=?",
            (execution_id,),
        ).fetchone()
        if not execution:
            return []
        rows = self.connection.execute(
            "SELECT * FROM feedback WHERE execution_id=? AND target_id=? ORDER BY created_at,id",
            (execution_id, execution["current_state_run_id"]),
        ).fetchall()
        return [
            {"id": str(row["id"]), "body": json.loads(row["body_json"])} for row in rows
        ]

    def current_review_feedback(self, execution_id: str) -> list[dict[str, Any]]:
        return self.current_state_feedback(execution_id)

    def defer_transition(
        self, *, execution_id: str, edge_id: str, from_state: str, to_state: str,
        actor: str, signal: str, feedback: list[dict[str, Any]],
        idempotency_key: str, observed_linear_status: str,
        source_event_id: str | None = None,
        requires_feedback: bool = False,
        feedback_kind: str | None = None,
    ) -> dict[str, Any]:
        prior = self.event_for_command(idempotency_key)
        if prior:
            return prior
        with self.transaction() as db:
            execution = db.execute(
                "SELECT * FROM workflow_executions WHERE id=?", (execution_id,)
            ).fetchone()
            if not execution or execution["status"] != "running":
                raise LedgerError("execution is not running")
            if execution["current_state_id"] != from_state:
                raise LedgerError("transition source is stale")
            if db.execute(
                "SELECT 1 FROM transition_requests WHERE execution_id=? AND status='pending'",
                (execution_id,),
            ).fetchone():
                raise LedgerError("execution already has a pending transition")
            stored = db.execute(
                "SELECT * FROM feedback WHERE execution_id=? AND target_id=?",
                (execution_id, execution["current_state_run_id"]),
            ).fetchall()
            combined = [json.loads(row["body_json"]) for row in stored] + list(feedback)
            if requires_feedback and not combined:
                raise LedgerError("transition requires durable feedback")
            if combined:
                validate_feedback(combined, expected_kind=feedback_kind)
            request_id = self.id_factory()
            feedback_ids = [str(row["id"]) for row in stored]
            for item in feedback:
                feedback_id = self.id_factory()
                db.execute(
                    "INSERT INTO feedback VALUES(?,?,?,?,?,?)",
                    (feedback_id, execution_id, item["source"], request_id,
                     json.dumps(redact_payload(item), sort_keys=True), self.clock()),
                )
                feedback_ids.append(feedback_id)
            payload = {
                "actor": actor, "from": from_state, "to": to_state,
                "signal": signal, "observed_status": observed_linear_status,
                "disposition": "pending", "source_event_id": source_event_id,
                "feedback": combined, "feedback_ids": feedback_ids,
                "request_id": request_id,
            }
            seq = self._event(
                db, execution_id=execution_id,
                state_run_id=execution["current_state_run_id"], attempt_id=None,
                event_type="transition_requested", payload=payload,
                idempotency_key=idempotency_key, destinations=("logfire",),
            )
            db.execute(
                "INSERT INTO transition_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, execution_id, edge_id, from_state, to_state, actor, signal,
                 observed_linear_status, source_event_id, "pending", seq, self.clock(), None),
            )
            db.execute(
                "UPDATE workflow_executions SET observed_linear_status=? WHERE id=?",
                (observed_linear_status, execution_id),
            )
        return {
            "request_id": request_id, "event_seq": seq, "event_type": "transition_requested",
            "to_state": to_state, "disposition": "pending", "feedback_ids": feedback_ids,
        }

    def pending_transition(self, execution_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM transition_requests WHERE execution_id=? AND status='pending'",
            (execution_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        execution = self.connection.execute(
            "SELECT current_state_run_id FROM workflow_executions WHERE id=?", (execution_id,)
        ).fetchone()
        targets = [str(row["id"])]
        if execution:
            targets.append(str(execution["current_state_run_id"]))
        placeholders = ",".join("?" for _ in targets)
        feedback_rows = self.connection.execute(
            f"SELECT id,body_json FROM feedback WHERE execution_id=? "
            f"AND target_id IN ({placeholders}) ORDER BY created_at,id",
            (execution_id, *targets),
        ).fetchall()
        result["feedback_ids"] = [str(item["id"]) for item in feedback_rows]
        result["feedback"] = [json.loads(item["body_json"]) for item in feedback_rows]
        return result

    def record_linear_observation(
        self, execution_id: str, *, observed_status: str, actor: str,
        disposition: str, reason: str, idempotency_key: str,
        source_event_id: str | None = None,
        feedback: list[dict[str, Any]] | None = None,
        feedback_allowed: bool = False,
    ) -> dict[str, Any]:
        prior = self.event_for_command(idempotency_key)
        if prior:
            return prior
        with self.transaction() as db:
            execution = db.execute(
                "SELECT current_state_id,desired_linear_status,current_state_run_id "
                "FROM workflow_executions WHERE id=?", (execution_id,)
            ).fetchone()
            if not execution:
                raise LedgerError("execution not found")
            review_feedback = list(feedback or [])
            if review_feedback:
                if not feedback_allowed:
                    raise LedgerError("feedback is not allowed in the current state")
                validate_feedback(review_feedback)
            canceled_requests = []
            if observed_status == execution["desired_linear_status"]:
                canceled_requests = [
                    str(row["id"]) for row in db.execute(
                        "SELECT id FROM transition_requests WHERE execution_id=? "
                        "AND status='pending'", (execution_id,),
                    )
                ]
                db.execute(
                    "UPDATE transition_requests SET status='canceled',consumed_at=? "
                    "WHERE execution_id=? AND status='pending'",
                    (self.clock(), execution_id),
                )
            feedback_ids = []
            for item in review_feedback:
                feedback_id = self.id_factory()
                db.execute(
                    "INSERT INTO feedback VALUES(?,?,?,?,?,?)",
                    (feedback_id, execution_id, item["source"],
                     execution["current_state_run_id"],
                     json.dumps(redact_payload(item), sort_keys=True), self.clock()),
                )
                feedback_ids.append(feedback_id)
            payload = {
                "actor": actor,
                "observed_status": observed_status,
                "desired_status": execution["desired_linear_status"],
                "current_state": execution["current_state_id"],
                "disposition": disposition,
                "reason": reason,
                "source_event_id": source_event_id,
                "feedback": review_feedback,
                "feedback_ids": feedback_ids,
                "canceled_request_ids": canceled_requests,
            }
            destinations = (
                ("logfire",)
                if disposition == "no_change" and not canceled_requests
                else ("linear", "logfire")
            )
            seq = self._event(
                db, execution_id=execution_id,
                state_run_id=execution["current_state_run_id"], attempt_id=None,
                event_type="linear_status_observed", payload=payload,
                idempotency_key=idempotency_key, destinations=destinations,
            )
            db.execute(
                "UPDATE workflow_executions SET observed_linear_status=? WHERE id=?",
                (observed_status, execution_id),
            )
        return {"event_seq": seq, "event_type": "linear_status_observed", "payload": payload}

    def feedback_for_attempt(self, attempt_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM feedback WHERE target_id=? ORDER BY created_at,id", (attempt_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["body"] = json.loads(item.pop("body_json"))
            result.append(item)
        return result

    def workflow_snapshot(self, execution_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT ws.* FROM execution_workflow_snapshots ews "
            "JOIN workflow_snapshots ws ON ws.digest=ews.workflow_digest "
            "WHERE ews.execution_id=?", (execution_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["normalized"] = json.loads(result.pop("normalized_json"))
        return result

    def run_history(self, execution_id: str) -> dict[str, Any]:
        execution = self.current(execution_id)
        state_runs = [
            dict(row) for row in self.connection.execute(
                "SELECT * FROM state_runs WHERE execution_id=? ORDER BY ordinal", (execution_id,)
            )
        ]
        attempts = [
            dict(row) for row in self.connection.execute(
                "SELECT a.* FROM attempts a JOIN state_runs sr ON sr.id=a.state_run_id "
                "WHERE sr.execution_id=? ORDER BY sr.ordinal,a.started_at,a.id", (execution_id,)
            )
        ]
        bindings = {}
        for row in self.connection.execute(
            "SELECT ab.* FROM attempt_bindings ab JOIN attempts a ON a.id=ab.attempt_id "
            "JOIN state_runs sr ON sr.id=a.state_run_id WHERE sr.execution_id=?",
            (execution_id,),
        ):
            item = dict(row)
            item["resolved"] = json.loads(item.pop("resolved_json"))
            bindings[str(item["attempt_id"])] = item
        for attempt in attempts:
            attempt["binding"] = bindings.get(str(attempt["id"]))
        events = []
        for row in self.connection.execute(
            "SELECT * FROM events WHERE execution_id=? ORDER BY seq", (execution_id,)
        ):
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            events.append(item)
        artifacts = []
        for row in self.connection.execute(
            "SELECT * FROM artifacts WHERE execution_id=? ORDER BY created_at,id", (execution_id,)
        ):
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            artifacts.append(item)
        feedback = []
        for row in self.connection.execute(
            "SELECT * FROM feedback WHERE execution_id=? ORDER BY created_at,id", (execution_id,)
        ):
            item = dict(row)
            item["body"] = json.loads(item.pop("body_json"))
            feedback.append(item)
        transition_requests = [
            dict(row) for row in self.connection.execute(
                "SELECT * FROM transition_requests WHERE execution_id=? "
                "ORDER BY requested_at,id", (execution_id,)
            )
        ]
        return {
            "project_key": execution["project_key"],
            "work_item_identifier": execution["work_item_identifier"],
            "intent": json.loads(execution["intent_snapshot_json"]),
            "policy": {
                "workflow_name": execution["workflow_name"],
                "workflow_version": execution["workflow_version"],
                "workflow_digest": execution["workflow_digest"],
            },
            "execution": execution,
            "state_runs": state_runs,
            "attempts": attempts,
            "events": events,
            "artifacts": artifacts,
            "feedback": feedback,
            "transition_requests": transition_requests,
            "workflow_snapshot": self.workflow_snapshot(execution_id),
        }

    def overview(self) -> dict[str, Any]:
        identity = self.connection.execute(
            "SELECT factory_id,created_at FROM factory_identity WHERE singleton=1"
        ).fetchone()
        projects = [
            dict(row) for row in self.connection.execute(
                "SELECT p.project_key,p.display_name,p.tracker_kind,p.tracker_project_slug,"
                "COUNT(we.id) AS run_count,"
                "SUM(CASE WHEN we.status='running' THEN 1 ELSE 0 END) AS running_count "
                "FROM projects p LEFT JOIN work_items wi ON wi.project_key=p.project_key "
                "LEFT JOIN workflow_executions we ON we.work_item_id=wi.id "
                "GROUP BY p.project_key ORDER BY p.project_key"
            )
        ]
        states = {
            str(row["current_state_id"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT current_state_id,COUNT(*) AS count FROM workflow_executions "
                "GROUP BY current_state_id ORDER BY current_state_id"
            )
        }
        outbox = [
            dict(row) for row in self.connection.execute(
                "SELECT destination,status,COUNT(*) AS count,"
                "MIN(created_at) AS oldest_at FROM outbox "
                "GROUP BY destination,status ORDER BY destination,status"
            )
        ]
        active_leases = int(self.connection.execute(
            "SELECT COUNT(*) FROM resource_leases WHERE status='active'"
        ).fetchone()[0])
        active_allocations = int(self.connection.execute(
            "SELECT COUNT(*) FROM resource_allocations "
            "WHERE status IN ('active','release_pending')"
        ).fetchone()[0])
        open_attention = int(self.connection.execute(
            "SELECT COUNT(*) FROM attention_requests WHERE status='open'"
        ).fetchone()[0])
        return {
            "factory": dict(identity) if identity else None,
            "ledger_schema_version": SCHEMA_VERSION,
            "projects": projects,
            "runs_by_state": states,
            "active_resource_leases": active_leases,
            "active_resource_allocations": active_allocations,
            "open_attention_requests": open_attention,
            "projection_outbox": outbox,
            "generated_at": self.clock(),
        }

    def list_runs(
        self, *, project_key: str | None = None, status: str | None = None,
        state: str | None = None, limit: int = 25,
        before_created_at: str | None = None, before_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if project_key:
            clauses.append("wi.project_key=?")
            parameters.append(project_key)
        if status:
            clauses.append("we.status=?")
            parameters.append(status)
        if state:
            clauses.append("we.current_state_id=?")
            parameters.append(state)
        if before_created_at and before_id:
            clauses.append("(we.created_at<? OR (we.created_at=? AND we.id<?))")
            parameters.extend((before_created_at, before_created_at, before_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit)
        rows = self.connection.execute(
            "SELECT we.*,wi.project_key,wi.identifier AS work_item_identifier,"
            "(SELECT MAX(seq) FROM events e WHERE e.execution_id=we.id) AS latest_event_seq,"
            "(SELECT COUNT(*) FROM transition_requests tr WHERE tr.execution_id=we.id "
            "AND tr.status='pending') AS pending_transition_count "
            "FROM workflow_executions we JOIN work_items wi ON wi.id=we.work_item_id" +
            where + " ORDER BY we.created_at DESC,we.id DESC LIMIT ?", tuple(parameters)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["intent"] = json.loads(item.pop("intent_snapshot_json"))
            result.append(item)
        return result

    def run_snapshot(self, execution_id: str) -> dict[str, Any]:
        current = self.current(execution_id)
        current["intent"] = json.loads(current.pop("intent_snapshot_json"))
        current["pending_transition"] = self.pending_transition(execution_id)
        workflow = self.workflow_snapshot(execution_id)
        current["workflow"] = (
            {
                "digest": workflow["digest"],
                "name": workflow["workflow_name"],
                "schema_version": workflow["schema_version"],
                "source_format": workflow["source_format"],
            }
            if workflow else None
        )
        current["latest_event_seq"] = int(self.connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM events WHERE execution_id=?", (execution_id,)
        ).fetchone()[0])
        current["pending_projection_count"] = int(self.connection.execute(
            "SELECT COUNT(*) FROM outbox o JOIN events e ON e.seq=o.event_seq "
            "WHERE e.execution_id=? AND o.status IN ('pending','failed')", (execution_id,)
        ).fetchone()[0])
        current["active_resources"] = [
            dict(row) for row in self.connection.execute(
                "SELECT rl.* FROM resource_leases rl JOIN attempts a ON a.id=rl.attempt_id "
                "JOIN state_runs sr ON sr.id=a.state_run_id "
                "WHERE sr.execution_id=? AND rl.status='active' ORDER BY rl.resource_id",
                (execution_id,),
            )
        ]
        workspace = self.workspace_for_execution(execution_id)
        current["workspace"] = (
            {
                key: workspace[key] for key in (
                    "id", "remote", "base_ref", "base_sha", "branch_name", "status",
                    "created_at", "updated_at", "cleaned_at",
                )
            }
            if workspace else None
        )
        attempt = current.get("attempt")
        preparation = None
        if attempt:
            row = self.connection.execute(
                "SELECT id FROM preparations WHERE attempt_id=?", (attempt["id"],)
            ).fetchone()
            preparation = self.preparation(str(row["id"])) if row else None
        current["preparation"] = preparation
        current["resource_allocations"] = [
            self._allocation_dict(row) for row in self.connection.execute(
                "SELECT * FROM resource_allocations WHERE execution_id=? "
                "AND status IN ('active','release_pending') "
                "ORDER BY acquired_at,id", (execution_id,)
            )
        ]
        current["attention_requests"] = [
            self._attention_dict(row) for row in self.connection.execute(
                "SELECT * FROM attention_requests WHERE execution_id=? AND status='open' "
                "ORDER BY created_at,id", (execution_id,)
            )
        ]
        return current

    def events_page(
        self, execution_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE execution_id=? AND seq>? ORDER BY seq LIMIT ?",
            (execution_id, after_seq, limit),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    @staticmethod
    def _bounded_page(after_seq: int, limit: int) -> None:
        if after_seq < 0:
            raise LedgerError("page sequence cannot be negative")
        if limit < 1 or limit > 1000:
            raise LedgerError("page limit must be between 1 and 1000")

    @staticmethod
    def _trace_record_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["links"] = json.loads(item.pop("links_json"))
        item["completeness"] = json.loads(item.pop("completeness_json"))
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def trace_page(
        self, execution_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        self._bounded_page(after_seq, limit)
        rows = self.connection.execute(
            "SELECT * FROM trace_records WHERE execution_id=? AND seq>? "
            "ORDER BY seq LIMIT ?", (execution_id, after_seq, limit),
        ).fetchall()
        return [self._trace_record_dict(row) for row in rows]

    @staticmethod
    def _error_fact_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["retryable"] = bool(item["retryable"])
        item["ambiguous_side_effect"] = bool(item["ambiguous_side_effect"])
        item["capture_complete"] = bool(item["capture_complete"])
        item["completeness"] = json.loads(item.pop("completeness_json"))
        return item

    def error_page(
        self, execution_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        self._bounded_page(after_seq, limit)
        rows = self.connection.execute(
            "SELECT * FROM error_facts WHERE execution_id=? AND seq>? "
            "ORDER BY seq LIMIT ?", (execution_id, after_seq, limit),
        ).fetchall()
        return [self._error_fact_dict(row) for row in rows]

    def artifacts_page(
        self, execution_id: str, *, kind: str | None = None, limit: int = 25,
        before_created_at: str | None = None, before_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["execution_id=?"]
        parameters: list[Any] = [execution_id]
        if kind:
            clauses.append("kind=?")
            parameters.append(kind)
        if before_created_at and before_id:
            clauses.append("(created_at<? OR (created_at=? AND id<?))")
            parameters.extend((before_created_at, before_created_at, before_id))
        parameters.append(limit)
        rows = self.connection.execute(
            "SELECT * FROM artifacts WHERE " + " AND ".join(clauses) +
            " ORDER BY created_at DESC,id DESC LIMIT ?", tuple(parameters)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def feedback_page(
        self, execution_id: str, *, limit: int = 25,
        before_created_at: str | None = None, before_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["execution_id=?"]
        parameters: list[Any] = [execution_id]
        if before_created_at and before_id:
            clauses.append("(created_at<? OR (created_at=? AND id<?))")
            parameters.extend((before_created_at, before_created_at, before_id))
        parameters.append(limit)
        rows = self.connection.execute(
            "SELECT * FROM feedback WHERE " + " AND ".join(clauses) +
            " ORDER BY created_at DESC,id DESC LIMIT ?", tuple(parameters)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["body"] = json.loads(item.pop("body_json"))
            result.append(item)
        return result

    def resource_page(
        self, *, status: str | None = None, project_key: str | None = None,
        execution_id: str | None = None, limit: int = 25,
        before_acquired_at: str | None = None, before_id: str | None = None,
    ) -> list[dict[str, Any]]:
        legacy_clauses: list[str] = []
        allocation_clauses: list[str] = []
        legacy_parameters: list[Any] = []
        allocation_parameters: list[Any] = []
        if status:
            legacy_clauses.append("rl.status=?")
            allocation_clauses.append("ra.status=?")
            legacy_parameters.append(status)
            allocation_parameters.append(status)
        if project_key:
            legacy_clauses.append("wi.project_key=?")
            allocation_clauses.append("wi.project_key=?")
            legacy_parameters.append(project_key)
            allocation_parameters.append(project_key)
        if execution_id:
            legacy_clauses.append("sr.execution_id=?")
            allocation_clauses.append("ra.execution_id=?")
            legacy_parameters.append(execution_id)
            allocation_parameters.append(execution_id)
        if before_acquired_at and before_id:
            legacy_clauses.append(
                "(rl.acquired_at<? OR (rl.acquired_at=? AND rl.id<?))"
            )
            allocation_clauses.append(
                "(ra.acquired_at<? OR (ra.acquired_at=? AND ra.id<?))"
            )
            cursor = (before_acquired_at, before_acquired_at, before_id)
            legacy_parameters.extend(cursor)
            allocation_parameters.extend(cursor)
        legacy_where = (
            " WHERE " + " AND ".join(legacy_clauses) if legacy_clauses else ""
        )
        allocation_where = (
            " WHERE " + " AND ".join(allocation_clauses)
            if allocation_clauses else ""
        )
        legacy_parameters.append(limit)
        allocation_parameters.append(limit)
        resources = []
        for row in self.connection.execute(
            "SELECT rl.*,sr.execution_id,sr.state_id,wi.project_key,"
            "wi.identifier AS work_item_identifier,a.owner "
            "FROM resource_leases rl JOIN attempts a ON a.id=rl.attempt_id "
            "JOIN state_runs sr ON sr.id=a.state_run_id "
            "JOIN workflow_executions we ON we.id=sr.execution_id "
            "JOIN work_items wi ON wi.id=we.work_item_id" + legacy_where +
            " ORDER BY rl.acquired_at DESC,rl.id DESC LIMIT ?", tuple(legacy_parameters)
        ):
            item = dict(row)
            item.update({"record_kind": "legacy_lease", "scope": "attempt",
                         "provider": "legacy", "capability": "legacy",
                         "metadata": {}})
            resources.append(item)
        for row in self.connection.execute(
            "SELECT ra.*,sr.state_id,wi.project_key,"
            "wi.identifier AS work_item_identifier,a.owner "
            "FROM resource_allocations ra "
            "JOIN preparations p ON p.id=ra.preparation_id "
            "JOIN attempts a ON a.id=p.attempt_id "
            "JOIN state_runs sr ON sr.id=a.state_run_id "
            "JOIN workflow_executions we ON we.id=ra.execution_id "
            "JOIN work_items wi ON wi.id=we.work_item_id" + allocation_where +
            " ORDER BY ra.acquired_at DESC,ra.id DESC LIMIT ?",
            tuple(allocation_parameters),
        ):
            item = self._allocation_dict(row)
            item["record_kind"] = "allocation"
            resources.append(item)
        resources.sort(key=lambda item: (item["acquired_at"], item["id"]), reverse=True)
        return resources[:limit]

    def _control_event(
        self, db: sqlite3.Connection, command_id: str, event_type: str,
        payload: dict[str, Any],
    ) -> None:
        event_id = self.id_factory()
        occurred_at = self.clock()
        redacted = redact_payload({"command_id": command_id, **payload})
        inserted = db.execute(
            "INSERT OR IGNORE INTO control_command_events "
            "(event_id,command_id,event_type,occurred_at,payload_json) VALUES(?,?,?,?,?)",
            (event_id, command_id, event_type, occurred_at, canonical_json(redacted)),
        )
        if inserted.rowcount != 1:
            return
        self._fault("after_control_trace_source_recorded")
        source_seq = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        context = db.execute(
            "SELECT cc.execution_id,we.current_state_run_id,a.id AS attempt_id "
            "FROM control_commands cc JOIN workflow_executions we "
            "ON we.id=cc.execution_id LEFT JOIN attempts a "
            "ON a.state_run_id=we.current_state_run_id AND a.status='active' "
            "WHERE cc.command_id=?", (command_id,),
        ).fetchone()
        self._mirror_lifecycle_trace(
            db, event_id=event_id, source_seq=source_seq,
            execution_id=str(context["execution_id"]),
            state_run_id=context["current_state_run_id"],
            attempt_id=context["attempt_id"], event_type=event_type,
            payload=redacted, occurred_at=occurred_at, source_kind="control_event",
        )

    def begin_control_command(
        self, *, command_id: str, execution_id: str, action: str,
        principal: dict[str, Any], request_hash: str, request: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.connection.execute(
            "SELECT 1 FROM workflow_executions WHERE id=?", (execution_id,)
        ).fetchone():
            raise LedgerError("execution not found")
        existing = self.connection.execute(
            "SELECT request_hash FROM control_commands WHERE command_id=?", (command_id,)
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise LedgerError("command ID was reused with different inputs")
            return self.control_command(command_id)
        with self.transaction() as db:
            now = self.clock()
            db.execute(
                "INSERT INTO control_commands VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (command_id, execution_id, action,
                 json.dumps(redact_payload(principal), sort_keys=True), request_hash,
                 json.dumps(redact_payload(request), sort_keys=True), "received", None,
                 None, None, None, now, now),
            )
            self._control_event(
                db, command_id, "control_command_received",
                {"action": action, "execution_id": execution_id, "principal": principal},
            )
        return self.control_command(command_id)

    def authorize_control_command(
        self, command_id: str, *, allowed: bool, reason: str
    ) -> dict[str, Any]:
        with self.transaction() as db:
            status = "authorized" if allowed else "denied"
            db.execute(
                "UPDATE control_commands SET status=?,authorization_decision=?,"
                "authorization_reason=?,updated_at=? WHERE command_id=?",
                (status, "allowed" if allowed else "denied", reason,
                 self.clock(), command_id),
            )
            self._control_event(
                db, command_id,
                "control_command_authorized" if allowed else "control_command_denied",
                {"allowed": allowed, "reason": reason},
            )
        return self.control_command(command_id)

    def finish_control_command(
        self, command_id: str, *, result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            status = "completed" if error is None else "failed"
            db.execute(
                "UPDATE control_commands SET status=?,result_json=?,error_json=?,updated_at=? "
                "WHERE command_id=?",
                (status, json.dumps(redact_payload(result), sort_keys=True) if result else None,
                 json.dumps(redact_payload(error), sort_keys=True) if error else None,
                 self.clock(), command_id),
            )
            self._control_event(
                db, command_id,
                "control_command_completed" if error is None else "control_command_failed",
                result if error is None else {"error": error},
            )
            if result and result.get("reconciliation", {}).get("pending"):
                self._control_event(
                    db, command_id, "control_reconciliation_queued",
                    result["reconciliation"],
                )
        return self.control_command(command_id)

    def control_command(self, command_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM control_commands WHERE command_id=?", (command_id,)
        ).fetchone()
        if not row:
            raise LedgerError("control command not found")
        result = dict(row)
        result["principal"] = json.loads(result.pop("principal_json"))
        result["request"] = json.loads(result.pop("request_json"))
        result["result"] = (
            json.loads(result.pop("result_json")) if result["result_json"] else None
        )
        result["error"] = (
            json.loads(result.pop("error_json")) if result["error_json"] else None
        )
        events = []
        for event in self.connection.execute(
            "SELECT * FROM control_command_events WHERE command_id=? ORDER BY seq",
            (command_id,),
        ):
            item = dict(event)
            item["payload"] = json.loads(item.pop("payload_json"))
            events.append(item)
        result["events"] = events
        return result

    def pending(self, destination: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT o.*,e.event_id,e.event_type,e.schema_version,e.payload_json,"
            "e.execution_id,e.occurred_at,e.idempotency_key AS event_command_id,"
            "we.execution_key,we.workflow_name,we.workflow_version,we.intent_snapshot_json,"
            "wi.project_key,wi.identifier AS work_item_identifier "
            "FROM outbox o JOIN events e ON e.seq=o.event_seq "
            "JOIN workflow_executions we ON we.id=e.execution_id "
            "JOIN work_items wi ON wi.id=we.work_item_id "
            "WHERE o.destination=? AND o.status='pending' ORDER BY o.event_seq LIMIT ?",
            (destination, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def start_projection_attempt(
        self, destination: str, *, command_id: str, idempotency_key: str,
        source_kind: str = "trace_record", from_source_seq: int = 1,
        through_source_seq: int | None = None,
    ) -> dict[str, Any]:
        for value, name in (
            (destination, "destination"), (command_id, "command ID"),
            (idempotency_key, "idempotency key"), (source_kind, "source kind"),
        ):
            if not value.strip():
                raise LedgerError(f"projection {name} is required")
        if from_source_seq < 1:
            raise LedgerError("projection range must start at sequence 1 or later")
        if through_source_seq is None:
            if source_kind != "trace_record":
                raise LedgerError("non-trace projections require a fixed through sequence")
            through_source_seq = int(self.connection.execute(
                "SELECT COALESCE(MAX(seq),0) FROM trace_records"
            ).fetchone()[0])
            through_source_seq = max(through_source_seq, from_source_seq - 1)
        if through_source_seq < from_source_seq - 1:
            raise LedgerError("projection range ends before it starts")
        prior = self.connection.execute(
            "SELECT * FROM projection_attempts WHERE destination=? "
            "AND (command_id=? OR idempotency_key=?)",
            (destination, command_id, idempotency_key),
        ).fetchone()
        expected = (
            command_id, source_kind, from_source_seq, through_source_seq,
            idempotency_key,
        )
        if prior:
            observed = tuple(prior[key] for key in (
                "command_id", "source_kind", "from_source_seq",
                "through_source_seq", "idempotency_key",
            ))
            if observed != expected:
                raise LedgerError("projection attempt identity was reused with new inputs")
            return self.projection_attempt(str(prior["id"]))
        attempt_id = self.id_factory()
        now = self.clock()
        empty = through_source_seq < from_source_seq
        with self.transaction() as db:
            db.execute(
                "INSERT INTO projection_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id, destination, command_id, source_kind,
                    from_source_seq, through_source_seq, idempotency_key,
                    "completed" if empty else "running", now, now,
                    now if empty else None,
                ),
            )
        return self.projection_attempt(attempt_id)

    def projection_attempt(self, attempt_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT pa.*,COUNT(pr.receipt_id) AS receipt_count,"
            "COALESCE(SUM(CASE WHEN pr.status='accepted' THEN 1 ELSE 0 END),0) "
            "AS accepted_count,"
            "COALESCE(SUM(CASE WHEN pr.status='rejected' THEN 1 ELSE 0 END),0) "
            "AS rejected_count,"
            "COALESCE(SUM(CASE WHEN pr.status='duplicate' THEN 1 ELSE 0 END),0) "
            "AS duplicate_count FROM projection_attempts pa "
            "LEFT JOIN projection_receipts pr ON pr.attempt_id=pa.id "
            "WHERE pa.id=? GROUP BY pa.id", (attempt_id,),
        ).fetchone()
        if not row:
            raise LedgerError("projection attempt not found")
        return dict(row)

    def resume_projection_attempt(self, attempt_id: str) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM projection_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not row:
                raise LedgerError("projection attempt not found")
            if row["status"] != "completed":
                db.execute(
                    "UPDATE projection_attempts SET status='running',updated_at=?,"
                    "completed_at=NULL WHERE id=?", (self.clock(), attempt_id),
                )
        return self.projection_attempt(attempt_id)

    def pending_projection_records(
        self, attempt_id: str, *, after_source_seq: int = 0, limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._bounded_page(after_source_seq, limit)
        attempt = self.connection.execute(
            "SELECT * FROM projection_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if not attempt:
            raise LedgerError("projection attempt not found")
        if attempt["source_kind"] != "trace_record":
            raise LedgerError("projection source does not support ledger record paging")
        rows = self.connection.execute(
            "SELECT tr.* FROM trace_records tr WHERE tr.seq BETWEEN ? AND ? "
            "AND tr.seq>? AND NOT EXISTS ("
            "SELECT 1 FROM projection_receipts pr WHERE pr.attempt_id=? "
            "AND pr.source_record_id=tr.record_id "
            "AND pr.status IN ('accepted','duplicate')) ORDER BY tr.seq LIMIT ?",
            (
                attempt["from_source_seq"], attempt["through_source_seq"],
                after_source_seq, attempt_id, limit,
            ),
        ).fetchall()
        return [self._trace_record_dict(row) for row in rows]

    def record_projection_receipt(
        self, receipt: ProjectionReceiptV1, *,
        rejection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = ProjectionReceiptV1(
            receipt_id=receipt.receipt_id, attempt_id=receipt.attempt_id,
            destination=receipt.destination, source_record_id=receipt.source_record_id,
            status=receipt.status, idempotency_key=receipt.idempotency_key,
            recorded_at=receipt.recorded_at, external_id=receipt.external_id,
            error_code=receipt.error_code, detail=redact_payload(dict(receipt.detail)),
        )
        prior = self.connection.execute(
            "SELECT * FROM projection_receipts WHERE receipt_id=? OR "
            "(attempt_id=? AND idempotency_key=?)",
            (normalized.receipt_id, normalized.attempt_id, normalized.idempotency_key),
        ).fetchone()
        if prior:
            observed = (
                prior["receipt_id"], prior["attempt_id"], prior["destination"],
                prior["source_record_id"], prior["status"], prior["idempotency_key"],
                prior["recorded_at"], prior["external_id"], prior["error_code"],
                prior["detail_json"], int(prior["schema_version"]),
            )
            expected = (
                normalized.receipt_id, normalized.attempt_id, normalized.destination,
                normalized.source_record_id, normalized.status,
                normalized.idempotency_key, normalized.recorded_at,
                normalized.external_id, normalized.error_code,
                canonical_json(normalized.detail), normalized.schema_version,
            )
            if observed != expected:
                raise LedgerError("projection receipt identity was reused with new inputs")
            return self._projection_receipt_dict(prior)
        with self.transaction() as db:
            attempt = db.execute(
                "SELECT * FROM projection_attempts WHERE id=?", (normalized.attempt_id,)
            ).fetchone()
            if not attempt:
                raise LedgerError("projection attempt not found")
            if attempt["destination"] != normalized.destination:
                raise LedgerError("projection receipt destination does not match attempt")
            source_record = None
            if attempt["source_kind"] == "trace_record":
                source_record = db.execute(
                    "SELECT * FROM trace_records WHERE record_id=? AND seq BETWEEN ? AND ?",
                    (
                        normalized.source_record_id, attempt["from_source_seq"],
                        attempt["through_source_seq"],
                    ),
                ).fetchone()
                if not source_record:
                    raise LedgerError("projection receipt is outside the fixed source range")
            db.execute(
                "INSERT INTO projection_receipts("
                "receipt_id,attempt_id,destination,source_record_id,status,"
                "idempotency_key,external_id,error_code,detail_json,schema_version,"
                "recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    normalized.receipt_id, normalized.attempt_id,
                    normalized.destination, normalized.source_record_id,
                    normalized.status, normalized.idempotency_key,
                    normalized.external_id, normalized.error_code,
                    canonical_json(normalized.detail), normalized.schema_version,
                    normalized.recorded_at,
                ),
            )
            self._fault("after_projection_receipt_recorded")
            if normalized.status == "rejected":
                details = redact_payload(dict(rejection or {}))
                error_code = str(normalized.error_code or details.get(
                    "code", "PROJECTION_REJECTED"
                ))
                message = str(details.get("message", "destination rejected record"))[:2048]
                db.execute(
                    "INSERT INTO projection_rejections VALUES(?,?,?,?,?,?,?,?)",
                    (
                        normalized.receipt_id, normalized.attempt_id,
                        normalized.destination, normalized.source_record_id,
                        error_code, message, int(bool(details.get("retryable", True))),
                        normalized.recorded_at,
                    ),
                )
                db.execute(
                    "UPDATE projection_attempts SET status='paused',updated_at=? "
                    "WHERE id=?", (normalized.recorded_at, normalized.attempt_id),
                )
            else:
                db.execute(
                    "UPDATE projection_attempts SET updated_at=? WHERE id=?",
                    (normalized.recorded_at, normalized.attempt_id),
                )
            if source_record:
                links = ({
                    "type": "asynchronous_projection",
                    "record_id": normalized.source_record_id,
                },)
                record = TraceRecordV1(
                    record_id=stable_record_id(
                        "projection_receipt", normalized.receipt_id
                    ),
                    execution_id=str(source_record["execution_id"]),
                    source_kind="projection_receipt",
                    source_id=normalized.receipt_id,
                    state_run_id=source_record["state_run_id"],
                    attempt_id=source_record["attempt_id"],
                    trace_id=stable_trace_id(str(source_record["execution_id"])),
                    span_id=stable_span_id(
                        "projection_receipt", normalized.receipt_id
                    ),
                    record_kind=(
                        "error" if normalized.status == "rejected" else "event"
                    ),
                    domain="projection", phase="delivery",
                    name=f"projection_item_{normalized.status}",
                    status="failed" if normalized.status == "rejected" else "completed",
                    entity_kind="projection_attempt",
                    entity_id=normalized.attempt_id,
                    observed_at=normalized.recorded_at,
                    source_occurred_at=normalized.recorded_at,
                    origin="dotfactory-projection",
                    trust_class="trusted-runtime", links=links,
                    completeness=(
                        {"complete": False, "reasons": ["projection_rejected"]}
                        if normalized.status == "rejected" else {}
                    ),
                    payload={
                        "destination": normalized.destination,
                        "source_record_id": normalized.source_record_id,
                        "receipt_status": normalized.status,
                        "error_code": normalized.error_code,
                    },
                )
                self._insert_trace_record(db, record)
                if normalized.status == "rejected":
                    self._insert_error_fact(db, record, {
                        **redact_payload(dict(rejection or {})),
                        "code": normalized.error_code or "PROJECTION_REJECTED",
                    })
        row = self.connection.execute(
            "SELECT * FROM projection_receipts WHERE receipt_id=?",
            (normalized.receipt_id,),
        ).fetchone()
        return self._projection_receipt_dict(row)

    @staticmethod
    def _projection_receipt_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json"))
        return item

    def projection_receipts_page(
        self, attempt_id: str, *, after_seq: int = 0, limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._bounded_page(after_seq, limit)
        rows = self.connection.execute(
            "SELECT * FROM projection_receipts WHERE attempt_id=? AND seq>? "
            "ORDER BY seq LIMIT ?", (attempt_id, after_seq, limit),
        ).fetchall()
        return [self._projection_receipt_dict(row) for row in rows]

    def advance_projection_watermark(
        self, attempt_id: str, *, through_source_seq: int,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            attempt = db.execute(
                "SELECT * FROM projection_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not attempt:
                raise LedgerError("projection attempt not found")
            if not (
                int(attempt["from_source_seq"]) - 1 <= through_source_seq
                <= int(attempt["through_source_seq"])
            ):
                raise LedgerError("projection watermark is outside the fixed source range")
            if attempt["source_kind"] == "trace_record":
                missing = int(db.execute(
                    "SELECT COUNT(*) FROM trace_records tr WHERE tr.seq BETWEEN ? AND ? "
                    "AND NOT EXISTS (SELECT 1 FROM projection_receipts pr "
                    "WHERE pr.attempt_id=? AND pr.source_record_id=tr.record_id "
                    "AND pr.status IN ('accepted','duplicate'))",
                    (attempt["from_source_seq"], through_source_seq, attempt_id),
                ).fetchone()[0])
                if missing:
                    raise LedgerError("projection watermark would skip unaccepted records")
            prior = db.execute(
                "SELECT * FROM projection_watermarks WHERE destination=? AND source_kind=?",
                (attempt["destination"], attempt["source_kind"]),
            ).fetchone()
            if prior and int(prior["through_source_seq"]) > through_source_seq:
                raise LedgerError("projection watermark cannot move backward")
            now = self.clock()
            if prior:
                db.execute(
                    "UPDATE projection_watermarks SET through_source_seq=?,attempt_id=?,"
                    "updated_at=? WHERE destination=? AND source_kind=?",
                    (
                        through_source_seq, attempt_id, now, attempt["destination"],
                        attempt["source_kind"],
                    ),
                )
            else:
                db.execute(
                    "INSERT INTO projection_watermarks VALUES(?,?,?,?,?)",
                    (
                        attempt["destination"], attempt["source_kind"],
                        through_source_seq, attempt_id, now,
                    ),
                )
            if through_source_seq == int(attempt["through_source_seq"]):
                db.execute(
                    "UPDATE projection_attempts SET status='completed',updated_at=?,"
                    "completed_at=? WHERE id=?", (now, now, attempt_id),
                )
        return dict(self.connection.execute(
            "SELECT * FROM projection_watermarks WHERE destination=? AND source_kind=?",
            (attempt["destination"], attempt["source_kind"]),
        ).fetchone())

    def start_projection_rebuild(
        self, destination: str, *, command_id: str, requested_by: str,
        from_event_seq: int = 1,
    ) -> dict[str, Any]:
        if from_event_seq < 1:
            raise LedgerError("projection replay must start at event sequence 1 or later")
        if not requested_by.strip():
            raise LedgerError("projection replay requires an initiator")
        prior = self.connection.execute(
            "SELECT * FROM projection_replays WHERE destination=? AND command_id=?",
            (destination, command_id),
        ).fetchone()
        if prior:
            if (
                int(prior["from_event_seq"]) != from_event_seq
                or prior["requested_by"] != requested_by
            ):
                raise LedgerError("projection replay command was reused with different inputs")
            return self.projection_rebuild(str(prior["id"]))
        with self.transaction() as db:
            prior = db.execute(
                "SELECT * FROM projection_replays WHERE destination=? AND command_id=?",
                (destination, command_id),
            ).fetchone()
            if prior:
                if (
                    int(prior["from_event_seq"]) != from_event_seq
                    or prior["requested_by"] != requested_by
                ):
                    raise LedgerError("projection replay command was reused with different inputs")
                replay_id = str(prior["id"])
            else:
                through_event_seq = int(db.execute(
                    "SELECT COALESCE(MAX(event_seq),0) FROM outbox "
                    "WHERE destination=? AND event_seq>=?",
                    (destination, from_event_seq),
                ).fetchone()[0])
                replay_id = self.id_factory()
                empty = through_event_seq == 0
                db.execute(
                    "INSERT INTO projection_replays VALUES(?,?,?,?,?,?,?,?,?)",
                    (replay_id, destination, command_id, requested_by, from_event_seq,
                     through_event_seq, "completed" if empty else "running",
                     self.clock(), self.clock() if empty else None),
                )
                if not empty:
                    db.execute(
                        "INSERT INTO projection_replay_items("
                        "replay_id,outbox_id,event_seq,status,delivery_attempts) "
                        "SELECT ?,id,event_seq,'pending',0 FROM outbox "
                        "WHERE destination=? AND event_seq BETWEEN ? AND ?",
                        (replay_id, destination, from_event_seq, through_event_seq),
                    )
        return self.projection_rebuild(replay_id)

    def projection_rebuild(self, replay_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT pr.*,COUNT(pri.outbox_id) AS queued,"
            "COALESCE(SUM(CASE WHEN pri.status='delivered' THEN 1 ELSE 0 END),0) "
            "AS delivered,"
            "COALESCE(SUM(CASE WHEN pri.status='pending' THEN 1 ELSE 0 END),0) AS pending "
            "FROM projection_replays pr LEFT JOIN projection_replay_items pri "
            "ON pri.replay_id=pr.id WHERE pr.id=? GROUP BY pr.id", (replay_id,),
        ).fetchone()
        if not row:
            raise LedgerError("projection replay not found")
        return dict(row)

    def pending_rebuild(self, replay_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT pri.replay_id,pri.outbox_id,pri.event_seq,pri.status,"
            "pri.delivery_attempts,pri.last_error,pri.delivered_at,"
            "o.destination,e.event_id,e.event_type,e.schema_version,e.payload_json,"
            "e.execution_id,e.occurred_at,e.idempotency_key AS event_command_id,"
            "we.execution_key,we.workflow_name,we.workflow_version,we.intent_snapshot_json,"
            "wi.project_key,wi.identifier AS work_item_identifier "
            "FROM projection_replay_items pri JOIN outbox o ON o.id=pri.outbox_id "
            "JOIN events e ON e.seq=pri.event_seq "
            "JOIN workflow_executions we ON we.id=e.execution_id "
            "JOIN work_items wi ON wi.id=we.work_item_id "
            "WHERE pri.replay_id=? AND pri.status='pending' "
            "ORDER BY pri.event_seq LIMIT ?",
            (replay_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def resume_projection_rebuild(self, replay_id: str) -> None:
        self.connection.execute(
            "UPDATE projection_replays SET status='running',completed_at=NULL "
            "WHERE id=? AND status<>'completed'", (replay_id,),
        )

    def mark_rebuild_delivered(self, replay_id: str, outbox_id: str) -> None:
        with self.transaction() as db:
            item = db.execute(
                "SELECT status FROM projection_replay_items "
                "WHERE replay_id=? AND outbox_id=?", (replay_id, outbox_id),
            ).fetchone()
            if not item:
                raise LedgerError("projection replay item not found")
            if item["status"] != "delivered":
                db.execute(
                    "UPDATE projection_replay_items SET status='delivered',"
                    "delivery_attempts=delivery_attempts+1,delivered_at=? "
                    "WHERE replay_id=? AND outbox_id=?",
                    (self.clock(), replay_id, outbox_id),
                )
            if not db.execute(
                "SELECT 1 FROM projection_replay_items "
                "WHERE replay_id=? AND status='pending' LIMIT 1", (replay_id,),
            ).fetchone():
                db.execute(
                    "UPDATE projection_replays SET status='completed',completed_at=? "
                    "WHERE id=?", (self.clock(), replay_id),
                )

    def mark_rebuild_failed(self, replay_id: str, outbox_id: str, error: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE projection_replay_items SET delivery_attempts=delivery_attempts+1,"
                "last_error=? WHERE replay_id=? AND outbox_id=? AND status='pending'",
                (error[:500], replay_id, outbox_id),
            )
            db.execute(
                "UPDATE projection_replays SET status='paused' WHERE id=?", (replay_id,)
            )

    def mark_delivered(self, outbox_id: str) -> None:
        self.connection.execute(
            "UPDATE outbox SET status='delivered',delivery_attempts=delivery_attempts+1,"
            "delivered_at=?,last_error=NULL WHERE id=?", (self.clock(), outbox_id),
        )

    def mark_failed(self, outbox_id: str, error: str) -> None:
        self.connection.execute(
            "UPDATE outbox SET delivery_attempts=delivery_attempts+1,last_error=? WHERE id=?",
            (error[:500], outbox_id),
        )

    def export_jsonl(self, path: str | Path) -> None:
        with Path(path).open("w", encoding="utf-8") as output:
            for row in self.connection.execute("SELECT * FROM events ORDER BY seq"):
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                output.write(json.dumps(item, sort_keys=True) + "\n")
