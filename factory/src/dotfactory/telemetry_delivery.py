"""Projection-owned durable OTLP plans, batches, and coverage in the ledger DB."""

from __future__ import annotations

import hashlib
import json
import contextlib
import fcntl
import os
import threading
from pathlib import Path
from typing import Any

from .ledger import LedgerError, SQLiteLedger
from .observability import canonical_json
from .telemetry_mapping import (
    OTEL_MAPPING_VERSION, record_spans, span_document,
)


class TelemetryOutbox:
    """No network inside transactions; no receipt before ancestor acceptance."""

    def __init__(self, ledger: SQLiteLedger, *, destination: str) -> None:
        self.ledger = ledger
        self.destination = destination
        # Independent namespace: do not consume a canonical ledger schema version.
        with ledger.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS otlp_schema ("
                       "component TEXT PRIMARY KEY,version INTEGER NOT NULL)")
            row = db.execute("SELECT version FROM otlp_schema WHERE component='delivery'").fetchone()
            if row and int(row[0]) != 1:
                raise LedgerError("unsupported OTLP delivery schema")
            db.execute("INSERT OR IGNORE INTO otlp_schema VALUES('delivery',1)")
            db.execute("CREATE TABLE IF NOT EXISTS otlp_plans ("
                       "attempt_id TEXT PRIMARY KEY REFERENCES projection_attempts(id),"
                       "mapping_version INTEGER NOT NULL,service_name TEXT NOT NULL,"
                       "batch_size INTEGER NOT NULL,created_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS otlp_plan_failures ("
                       "attempt_id TEXT PRIMARY KEY REFERENCES projection_attempts(id),"
                       "error_code TEXT NOT NULL,recorded_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS otlp_spans ("
                       "destination TEXT NOT NULL,span_id TEXT NOT NULL,"
                       "parent_span_id TEXT,body_json TEXT NOT NULL,"
                       "accepted INTEGER NOT NULL DEFAULT 0,"
                       "PRIMARY KEY(destination,span_id))")
            db.execute("CREATE TABLE IF NOT EXISTS otlp_dependencies ("
                       "attempt_id TEXT NOT NULL REFERENCES otlp_plans(attempt_id),"
                       "record_id TEXT NOT NULL REFERENCES trace_records(record_id),"
                       "span_id TEXT NOT NULL,PRIMARY KEY(attempt_id,record_id,span_id))")
            db.execute("CREATE TABLE IF NOT EXISTS otlp_batches ("
                       "attempt_id TEXT NOT NULL REFERENCES otlp_plans(attempt_id),"
                       "ordinal INTEGER NOT NULL,body_json TEXT NOT NULL,"
                       "body_sha256 TEXT NOT NULL,span_ids_json TEXT NOT NULL,"
                       "status TEXT NOT NULL,delivery_count INTEGER NOT NULL DEFAULT 0,"
                       "outcome_json TEXT,PRIMARY KEY(attempt_id,ordinal))")
            db.execute("CREATE TABLE IF NOT EXISTS otlp_deliveries ("
                       "attempt_id TEXT NOT NULL,ordinal INTEGER NOT NULL,"
                       "delivery INTEGER NOT NULL,status TEXT NOT NULL,"
                       "outcome_json TEXT,started_at TEXT NOT NULL,completed_at TEXT,"
                       "PRIMARY KEY(attempt_id,ordinal,delivery),"
                       "FOREIGN KEY(attempt_id,ordinal) REFERENCES otlp_batches(attempt_id,ordinal))")

    @contextlib.contextmanager
    def delivery_lock(self):
        """One sender per local DB; kernel releases the lock on process death."""
        path = self.ledger.connection.execute("PRAGMA database_list").fetchone()[2]
        if not path:
            if not hasattr(self.ledger, "_otlp_delivery_lock"):
                self.ledger._otlp_delivery_lock = threading.Lock()
            lock = self.ledger._otlp_delivery_lock
            acquired = lock.acquire(blocking=False)
            try:
                yield acquired
            finally:
                if acquired:
                    lock.release()
            return
        lock_path = str(Path(path).resolve()) + ".otlp-v2.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def freeze(self, attempt: dict[str, Any], *, service_name: str, batch_size: int) -> bool:
        """Read raw rows in <=1000 pages; commit the entire plan before first IO."""
        attempt_id = str(attempt["id"])
        with self.ledger.transaction() as db:
            if db.execute("SELECT 1 FROM otlp_plans WHERE attempt_id=?", (attempt_id,)).fetchone():
                return True
            other = db.execute(
                "SELECT a.id FROM projection_attempts a WHERE a.destination=? "
                "AND a.status<>'completed' AND a.id<>? AND ("
                "EXISTS(SELECT 1 FROM otlp_plans p WHERE p.attempt_id=a.id) OR "
                "EXISTS(SELECT 1 FROM otlp_plan_failures f WHERE f.attempt_id=a.id)) LIMIT 1",
                (self.destination, attempt_id),
            ).fetchone()
            if other:
                db.execute("UPDATE projection_attempts SET status='paused' WHERE id=?", (attempt_id,))
                return False
            db.execute("INSERT INTO otlp_plans VALUES(?,?,?,?,?)", (
                attempt_id, OTEL_MAPPING_VERSION, service_name, batch_size, self.ledger.clock(),
            ))
            cursor = 0
            while True:
                rows = db.execute(
                    "SELECT tr.*,rr.preparation_id AS otlp_preparation_id FROM trace_records tr "
                    "LEFT JOIN runner_runs rr ON rr.id=tr.runner_run_id "
                    "WHERE tr.seq>? AND tr.seq<=? AND tr.execution_id IN ("
                    "SELECT execution_id FROM trace_records WHERE seq BETWEEN ? AND ?) "
                    "ORDER BY tr.seq LIMIT 1000",
                    (cursor, attempt["through_source_seq"], attempt["from_source_seq"],
                     attempt["through_source_seq"]),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    record = self.ledger._trace_record_dict(row)
                    record["_preparation_id"] = row["otlp_preparation_id"]
                    table = {"resource_allocation": "resource_allocations",
                             "resource_mutation": "resource_mutations"}.get(record["entity_kind"])
                    if table:
                        owner = db.execute(f"SELECT preparation_id FROM {table} WHERE id=?",
                                           (record["entity_id"],)).fetchone()
                        if owner:
                            record["_preparation_id"] = owner[0]
                    error = db.execute("SELECT code,category,fingerprint,ambiguous_side_effect,capture_complete "
                                       "FROM error_facts WHERE trace_record_id=?", (record["record_id"],)).fetchone()
                    if error:
                        record["_error"] = dict(error)
                    candidates = record_spans(record)
                    for span in candidates:
                        # Later observations may omit ownership metadata. The first
                        # frozen context remains authoritative, including its parent.
                        db.execute("INSERT OR IGNORE INTO otlp_spans VALUES(?,?,?,?,0)", (
                            self.destination, span["spanId"], span.get("parentSpanId"),
                            canonical_json(span),
                        ))
                    if int(row["seq"]) >= int(attempt["from_source_seq"]):
                        # The frozen leaf's actual ancestry is the coverage contract.
                        span_id = candidates[-1]["spanId"]
                        while span_id:
                            db.execute("INSERT OR IGNORE INTO otlp_dependencies VALUES(?,?,?)", (
                                attempt_id, record["record_id"], span_id,
                            ))
                            span_id = db.execute(
                                "SELECT parent_span_id FROM otlp_spans WHERE destination=? AND span_id=?",
                                (self.destination, span_id),
                            ).fetchone()[0]
                cursor = int(rows[-1]["seq"])
            # Insertion rowid is topological: parents are frozen before children.
            cursor = 0
            ordinal = 0
            while True:
                rows = db.execute(
                    "SELECT s.rowid,s.* FROM otlp_spans s WHERE s.destination=? "
                    "AND s.accepted=0 AND s.rowid>? AND EXISTS ("
                    "SELECT 1 FROM otlp_dependencies d WHERE d.attempt_id=? AND d.span_id=s.span_id) "
                    "ORDER BY s.rowid LIMIT ?",
                    (self.destination, cursor, attempt_id, batch_size),
                ).fetchall()
                if not rows:
                    break
                body = canonical_json(span_document(
                    [json.loads(row["body_json"]) for row in rows], service_name, self.destination,
                ))
                db.execute("INSERT INTO otlp_batches VALUES(?,?,?,?,?,'pending',0,NULL)", (
                    attempt_id, ordinal, body, hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    canonical_json([row["span_id"] for row in rows]),
                ))
                cursor = int(rows[-1]["rowid"])
                ordinal += 1
            self.ledger._fault("after_otlp_plan_frozen")
            self._cover(db, attempt_id)
        return True

    def next_batch(self, attempt_id: str) -> dict[str, Any] | None:
        row = self.ledger.connection.execute(
            "SELECT * FROM otlp_batches WHERE attempt_id=? AND status<>'accepted' "
            "ORDER BY ordinal LIMIT 1", (attempt_id,),
        ).fetchone()
        return dict(row) if row else None

    def plan_failure(self, attempt_id: str) -> str | None:
        row = self.ledger.connection.execute(
            "SELECT error_code FROM otlp_plan_failures WHERE attempt_id=?", (attempt_id,),
        ).fetchone()
        return str(row[0]) if row else None

    def block_invalid_plan(self, attempt_id: str) -> None:
        with self.ledger.transaction() as db:
            db.execute("INSERT OR IGNORE INTO otlp_plan_failures VALUES(?,'LOGFIRE_SOURCE_INVALID',?)",
                       (attempt_id, self.ledger.clock()))
            db.execute("UPDATE projection_attempts SET status='paused',updated_at=? WHERE id=?",
                       (self.ledger.clock(), attempt_id))

    def begin_delivery(self, batch: dict[str, Any]) -> None:
        with self.ledger.transaction() as db:
            db.execute("UPDATE otlp_batches SET status='in_flight',delivery_count=delivery_count+1 "
                       "WHERE attempt_id=? AND ordinal=?", (batch["attempt_id"], batch["ordinal"]))
            count = db.execute("SELECT delivery_count FROM otlp_batches WHERE attempt_id=? AND ordinal=?",
                               (batch["attempt_id"], batch["ordinal"])).fetchone()[0]
            db.execute("INSERT INTO otlp_deliveries VALUES(?,?,?,'unknown',NULL,?,NULL)", (
                batch["attempt_id"], batch["ordinal"], count, self.ledger.clock(),
            ))
            db.execute("UPDATE projection_attempts SET status='running',updated_at=? WHERE id=?", (
                self.ledger.clock(), batch["attempt_id"],
            ))

    def acknowledge(self, batch: dict[str, Any], outcome: dict[str, Any]) -> None:
        """The accepted batch and all newly covered sources are one commit."""
        with self.ledger.transaction() as db:
            self._outcome(db, batch, "accepted", outcome)
            for span_id in json.loads(batch["span_ids_json"]):
                db.execute("UPDATE otlp_spans SET accepted=1 WHERE destination=? AND span_id=?", (
                    self.destination, span_id,
                ))
            self._cover(db, str(batch["attempt_id"]))
            self.ledger._fault("after_otlp_batch_acknowledged")

    def reject(self, batch: dict[str, Any], outcome: dict[str, Any]) -> None:
        with self.ledger.transaction() as db:
            self._outcome(db, batch, "retryable" if outcome["retryable"] else "blocked", outcome)
            db.execute("UPDATE projection_attempts SET status='paused',updated_at=? WHERE id=?", (
                self.ledger.clock(), batch["attempt_id"],
            ))

    def _outcome(self, db: Any, batch: dict[str, Any], status: str, outcome: dict[str, Any]) -> None:
        db.execute("UPDATE otlp_batches SET status=?,outcome_json=? WHERE attempt_id=? AND ordinal=?", (
            status, canonical_json(outcome), batch["attempt_id"], batch["ordinal"],
        ))
        db.execute("UPDATE otlp_deliveries SET status=?,outcome_json=?,completed_at=? "
                   "WHERE attempt_id=? AND ordinal=? AND delivery=(SELECT delivery_count "
                   "FROM otlp_batches WHERE attempt_id=? AND ordinal=?)", (
                       status, canonical_json(outcome), self.ledger.clock(), batch["attempt_id"],
                       batch["ordinal"], batch["attempt_id"], batch["ordinal"],
                   ))

    def _cover(self, db: Any, attempt_id: str) -> None:
        # No recursive trace-record mirror: delivery facts live in otlp_deliveries.
        rows = db.execute(
            "SELECT DISTINCT d.record_id FROM otlp_dependencies d WHERE d.attempt_id=? "
            "AND NOT EXISTS (SELECT 1 FROM otlp_dependencies missing JOIN otlp_spans s "
            "ON s.span_id=missing.span_id AND s.destination=? WHERE missing.attempt_id=d.attempt_id "
            "AND missing.record_id=d.record_id AND s.accepted=0) AND NOT EXISTS ("
            "SELECT 1 FROM projection_receipts r WHERE r.attempt_id=d.attempt_id "
            "AND r.source_record_id=d.record_id AND r.status='accepted')",
            (attempt_id, self.destination),
        )
        for row in rows:
            record_id = str(row[0])
            identity = hashlib.sha256(f"{attempt_id}:{record_id}:otel-v2".encode()).hexdigest()
            detail = canonical_json({
                "mapping_version": OTEL_MAPPING_VERSION,
                "coverage": "observation_and_all_structural_ancestors",
                "credential_kind": "write_token", "required_purpose": "otlp_trace_write",
            })
            db.execute("INSERT INTO projection_receipts(receipt_id,attempt_id,destination,"
                       "source_record_id,status,idempotency_key,external_id,error_code,detail_json,"
                       "schema_version,recorded_at) VALUES(?,?,?,?,'accepted',?,(SELECT trace_id FROM trace_records WHERE record_id=?),NULL,?,1,?)", (
                           f"otlp-{identity[:32]}", attempt_id, self.destination, record_id,
                           f"otlp:{identity}", record_id, detail, self.ledger.clock(),
                       ))
            self.ledger._fault("after_projection_receipt_recorded")

    def finish(self, attempt_id: str) -> None:
        with self.ledger.transaction() as db:
            attempt = db.execute("SELECT * FROM projection_attempts WHERE id=?", (attempt_id,)).fetchone()
            missing = db.execute(
                "SELECT 1 FROM trace_records t WHERE t.seq BETWEEN ? AND ? AND NOT EXISTS ("
                "SELECT 1 FROM projection_receipts r WHERE r.attempt_id=? AND r.source_record_id=t.record_id "
                "AND r.status='accepted') LIMIT 1",
                (attempt["from_source_seq"], attempt["through_source_seq"], attempt_id),
            ).fetchone()
            if missing:
                raise LedgerError("OTLP plan completed without full source coverage")
            now = self.ledger.clock()
            db.execute("UPDATE projection_attempts SET status='completed',updated_at=?,completed_at=? WHERE id=?",
                       (now, now, attempt_id))
            # A manual high range cannot skip an earlier unaccepted source range.
            first_gap = db.execute(
                "SELECT MIN(t.seq) FROM trace_records t WHERE NOT EXISTS ("
                "SELECT 1 FROM projection_receipts r WHERE r.destination=? "
                "AND r.source_record_id=t.record_id AND r.status='accepted')", (self.destination,),
            ).fetchone()[0]
            through = int(first_gap) - 1 if first_gap else int(db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM trace_records").fetchone()[0])
            db.execute("INSERT INTO projection_watermarks VALUES(?,'trace_record',?,?,?) "
                       "ON CONFLICT(destination,source_kind) DO UPDATE SET through_source_seq=excluded.through_source_seq,"
                       "attempt_id=excluded.attempt_id,updated_at=excluded.updated_at", (
                           self.destination, through, attempt_id, now,
                       ))

    def result(self, attempt_id: str) -> dict[str, Any]:
        result = self.ledger.projection_attempt(attempt_id)
        batch = self.next_batch(attempt_id)
        result["mapping_version"] = OTEL_MAPPING_VERSION
        result["delivery"] = {
            "status": batch["status"] if batch else result["status"],
            "batch_ordinal": batch["ordinal"] if batch else None,
            "outcome": json.loads(batch["outcome_json"]) if batch and batch["outcome_json"] else None,
        }
        failure = self.plan_failure(attempt_id)
        if failure:
            result["delivery"] = {"status": "blocked", "batch_ordinal": None,
                                  "outcome": {"code": failure, "retryable": False, "ambiguous": False}}
        return result
