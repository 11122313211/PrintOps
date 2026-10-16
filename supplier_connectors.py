"""Trusted supplier registry, offline connectors, and a guarded transport seam.

This module deliberately contains no HTTP client, URLs, credential discovery or
real supplier implementation. A deployment may register reviewed Python objects
at startup. Live effects remain disabled unless every guard is explicitly met.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from queue import Empty, Queue
import sqlite3
from threading import BoundedSemaphore, Event, RLock, Thread
import time
from typing import Any, Mapping
from uuid import uuid4

from supplier_adapters import (ADAPTERS, DEFAULT_FIELD_MAP, PLATFORMS, SupplierAdapter,
                               canonical_export_order, platform_metadata, invalid_export_dimensions)
from supplier_contract import (CONTRACT_VERSION, MODES, OPERATIONS, HumanConfirmation,
                               RequestContext, RequestJournal, SupplierConnector,
                               SupplierTransport, TransportPolicy, UnknownProviderError,
                               order_fingerprint, utc_now)


class MemoryRequestJournal:
    """Atomic process-local journal for offline and sandbox testing only."""
    durable = False

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.lock = RLock()

    def claim(self, key: str, fingerprint: str, record: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        with self.lock:
            if key in self.records:
                return False, deepcopy(self.records[key])
            saved = {**deepcopy(record), "fingerprint": fingerprint}
            self.records[key] = saved
            return True, deepcopy(saved)

    def finish(self, key: str, result: dict[str, Any]) -> None:
        with self.lock:
            self.records[key]["result"] = deepcopy(result)

    def get(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            return deepcopy(self.records.get(key))

    def append_audit(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.events.append(deepcopy(event))


class SQLiteRequestJournal:
    """Durable idempotency and audit implementation; injectable with enterprise stores.

    Claims use SQLite's unique key under BEGIN IMMEDIATE. An unfinished claim
    after a crash is returned as unknown; a potentially accepted submission is
    never silently sent again after restart.
    """
    durable = True

    def __init__(self, path: str) -> None:
        if path == ":memory:":
            raise ValueError("Use MemoryRequestJournal for transient storage")
        self.path = str(path)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS supplier_requests (
                    request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    record_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supplier_audit (
                    event_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, event_json TEXT NOT NULL
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def claim(self, key: str, fingerprint: str, record: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,record_json,result_json FROM supplier_requests WHERE request_key=?", (key,)).fetchone()
            if row:
                value = {**json.loads(row[1]), "fingerprint": row[0]}
                if row[2]:
                    value["result"] = json.loads(row[2])
                return False, value
            db.execute("INSERT INTO supplier_requests VALUES (?,?,?,?,?)",
                       (key, fingerprint, json.dumps(record, ensure_ascii=False), None, utc_now()))
            return True, {**deepcopy(record), "fingerprint": fingerprint}

    def finish(self, key: str, result: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("UPDATE supplier_requests SET result_json=? WHERE request_key=?",
                       (json.dumps(result, ensure_ascii=False), key))

    def get(self, key: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT fingerprint,record_json,result_json FROM supplier_requests WHERE request_key=?", (key,)).fetchone()
        if not row:
            return None
        value = {**json.loads(row[1]), "fingerprint": row[0]}
        if row[2]:
            value["result"] = json.loads(row[2])
        return value

    def append_audit(self, event: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO supplier_audit VALUES (?,?,?)", (uuid4().hex, utc_now(), json.dumps(event, ensure_ascii=False)))


class ExportConnector:
    """Offline drafting works whether or not any supplier has authorized data."""
    def __init__(self, adapter: SupplierAdapter, metadata: Mapping[str, Any] | None = None) -> None:
        self.adapter = adapter
        self._metadata = deepcopy(dict(metadata or platform_metadata(adapter.platform_id)))

    def metadata(self) -> dict[str, Any]:
        return deepcopy(self._metadata)

    def health(self) -> dict[str, Any]:
        return {"providerId": self.adapter.platform_id, "status": "offline_ready",
                "connectionStatus": self._metadata["connectionStatus"], "networkEnabled": False}

    def capabilities(self) -> dict[str, Any]:
        return {"providerId": self.adapter.platform_id, "operations": list(self._metadata["capabilities"]),
                "production": deepcopy(self._metadata.get("supplierProfile", {})),
                "verified": bool(self._metadata.get("verified")),
                "source": deepcopy(self._metadata["source"]), "updatedAt": self._metadata.get("updatedAt"),
                "expiresAt": self._metadata.get("expiresAt")}

    def _unsupported(self, operation: str) -> dict[str, Any]:
        return {"status": "unsupported", "reason": "operation_not_supported", "operation": operation,
                "providerId": self.adapter.platform_id, "mode": self._metadata["mode"],
                "sent": False, "requiresHumanConfirmation": True}

    def create_quote_draft(self, order: Mapping[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        invalid = invalid_export_dimensions(order)
        if invalid:
            return {"status": "blocked", "reason": "invalid_measurement", "invalidFields": invalid,
                    "providerId": self.adapter.platform_id, "sent": False, "requiresHumanConfirmation": True}
        return {"requestId": f"quote-{uuid4().hex}", "type": "quote_draft",
                "status": "awaiting_human_confirmation", "providerId": self.adapter.platform_id,
                "platformId": self.adapter.platform_id, "platform": self.adapter.name,
                "mappedOrder": self.adapter.map_order(order), "unitSystem": "mm",
                "adapterMode": self._metadata["mode"], "mode": self._metadata["mode"],
                "connectionStatus": self._metadata["connectionStatus"], "capabilityStatus": "review",
                "source": deepcopy(self._metadata["source"]), "updatedAt": None, "expiresAt": None,
                "verified": False, "sent": False, "networkEnabled": False,
                "requiresHumanConfirmation": True,
                "message": "已生成本地询价草稿，尚未发送。供应商生产能力、价格和交期需另行确认。",
                "orderRevision": (context or RequestContext()).revision,
                "orderFingerprint": order_fingerprint(order)}

    def create_order_draft(self, order: Mapping[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        return {**self.create_quote_draft(order, context), "requestId": f"draft-{uuid4().hex}", "type": "order_draft"}

    def get_quote(self, request_id: str, context: RequestContext | None = None) -> dict[str, Any]:
        return self._unsupported("get_quote")

    def submit_order(self, order: Mapping[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        return self._unsupported("submit_order")

    def cancel(self, request_id: str, context: RequestContext | None = None) -> dict[str, Any]:
        return self._unsupported("cancel")


class MockConnector(ExportConnector):
    """In-memory sandbox fixture; results always identify themselves as mock."""
    def __init__(self, provider_id: str = "mock") -> None:
        adapter = SupplierAdapter(provider_id, "本地模拟供应商", field_map={"*": DEFAULT_FIELD_MAP})
        super().__init__(adapter, {
            "providerId": provider_id, "name": adapter.name, "displayName": adapter.name,
            "mode": "sandbox", "connectionStatus": "local", "networkEnabled": False,
            "contractVersion": CONTRACT_VERSION, "unitSystem": "mm", "authType": "none",
            "capabilities": ["quote_draft", "order_draft", "get_quote", "cancel"], "verified": False,
            "source": {"kind": "mock", "description": "本地模拟响应，不代表供应商数据"},
            "updatedAt": None, "expiresAt": None, "supplierProfile": {}, "requiresHumanConfirmation": True,
        })
        self.requests: dict[str, dict[str, Any]] = {}
        self.lock = RLock()

    def create_quote_draft(self, order: Mapping[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        # The adapter helper reads bundled metadata; mock has its own metadata.
        invalid = invalid_export_dimensions(order)
        if invalid:
            return {"status": "blocked", "reason": "invalid_measurement", "invalidFields": invalid,
                    "providerId": self.adapter.platform_id, "sent": False, "mock": True}
        result = {"requestId": f"quote-{uuid4().hex}", "status": "awaiting_human_confirmation",
                  "type": "quote_draft", "providerId": self.adapter.platform_id, "platformId": self.adapter.platform_id,
                  "platform": self.adapter.name, "mode": "sandbox", "adapterMode": "sandbox",
                  "connectionStatus": "local", "mappedOrder": self.adapter.map_order(order),
                  "source": self._metadata["source"], "verified": False, "mock": True, "sent": False,
                  "networkEnabled": False, "updatedAt": utc_now(), "expiresAt": None,
                  "requiresHumanConfirmation": True, "orderFingerprint": order_fingerprint(order),
                  "orderRevision": (context or RequestContext()).revision}
        with self.lock:
            self.requests[result["requestId"]] = deepcopy(result)
        return result

    def get_quote(self, request_id: str, context: RequestContext | None = None) -> dict[str, Any]:
        with self.lock:
            return deepcopy(self.requests.get(request_id) or {"status": "not_found", "mock": True, "sent": False})

    def cancel(self, request_id: str, context: RequestContext | None = None) -> dict[str, Any]:
        with self.lock:
            if request_id not in self.requests:
                return {"status": "not_found", "mock": True, "sent": False}
            result = self.requests[request_id]
            repeated = result["status"] == "cancelled"
            result["status"] = "cancelled"
            return {**deepcopy(result), "idempotent": repeated}


class GuardedTransportConnector(ExportConnector):
    """Optional seam for reviewed integrations. No real transport is bundled.

    Reads have bounded retries. Writes have durable idempotency and are never
    automatically retried: transport timeout can mean accepted-but-unobserved.
    A bounded number of daemon workers isolates transports that ignore timeout;
    cancellation is cooperative and never implies remote rollback.
    """
    def __init__(self, adapter: SupplierAdapter, metadata: Mapping[str, Any], transport: SupplierTransport,
                 *, journal: RequestJournal | None = None, policy: TransportPolicy | None = None,
                 live_enabled: bool = False) -> None:
        super().__init__(adapter, metadata)
        if self._metadata.get("mode") not in {"sandbox", "live"}:
            raise ValueError("Transport connectors require sandbox or live mode")
        self.transport = transport
        self.journal = journal or MemoryRequestJournal()
        self.policy = policy or TransportPolicy()
        self.live_enabled = live_enabled
        self._slots = BoundedSemaphore(self.policy.max_in_flight)
        self._pending: dict[str, Event] = {}
        self._lock = RLock()

    def health(self) -> dict[str, Any]:
        # This is readiness configuration, not an unsolicited network probe.
        return {"providerId": self.adapter.platform_id,
                "status": "configured" if self._metadata.get("healthVerified") else "unverified",
                "connectionStatus": self._metadata.get("connectionStatus", "unconfigured"),
                "networkEnabled": bool(self._metadata.get("networkEnabled")) and
                                  (self._metadata["mode"] != "live" or self.live_enabled)}

    def _blocked(self, reason: str) -> dict[str, Any]:
        return {"status": "blocked", "reason": reason, "providerId": self.adapter.platform_id,
                "sent": False, "requiresHumanConfirmation": True}

    def _approval_valid(self, confirmation: HumanConfirmation | None, revision: int,
                        fingerprint: str, operation: str, request_id: str | None) -> bool:
        if not isinstance(confirmation, HumanConfirmation):
            return False
        try:
            confirmed = datetime.fromisoformat(confirmation.confirmed_at)
            expires = datetime.fromisoformat(confirmation.expires_at)
            now = datetime.now(timezone.utc)
            return bool(confirmation.actor and confirmation.provider_id == self.adapter.platform_id
                        and confirmation.order_revision == revision and confirmation.order_fingerprint == fingerprint
                        and confirmation.action == operation and confirmation.request_id == request_id
                        and confirmed <= now < expires)
        except (TypeError, ValueError):
            return False

    def _precheck(self, operation: str, context: RequestContext, fingerprint: str,
                  request_id: str | None = None) -> dict[str, Any] | None:
        if operation not in self._metadata.get("capabilities", []):
            return self._unsupported(operation)
        if not self._metadata.get("networkEnabled") or self._metadata.get("connectionStatus") != "connected":
            return self._blocked("connector_unconfigured")
        if self._metadata["mode"] == "live":
            if not self.live_enabled:
                return self._blocked("live_disabled")
            if not all(self._metadata.get(key) for key in ("credentialsConfigured", "healthVerified", "contractVerified")):
                return self._blocked("integration_not_verified")
            if not self.journal.durable:
                return self._blocked("durable_journal_required")
            if operation in {"submit_order", "cancel"} and not self._approval_valid(
                    context.confirmation, context.revision, fingerprint, operation, request_id):
                return self._blocked("human_confirmation_required")
        return None

    def _once(self, operation: str, payload: Mapping[str, Any], key: str | None,
              timeout: float, cancel_event: Event) -> dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            return {"status": "busy", "sent": False, "reason": "transport_capacity"}
        queue: Queue[Any] = Queue(maxsize=1)
        def run() -> None:
            try:
                queue.put((True, self.transport.execute(operation, deepcopy(payload), timeout=timeout,
                                                       idempotency_key=key, cancel_event=cancel_event)))
            except Exception as exc:
                queue.put((False, exc))
            finally:
                self._slots.release()
        Thread(target=run, daemon=True, name="printops-supplier").start()
        until = time.monotonic() + timeout
        while True:
            remaining = until - time.monotonic()
            if remaining <= 0 or cancel_event.is_set():
                reason = "wait_cancelled" if cancel_event.is_set() else "transport_timeout"
                cancel_event.set()
                return {"status": "unknown" if operation != "get_quote" else
                        ("cancelled" if reason == "wait_cancelled" else "timeout"), "sent": None,
                        "reason": reason, "requiresReconciliation": operation != "get_quote"}
            try:
                ok, result = queue.get(timeout=min(0.05, remaining))
                break
            except Empty:
                continue
        if not ok:
            if isinstance(result, TimeoutError):
                return {"status": "unknown" if operation != "get_quote" else "timeout", "sent": None,
                        "reason": "transport_timeout", "requiresReconciliation": operation != "get_quote"}
            return {"status": "unknown" if operation != "get_quote" else "failed", "sent": None,
                    "reason": "transport_error", "requiresReconciliation": operation != "get_quote"}
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            return {"status": "unknown" if operation != "get_quote" else "failed", "sent": None,
                    "reason": "invalid_transport_response", "requiresReconciliation": operation != "get_quote"}
        return deepcopy(result)

    def _execute(self, operation: str, payload: Mapping[str, Any], context: RequestContext,
                 fingerprint: str, request_id: str | None = None) -> dict[str, Any]:
        blocked = self._precheck(operation, context, fingerprint, request_id)
        if blocked:
            return blocked
        key = context.idempotency_key
        write = operation != "get_quote"
        if write and (not isinstance(key, str) or not key.strip() or len(key) > 256):
            return self._blocked("idempotency_key_required")
        journal_key = f"{self.adapter.platform_id}:{operation}:{key}"
        audit = {"providerId": self.adapter.platform_id, "operation": operation,
                 "orderFingerprint": fingerprint, "orderRevision": context.revision,
                 "idempotencyKey": key, "at": utc_now()}
        try:
            if write:
                claimed, prior = self.journal.claim(journal_key, fingerprint,
                    {"operation": operation, "revision": context.revision, "requestId": request_id})
                if not claimed:
                    if (prior["fingerprint"] != fingerprint or prior["revision"] != context.revision
                            or prior.get("requestId") != request_id):
                        return self._blocked("idempotency_conflict")
                    return {**prior.get("result", {"status": "unknown", "sent": None,
                            "requiresReconciliation": True}), "idempotent": True}
            self.journal.append_audit({**audit, "event": "started"})
        except Exception:
            return self._blocked("audit_unavailable")
        cancel_event = Event()
        tracking = journal_key if write or key else f"read:{uuid4().hex}"
        with self._lock:
            self._pending[tracking] = cancel_event
        deadline = time.monotonic() + self.policy.deadline_seconds
        result: dict[str, Any] = {"status": "timeout", "sent": False}
        try:
            attempts = self.policy.max_attempts if operation == "get_quote" else 1
            for attempt in range(attempts):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or cancel_event.is_set():
                    break
                result = self._once(operation, payload, key, min(self.policy.timeout_seconds, remaining), cancel_event)
                result["attempts"] = attempt + 1
                if result.get("status") not in {"timeout", "retryable_error"} or cancel_event.is_set():
                    break
            result.update({"providerId": self.adapter.platform_id, "mode": self._metadata["mode"],
                           "source": deepcopy(self._metadata["source"]), "updatedAt": utc_now(),
                           "verified": bool(self._metadata.get("verified"))})
            if result.get("requestId") and operation == "submit_order":
                self.journal.claim(f"request:{self.adapter.platform_id}:{result['requestId']}", fingerprint,
                                   {"revision": context.revision, "submissionKey": journal_key})
            if write:
                self.journal.finish(journal_key, result)
            self.journal.append_audit({**audit, "event": "finished", "status": result["status"],
                                       "attempts": result.get("attempts", 0)})
            return result
        except Exception:
            # An audit failure after a send cannot be represented as a safe retry.
            return {"status": "unknown", "reason": "journal_failure_after_dispatch", "sent": None,
                    "requiresReconciliation": True, "providerId": self.adapter.platform_id}
        finally:
            with self._lock:
                self._pending.pop(tracking, None)

    def get_quote(self, request_id: str, context: RequestContext | None = None) -> dict[str, Any]:
        return self._execute("get_quote", {"requestId": request_id}, context or RequestContext(), request_id)

    def submit_order(self, order: Mapping[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        invalid = invalid_export_dimensions(order)
        if invalid:
            return {**self._blocked("invalid_measurement"), "invalidFields": invalid}
        return self._execute("submit_order", {"order": self.adapter.map_order(order)},
                             context or RequestContext(), order_fingerprint(order))

    def cancel(self, request_id: str, context: RequestContext | None = None) -> dict[str, Any]:
        context = context or RequestContext()
        original = self.journal.get(f"request:{self.adapter.platform_id}:{request_id}")
        if not original:
            return self._blocked("unknown_request")
        if original["revision"] != context.revision:
            return self._blocked("stale_revision")
        return self._execute("cancel", {"requestId": request_id}, context, original["fingerprint"], request_id)

    def cancel_wait(self, idempotency_key: str, operation: str = "submit_order") -> bool:
        """Stop waiting cooperatively; remote cancellation is a separate operation."""
        with self._lock:
            event = self._pending.get(f"{self.adapter.platform_id}:{operation}:{idempotency_key}")
            if event:
                event.set()
            return event is not None


class ConnectorRegistry:
    def __init__(self, *, publish_legacy_view: bool = False) -> None:
        self._connectors: dict[str, SupplierConnector] = {}
        self._lock = RLock()
        self._publish = publish_legacy_view

    def register(self, connector: SupplierConnector, *, replace: bool = False) -> None:
        if not isinstance(connector, SupplierConnector):
            raise TypeError("Connector must implement the complete SupplierConnector protocol")
        metadata = connector.metadata()
        provider_id = metadata.get("providerId")
        if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 128:
            raise ValueError("Invalid provider ID")
        if metadata.get("mode") not in MODES or metadata.get("unitSystem") != "mm":
            raise ValueError("Connector mode and mm units must be declared")
        if metadata.get("contractVersion") != CONTRACT_VERSION:
            raise ValueError("Unsupported supplier contract version")
        if not set(metadata.get("capabilities", [])).issubset(OPERATIONS):
            raise ValueError("Unknown connector operation")
        if metadata["mode"] == "export_only" and metadata.get("networkEnabled"):
            raise ValueError("Export-only connectors cannot enable network access")
        if not isinstance(metadata.get("source"), dict):
            raise ValueError("Connector data provenance is required")
        with self._lock:
            if provider_id in self._connectors and not replace:
                raise ValueError(f"Provider already registered: {provider_id}")
            self._connectors[provider_id] = connector
            if self._publish:
                PLATFORMS[provider_id] = deepcopy(metadata)
                if isinstance(getattr(connector, "adapter", None), SupplierAdapter):
                    ADAPTERS[provider_id] = connector.adapter

    def get(self, provider_id: str | None = None) -> SupplierConnector:
        selected = provider_id or "generic"
        with self._lock:
            if selected not in self._connectors:
                raise UnknownProviderError(f"Unknown supplier provider: {selected}")
            return self._connectors[selected]

    def list_metadata(self) -> list[dict[str, Any]]:
        with self._lock:
            return [connector.metadata() for connector in self._connectors.values()]


REGISTRY = ConnectorRegistry(publish_legacy_view=True)
for _adapter in list(ADAPTERS.values()):
    REGISTRY.register(ExportConnector(_adapter))
REGISTRY.register(MockConnector())
