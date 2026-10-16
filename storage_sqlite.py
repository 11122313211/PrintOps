"""SQLite order store: normalized projections and atomic snapshot/event CAS."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from typing import Any

from storage_contract import (STORE_CONTRACT_VERSION, StoreConflictError, change_event,
                              json_text, message_delta, prepare_record, summary, timestamp)
from request_runtime import DeadlineExceeded, check_deadline, current_deadline, remaining


class _BudgetConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        # SQLite's busy handler otherwise gets a fresh five seconds at every
        # statement, even when the HTTP request only has milliseconds left.
        budget = remaining()
        if budget is not None:
            super().execute(f"PRAGMA busy_timeout = {max(0, min(5000, int(budget * 1000)))}")
        return super().execute(sql, parameters)


class SQLiteOrderStore:
    BUSY_TIMEOUT_MS = 5000

    def __init__(self, path: str | Path = "data/agent.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def metadata(self) -> dict[str, Any]:
        return {"mode": "sqlite", "persistent": True, "contractVersion": STORE_CONTRACT_VERSION,
                "oneOrderPerSession": True}

    @contextmanager
    def _db(self):
        budget = remaining()
        timeout = min(self.BUSY_TIMEOUT_MS / 1000, budget) if budget is not None else self.BUSY_TIMEOUT_MS / 1000
        connection = sqlite3.connect(self.path, timeout=timeout, factory=_BudgetConnection)
        connection.row_factory = sqlite3.Row
        deadline = current_deadline()
        if deadline is not None:
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                yield connection
                # Raising here rolls back instead of committing a mutation
                # whose computation/lock wait consumed the remaining budget.
                check_deadline()
        except sqlite3.OperationalError:
            if deadline is not None and time.monotonic() >= deadline - 0.005:
                raise DeadlineExceeded("request deadline expired during SQLite operation") from None
            raise
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()

    def _migrate(self) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            statements = [
                "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS storage_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS legacy_session_backups (session_id TEXT PRIMARY KEY, state TEXT NOT NULL, migrated_at TEXT NOT NULL)",
                """CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL,
                    schema_version TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
                    order_json TEXT NOT NULL, runtime_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS order_events (
                    event_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    event_type TEXT NOT NULL, payload_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS order_events_order ON order_events(order_id, revision, created_at)",
                """CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, sequence)",
                """CREATE TABLE IF NOT EXISTS quote_requests (
                    order_id TEXT NOT NULL, request_id TEXT NOT NULL, request_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(order_id, request_id))""",
                """CREATE TABLE IF NOT EXISTS audit_log (
                    audit_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    action TEXT NOT NULL, payload_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)""",
            ]
            for statement in statements:
                db.execute(statement)
            for row in db.execute("SELECT id, state FROM sessions WHERE id NOT IN (SELECT session_id FROM orders)").fetchall():
                try:
                    state = json.loads(row["state"])
                    if not isinstance(state, dict):
                        continue
                    from order_model import normalize_state
                    normalized = normalize_state(deepcopy(state))
                except (ValueError, TypeError, AttributeError, KeyError):
                    continue  # load_order retains the existing quarantine behavior.
                db.execute("INSERT OR IGNORE INTO legacy_session_backups VALUES (?, ?, ?)",
                           (row["id"], row["state"], timestamp()))
                self._save(db, {"orderId": row["id"], "state": normalized,
                                "events": [{"eventType": "legacy_migrated", "actor": "migration",
                                            "payload": {"source": "sessions"}}]}, 0)
            db.execute("INSERT OR IGNORE INTO storage_migrations VALUES (1, ?)", (timestamp(),))

    def _load(self, db, order_id: str) -> dict[str, Any] | None:
        row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            return None
        state = json.loads(row["runtime_json"])
        state["order"] = json.loads(row["order_json"])
        state["revision"] = row["revision"]
        messages = db.execute("SELECT role, content FROM messages WHERE session_id = ? ORDER BY sequence DESC LIMIT 80",
                              (row["session_id"],)).fetchall()
        state["messages"] = [{"role": m["role"], "text": m["content"]} for m in reversed(messages)]
        quotes = db.execute("SELECT request_json FROM quote_requests WHERE order_id = ? ORDER BY rowid DESC LIMIT 40",
                            (order_id,)).fetchall()
        state["quoteRequests"] = [json.loads(q[0]) for q in reversed(quotes)]
        return {"orderId": row["order_id"], "sessionId": row["session_id"], "state": state,
                "revision": row["revision"], "status": row["status"],
                "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    def load_order(self, order_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            # The legacy mirror remains for compatibility with existing local
            # tooling. A damaged mirror is quarantined, never silently trusted.
            legacy = db.execute("SELECT state FROM sessions WHERE id = ?", (order_id,)).fetchone()
            if legacy:
                try:
                    raw = json.loads(legacy[0])
                    if not isinstance(raw, dict):
                        raise ValueError("会话状态不是 JSON 对象")
                except (TypeError, ValueError) as error:
                    self._quarantine(db, order_id, legacy[0], error)
                    return None
            try:
                return self._load(db, order_id)
            except (TypeError, ValueError) as error:
                row = db.execute("SELECT order_json, runtime_json FROM orders WHERE order_id = ?", (order_id,)).fetchone()
                self._quarantine(db, order_id, json_text(dict(row)) if row else "", error)
                return None

    def _quarantine(self, db, order_id: str, raw: str, error: Exception) -> None:
        backup_dir = self.path.parent / "corrupted"
        backup_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", order_id)[:64] or "session"
        try:
            snapshot = self._load(db, order_id)
        except (TypeError, ValueError):
            row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            snapshot = dict(row) if row else None
        backup = {"sessionId": order_id, "error": str(error), "raw": raw, "snapshot": snapshot,
                  "messages": [dict(row) for row in db.execute("SELECT * FROM messages WHERE session_id = ?", (order_id,))],
                  "quoteRequests": [dict(row) for row in db.execute("SELECT * FROM quote_requests WHERE order_id = ?", (order_id,))]}
        (backup_dir / f"{safe_id}-{uuid.uuid4().hex}.json").write_text(json_text(backup), encoding="utf-8")
        # Keep append-only events/audit intact; only reset current projections.
        for table, column in (("orders", "order_id"), ("messages", "session_id"),
                              ("quote_requests", "order_id"), ("sessions", "id")):
            db.execute(f"DELETE FROM {table} WHERE {column} = ?", (order_id,))
        self._insert_audit(db, order_id, 0, "corruption_quarantined", {"error": type(error).__name__}, "system")

    def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            order_id = str(order.get("orderId") or order.get("sessionId") or "")
            previous = self._load(db, order_id)
            if previous is not None:
                raise StoreConflictError(0, previous["revision"])
            return self._save(db, order, 0)

    def save_order(self, order: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._save(db, order, expected_revision)

    def _save(self, db, order: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        order_id = str(order.get("orderId") or order.get("sessionId") or "")
        previous = self._load(db, order_id)
        record = prepare_record(order, previous, expected_revision)
        if previous == record:
            return record
        state = record["state"]
        runtime = {key: value for key, value in state.items() if key not in {"order", "messages", "quoteRequests"}}
        db.execute("""INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET schema_version=excluded.schema_version,
            revision=excluded.revision,status=excluded.status,order_json=excluded.order_json,
            runtime_json=excluded.runtime_json,updated_at=excluded.updated_at""",
            (record["orderId"], record["sessionId"], str(state.get("schemaVersion", "2.0")),
             record["revision"], record["status"], json_text(state.get("order", {})), json_text(runtime),
             record["createdAt"], record["updatedAt"]))
        incoming = order.get("newMessages")
        if incoming is None:
            incoming = message_delta(previous["state"].get("messages", []) if previous else [], state.get("messages", []))
        for message in incoming:
            if isinstance(message, dict) and isinstance(message.get("text"), str):
                db.execute("INSERT INTO messages(message_id,session_id,role,content,created_at) VALUES (?, ?, ?, ?, ?)",
                           (uuid.uuid4().hex, record["sessionId"], str(message.get("role", "user")), message["text"], timestamp()))
        for quote in state.get("quoteRequests", []):
            if isinstance(quote, dict) and quote.get("requestId"):
                db.execute("""INSERT INTO quote_requests VALUES (?, ?, ?, ?)
                    ON CONFLICT(order_id,request_id) DO UPDATE SET request_json=excluded.request_json,updated_at=excluded.updated_at""",
                    (order_id, str(quote["requestId"]), json_text(quote), timestamp()))
        self._insert_event(db, record, change_event(record, previous))
        for event in order.get("events", []):
            self._insert_event(db, record, event)
        before = previous["state"] if previous else {}
        for key, action in (("confirmation", "confirmation_changed"), ("handoff", "handoff_changed"),
                            ("quoteRequests", "quote_requests_changed"), ("orderStatus", "status_changed")):
            if state.get(key) != before.get(key) and state.get(key):
                self._insert_audit(db, order_id, record["revision"], action,
                                   {"before": before.get(key), "after": state.get(key)}, "agent")
        # Compatibility mirror, committed in the same transaction. Public
        # history APIs read orders/projections and never read browser snapshots.
        db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (record["sessionId"], json_text(state)))
        return deepcopy(record)

    def _insert_event(self, db, record: dict[str, Any], event: dict[str, Any]) -> None:
        db.execute("INSERT INTO order_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (uuid.uuid4().hex, record["orderId"], record["revision"],
                    str(event.get("eventType") or "note"), json_text(event.get("payload", {})),
                    str(event.get("actor") or "system"), timestamp()))

    def _insert_audit(self, db, order_id: str, revision: int, action: str, payload: Any, actor: str) -> None:
        db.execute("INSERT INTO audit_log VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (uuid.uuid4().hex, order_id, revision, action, json_text(payload), actor, timestamp()))

    def append_event(self, order_id: str, event: dict[str, Any]) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._load(db, order_id)
            if record is None:
                raise KeyError(order_id)
            self._insert_event(db, record, event)

    def load_events(self, order_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return self._events(db, order_id)

    def find_operation_receipt(self, order_id: str, operation_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            # Read only small receipt events, never all historical snapshots.
            # JSON parsing here keeps compatibility with SQLite without JSON1.
            for row in db.execute("SELECT payload_json FROM order_events WHERE order_id = ? AND event_type = 'operation_receipt' ORDER BY rowid DESC", (order_id,)):
                receipt = json.loads(row["payload_json"])
                if receipt.get("operationId") == operation_id:
                    return receipt
        return None

    def _events(self, db, order_id: str) -> list[dict[str, Any]]:
        return [{"eventId": r["event_id"], "revision": r["revision"], "eventType": r["event_type"],
                 "payload": json.loads(r["payload_json"]), "actor": r["actor"], "createdAt": r["created_at"]}
                for r in db.execute("SELECT * FROM order_events WHERE order_id = ? ORDER BY rowid", (order_id,))]

    def list_orders(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = filters or {}
        limit, offset = max(1, min(100, int(filters.get("limit", 50)))), max(0, int(filters.get("offset", 0)))
        clauses, args = [], []
        if not filters.get("includeArchived", False):
            clauses.append("status <> 'archived'")
        if filters.get("status"):
            clauses.append("status = ?")
            args.append(str(filters["status"]))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._db() as db:
            db.execute("BEGIN")
            total = db.execute("SELECT COUNT(*) FROM orders" + where, args).fetchone()[0]
            rows = db.execute("SELECT order_id FROM orders" + where + " ORDER BY updated_at DESC, order_id LIMIT ? OFFSET ?",
                              args + [limit, offset]).fetchall()
            records = [summary(self._load(db, row[0])) for row in rows]
        return {"orders": records, "total": total, "limit": limit, "offset": offset, "storage": self.metadata()}

    def delete_order(self, order_id: str, expected_revision: int) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._load(db, order_id)
            if record is None:
                raise KeyError(order_id)
            if record["revision"] != expected_revision:
                raise StoreConflictError(expected_revision, record["revision"])
            if record["status"] == "archived":
                return record
            record["state"]["orderStatus"] = "archived"
            return self._save(db, record, expected_revision)

    def purge_order(self, order_id: str, expected_revision: int) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._load(db, order_id)
            if record is None:
                raise KeyError(order_id)
            if expected_revision != record["revision"]:
                raise StoreConflictError(expected_revision, record["revision"])
            if record["status"] != "archived":
                raise ValueError("only archived orders can be permanently deleted")
            db.execute("DELETE FROM order_events WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM messages WHERE session_id = ?", (record["sessionId"],))
            db.execute("DELETE FROM quote_requests WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM audit_log WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM legacy_session_backups WHERE session_id = ?", (record["sessionId"],))
            db.execute("DELETE FROM sessions WHERE id = ?", (record["sessionId"],))
            db.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
            return {"orderId": order_id, "deleted": True}

    def export_order(self, order_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._load(db, order_id)
            if record is None:
                return None
            self._insert_audit(db, order_id, record["revision"], "exported", {}, "user")
            messages = [{"messageId": r["message_id"], "role": r["role"], "text": r["content"], "createdAt": r["created_at"]}
                        for r in db.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY sequence", (record["sessionId"],))]
            quotes = [json.loads(r[0]) for r in db.execute("SELECT request_json FROM quote_requests WHERE order_id = ? ORDER BY rowid", (order_id,))]
            audits = [{"auditId": r["audit_id"], "revision": r["revision"], "action": r["action"],
                       "payload": json.loads(r["payload_json"]), "actor": r["actor"], "createdAt": r["created_at"]}
                      for r in db.execute("SELECT * FROM audit_log WHERE order_id = ? ORDER BY rowid", (order_id,))]
            return {**record, "schemaVersion": "2.0", "exportedAt": timestamp(), "events": self._events(db, order_id),
                    "messages": messages, "quoteRequests": quotes, "auditLog": audits}
