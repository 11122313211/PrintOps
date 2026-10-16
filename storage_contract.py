"""Backend-neutral persistence contract. One session currently owns one order."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from typing import Any, Protocol, runtime_checkable

STORE_CONTRACT_VERSION = "1.0"
MAX_REVISION = 2**53 - 1
ORDER_STATUSES = frozenset({"draft", "collecting", "ready_for_review", "confirmed",
                          "handoff_ready", "quote_draft", "quote_received", "cancelled", "archived"})


class StoreConflictError(RuntimeError):
    def __init__(self, expected: int, current: int | None):
        super().__init__("order revision changed")
        self.expected, self.current = expected, current


@runtime_checkable
class OrderStore(Protocol):
    """Records contain orderId, sessionId, state, revision, status and timestamps.

    save_order atomically commits the snapshot, events, message delta, quote
    projection and audit entries. expected_revision is mandatory on updates.
    delete_order archives; purge_order is an explicit permanent deletion of an
    already archived record and its stored projections. Implementations must
    return detached values and raise StoreConflictError on a lost CAS.
    """
    def metadata(self) -> dict[str, Any]: ...
    def create_order(self, order: dict[str, Any]) -> dict[str, Any]: ...
    def load_order(self, order_id: str) -> dict[str, Any] | None: ...
    def save_order(self, order: dict[str, Any], expected_revision: int) -> dict[str, Any]: ...
    def list_orders(self, filters: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def append_event(self, order_id: str, event: dict[str, Any]) -> None: ...
    def load_events(self, order_id: str) -> list[dict[str, Any]]: ...
    def delete_order(self, order_id: str, expected_revision: int) -> dict[str, Any]: ...
    def purge_order(self, order_id: str, expected_revision: int) -> dict[str, Any]: ...
    def export_order(self, order_id: str) -> dict[str, Any] | None: ...


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def order_status(state: dict[str, Any]) -> str:
    explicit = state.get("orderStatus")
    if isinstance(explicit, str) and explicit in ORDER_STATUSES:
        return explicit
    confirmation = state.get("confirmation")
    if isinstance(confirmation, dict) and confirmation.get("status") == "confirmed":
        return "confirmed"
    if state.get("orderGenerated"):
        return "ready_for_review"
    if any(q.get("status") in {"awaiting_human_confirmation", "confirmed"}
           for q in state.get("quoteRequests", []) if isinstance(q, dict)):
        return "quote_draft"
    return "collecting" if any(state.get("order", {}).get(k) for k in ("productType", "quantity", "items")) else "draft"


def summary(record: dict[str, Any]) -> dict[str, Any]:
    order = deepcopy(record["state"].get("order") or {})
    title = " · ".join(str(order.get(key) or "") for key in ("productType", "quantity")).strip(" ·") or "订单草稿"
    return {key: deepcopy(record[key]) for key in ("orderId", "sessionId", "revision", "status", "createdAt", "updatedAt")} | {"title": title, "order": order}


def prepare_record(order: dict[str, Any], previous: dict[str, Any] | None,
                   expected_revision: int) -> dict[str, Any]:
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or not 0 <= expected_revision <= MAX_REVISION:
        raise ValueError("expected_revision must be a nonnegative integer")
    current = previous["revision"] if previous else 0
    if expected_revision != current:
        raise StoreConflictError(expected_revision, current)
    state = deepcopy(order.get("state"))
    if not isinstance(state, dict) or not isinstance(state.get("order", {}), dict):
        raise ValueError("state must contain an order object")
    state.setdefault("order", {})
    state.setdefault("messages", [])
    state.setdefault("quoteRequests", [])
    json_text(state)
    requested = state.get("revision", 0)
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
        requested = 0
    unchanged = previous is not None and state == previous["state"] and not order.get("events") and not order.get("newMessages")
    if unchanged:
        return deepcopy(previous)
    if previous and previous["status"] == "archived":
        raise ValueError("archived orders are read-only")
    revision = max(current + 1, requested) if previous else requested
    if revision > MAX_REVISION:
        raise ValueError("revision exceeds the supported range")
    state["revision"] = revision
    now = timestamp()
    order_id = str(order.get("orderId") or order.get("sessionId") or "")
    if not order_id:
        raise ValueError("orderId is required")
    if str(order.get("sessionId") or order_id) != order_id:
        raise ValueError("contract v1 requires sessionId == orderId")
    return {"orderId": order_id, "sessionId": str(order.get("sessionId") or order_id),
            "revision": revision, "status": order_status(state), "state": state,
            "createdAt": previous["createdAt"] if previous else order.get("createdAt") or now,
            "updatedAt": now}


def message_delta(previous: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compatibility for callers passing rolling windows; Agent supplies an explicit delta."""
    for overlap in range(min(len(previous), len(incoming)), 0, -1):
        if previous[-overlap:] == incoming[:overlap]:
            return deepcopy(incoming[overlap:])
    return deepcopy(incoming)


def change_event(record: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    before = previous["state"] if previous else {}
    after = record["state"]
    changed = {key: {"before": deepcopy(before.get(key)), "after": deepcopy(after.get(key))}
               for key in set(before) | set(after) if key != "revision" and before.get(key) != after.get(key)}
    return {"eventType": "order_created" if previous is None else "state_saved",
            "actor": "agent", "payload": {"changes": changed}}
