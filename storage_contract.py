"""Backend-neutral persistence contract. One session currently owns one order."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Protocol, runtime_checkable

STORE_CONTRACT_VERSION = "1.0"
MAX_REVISION = 2**53 - 1
ORDER_STATUSES = frozenset({"draft", "collecting", "ready_for_review", "confirmed",
                          "handoff_ready", "quote_draft", "quote_received", "cancelled", "archived"})

# Tool-call telemetry (v1.3.0). Recording is an optional store capability, so
# it lives on its own protocol instead of widening the OrderStore structural
# check. The bundled SQLite and memory stores both implement it.
TOOL_CALL_STATUSES = ("ok", "failed", "blocked", "rejected")
TOOL_CALL_LOG_LIMIT = 1000
MAX_TOOL_NAME_LOG_LENGTH = 64
MAX_TOOL_REASON_LENGTH = 128
TOOL_STATS_SCOPE = "local-only"


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


@runtime_checkable
class ToolCallRecorder(Protocol):
    """Optional telemetry capability of an OrderStore (v1.3.0).

    record_tool_call appends one gateway tool-call observation; entry fields
    mirror the tool_call_log columns: tool_name, status (one of
    TOOL_CALL_STATUSES), reason, duration_ms, session_id, run_id, item_index
    and created_at (ISO timestamp; implementations fill a missing value).
    Implementations must normalize malformed fields instead of raising —
    telemetry must never break the order workflow — and may cap how many rows
    are retained (the memory backend keeps TOOL_CALL_LOG_LIMIT entries; the
    SQLite backend persists history for the local operator).

    load_tool_stats(window_days) aggregates the recorded calls whose
    created_at falls inside the trailing window (1..365 days, clamped) and
    returns a detached dict: totals with per-status counters, successRate
    (0-100), overall avg/p95 duration in ms, a per-tool breakdown with the
    same counters, and the blocked reason distribution. p95 uses the
    nearest-rank method over the sorted duration sample; an empty window
    reports zero counters with null latency. scope is always "local-only":
    the numbers never leave this machine's database.
    """
    def record_tool_call(self, entry: dict[str, Any]) -> None: ...
    def load_tool_stats(self, window_days: int) -> dict[str, Any]: ...


def normalize_tool_call_entry(entry: Any) -> dict[str, Any]:
    """Coerce one telemetry entry into the shared row shape; never raises."""
    source = entry if isinstance(entry, dict) else {}
    status = str(source.get("status") or "")
    reason = str(source.get("reason") or "")[:MAX_TOOL_REASON_LENGTH]
    duration = source.get("duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        duration = 0
    item_index = source.get("item_index")
    if isinstance(item_index, bool) or not isinstance(item_index, int):
        item_index = None
    return {"tool_name": str(source.get("tool_name") or "unknown")[:MAX_TOOL_NAME_LOG_LENGTH],
            "status": status if status in TOOL_CALL_STATUSES else "failed",
            "reason": reason,
            "duration_ms": int(duration),
            "session_id": str(source.get("session_id") or "")[:128],
            "run_id": str(source.get("run_id") or "")[:64],
            "item_index": item_index,
            "created_at": str(source.get("created_at") or timestamp())}


def tool_stats_window(window_days: Any) -> int:
    """Clamp the requested window to the supported 1..365 day range."""
    if isinstance(window_days, bool) or not isinstance(window_days, (int, float)) or window_days <= 0:
        raise ValueError("window_days must be a positive number of days")
    return max(1, min(365, int(window_days)))


def tool_stats_cutoff(window_days: Any) -> str:
    """Return the ISO cutoff created_at for the trailing window."""
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=tool_stats_window(window_days))).isoformat(timespec="microseconds")


def _latency(durations: list[int]) -> tuple[float | None, float | None]:
    if not durations:
        return None, None
    ordered = sorted(durations)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return round(sum(ordered) / len(ordered), 2), float(ordered[rank - 1])


def aggregate_tool_calls(rows: list[dict[str, Any]], window_days: Any) -> dict[str, Any]:
    """Aggregate normalized tool_call rows; shared by every backend so the
    in-memory ring and the SQLite table report identical statistics."""
    days = tool_stats_window(window_days)
    durations: list[int] = []
    counts = {"ok": 0, "failed": 0, "blocked": 0, "rejected": 0}
    blocked_reasons: dict[str, int] = {}
    per_tool: dict[str, dict[str, Any]] = {}

    def bucket(tool: str) -> dict[str, Any]:
        return per_tool.setdefault(tool, {"tool": tool, "calls": 0, "ok": 0, "failed": 0,
                                          "blocked": 0, "rejected": 0, "durations": []})

    for row in rows:
        status = str(row.get("status") or "")
        duration = row.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            duration = 0
        tool = bucket(str(row.get("tool_name") or "unknown"))
        tool["calls"] += 1
        tool["durations"].append(duration)
        if status in counts:
            counts[status] += 1
            tool[status] += 1
            if status == "blocked":
                reason = str(row.get("reason") or "")
                blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
        durations.append(duration)

    def summary(entry: dict[str, Any]) -> dict[str, Any]:
        avg, p95 = _latency(entry.pop("durations"))
        return entry | {"successRate": round(entry["ok"] / entry["calls"] * 100) if entry["calls"] else 0,
                        "avgMs": avg, "p95Ms": p95}

    tools = sorted((summary(entry) for entry in per_tool.values()),
                   key=lambda item: (-item["calls"], item["tool"]))
    total_avg, total_p95 = _latency(durations)
    calls = len(durations)
    return {"windowDays": days, "scope": TOOL_STATS_SCOPE,
            "totals": counts | {"calls": calls,
                                "successRate": round(counts["ok"] / calls * 100) if calls else 0,
                                "avgMs": total_avg, "p95Ms": total_p95},
            "tools": tools,
            "blockedReasons": [{"reason": reason, "count": count} for reason, count in
                               sorted(blocked_reasons.items(), key=lambda item: (-item[1], item[0]))]}


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
