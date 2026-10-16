"""Volatile OrderStore for tests and explicitly selected ephemeral deployments."""
from __future__ import annotations

from copy import deepcopy
import threading
import uuid
from typing import Any
from storage_contract import (STORE_CONTRACT_VERSION, StoreConflictError, change_event,
                              json_text, message_delta, prepare_record, summary, timestamp)


class InMemoryOrderStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._orders: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._messages: dict[str, list[dict[str, Any]]] = {}
        self._quotes: dict[str, dict[str, dict[str, Any]]] = {}
        self._audit: dict[str, list[dict[str, Any]]] = {}

    def metadata(self) -> dict[str, Any]:
        return {"mode": "memory", "persistent": False, "contractVersion": STORE_CONTRACT_VERSION,
                "oneOrderPerSession": True}

    def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            order_id = str(order.get("orderId") or order.get("sessionId") or "")
            if order_id in self._orders:
                raise StoreConflictError(0, self._orders[order_id]["revision"])
            return self.save_order(order, 0)

    def load_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._orders.get(order_id))

    def save_order(self, order: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        with self._lock:
            order_id = str(order.get("orderId") or order.get("sessionId") or "")
            previous = self._orders.get(order_id)
            record = prepare_record(order, previous, expected_revision)
            if previous == record:
                return record
            # Prepare detached projections first; malformed events cannot leave
            # a partially changed in-memory snapshot.
            events = deepcopy(self._events.get(order_id, []))
            for event in [change_event(record, previous), *order.get("events", [])]:
                json_text(event)
                events.append({"eventId": uuid.uuid4().hex, "revision": record["revision"],
                               "eventType": str(event.get("eventType") or "note"),
                               "payload": deepcopy(event.get("payload", {})),
                               "actor": str(event.get("actor") or "system"), "createdAt": timestamp()})
            messages = deepcopy(self._messages.get(order_id, []))
            incoming = order.get("newMessages")
            if incoming is None:
                incoming = message_delta(previous["state"].get("messages", []) if previous else [], record["state"].get("messages", []))
            for message in incoming:
                if isinstance(message, dict) and isinstance(message.get("text"), str):
                    messages.append({"messageId": uuid.uuid4().hex, "role": str(message.get("role", "user")),
                                     "text": message["text"], "createdAt": timestamp()})
            quotes = deepcopy(self._quotes.get(order_id, {}))
            for quote in record["state"].get("quoteRequests", []):
                if isinstance(quote, dict) and quote.get("requestId"):
                    quotes[str(quote["requestId"])] = deepcopy(quote)
            audits = deepcopy(self._audit.get(order_id, []))
            before, after = previous["state"] if previous else {}, record["state"]
            for key, action in (("confirmation", "confirmation_changed"), ("handoff", "handoff_changed"),
                                ("quoteRequests", "quote_requests_changed"), ("orderStatus", "status_changed")):
                if after.get(key) != before.get(key) and after.get(key):
                    audits.append({"auditId": uuid.uuid4().hex, "revision": record["revision"], "action": action,
                                   "payload": {"before": deepcopy(before.get(key)), "after": deepcopy(after.get(key))},
                                   "actor": "agent", "createdAt": timestamp()})
            record["state"]["messages"] = [{"role": m["role"], "text": m["text"]} for m in messages[-80:]]
            record["state"]["quoteRequests"] = deepcopy(list(quotes.values())[-40:])
            self._orders[order_id], self._events[order_id] = record, events
            self._messages[order_id], self._quotes[order_id], self._audit[order_id] = messages, quotes, audits
            return deepcopy(record)

    def list_orders(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = filters or {}
        limit, offset = max(1, min(100, int(filters.get("limit", 50)))), max(0, int(filters.get("offset", 0)))
        with self._lock:
            rows = [r for r in self._orders.values()
                    if (filters.get("includeArchived") or r["status"] != "archived")
                    and (not filters.get("status") or r["status"] == filters["status"])]
            rows.sort(key=lambda r: (r["updatedAt"], r["orderId"]), reverse=True)
            return {"orders": [summary(r) for r in rows[offset:offset + limit]], "total": len(rows),
                    "limit": limit, "offset": offset, "storage": self.metadata()}

    def append_event(self, order_id: str, event: dict[str, Any]) -> None:
        json_text(event)
        with self._lock:
            record = self._orders[order_id]
            self._events[order_id].append({"eventId": uuid.uuid4().hex, "revision": record["revision"],
                                          "eventType": str(event.get("eventType") or "note"),
                                          "payload": deepcopy(event.get("payload", {})),
                                          "actor": str(event.get("actor") or "system"), "createdAt": timestamp()})

    def load_events(self, order_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._events.get(order_id, []))

    def find_operation_receipt(self, order_id: str, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((deepcopy(event["payload"]) for event in reversed(self._events.get(order_id, []))
                         if event.get("eventType") == "operation_receipt" and
                         event.get("payload", {}).get("operationId") == operation_id), None)

    def delete_order(self, order_id: str, expected_revision: int) -> dict[str, Any]:
        with self._lock:
            record = deepcopy(self._orders[order_id])
            if expected_revision != record["revision"]:
                raise StoreConflictError(expected_revision, record["revision"])
            if record["status"] == "archived":
                return record
            record["state"]["orderStatus"] = "archived"
            return self.save_order(record, expected_revision)

    def purge_order(self, order_id: str, expected_revision: int) -> dict[str, Any]:
        with self._lock:
            record = self._orders.get(order_id)
            if record is None:
                raise KeyError(order_id)
            if expected_revision != record["revision"]:
                raise StoreConflictError(expected_revision, record["revision"])
            if record["status"] != "archived":
                raise ValueError("only archived orders can be permanently deleted")
            for collection in (self._orders, self._events, self._messages, self._quotes, self._audit):
                collection.pop(order_id, None)
            return {"orderId": order_id, "deleted": True}

    def export_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._orders.get(order_id)
            if record is None:
                return None
            self._audit[order_id].append({"auditId": uuid.uuid4().hex, "revision": record["revision"],
                                          "action": "exported", "payload": {}, "actor": "user", "createdAt": timestamp()})
            return deepcopy({**record, "schemaVersion": "2.0", "exportedAt": timestamp(),
                             "events": self._events[order_id], "messages": self._messages[order_id],
                             "quoteRequests": list(self._quotes[order_id].values()), "auditLog": self._audit[order_id]})
