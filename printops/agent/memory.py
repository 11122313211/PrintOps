"""Session memory compatibility facade, revision conflicts, and shared agent constants."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from printops.domain.order_model import (
    ITEM_DEFAULTS,
    MAX_STATE_REVISION,
    ORDER_DEFAULTS,
    STATE_SCHEMA_VERSION,
    normalize_order_dimensions,
    normalize_order_items,
    normalize_state,
)
from printops.storage.contract import OrderStore, StoreConflictError
from printops.storage.factory import create_store

HISTORY_LIMIT = 80
# One initial plan plus up to three bounded local tool turns.  The model never
# receives an unbounded chain; every turn still passes through call_tool().
MAX_PLANNER_TOOL_ROUNDS = 3
# A provider can spend its own three tool turns on a poor plan.  Explicit user
# obligations still get a separate, bounded local recovery budget so a wrong
# or malformed model call cannot suppress a required deterministic tool.
MAX_PLANNER_REQUIRED_CALLS = 5
# Tool rounds and the final synthesis turn are separate budgets.  A bounded
# ledger lets a provider re-plan from the whole run without retaining an
# unbounded transcript or leaking raw provider reasoning.
MAX_PLANNER_LEDGER_ENTRIES = 8
MAX_PLANNER_LEDGER_BYTES = 48 * 1024
MAX_RUN_EVENTS = 64
MAX_RUN_HISTORY = 20
MAX_QUOTE_REQUESTS = 40
QUOTE_ACTIVE_STATUSES = {"awaiting_human_confirmation", "confirmed"}
# Planner tool receipts carrying one of these statuses failed outright; a
# deterministic retry with canonical local arguments may still succeed.
PLANNER_RETRYABLE_FAILURE_STATUSES = {"error", "invalid_arguments", "duplicate_blocked"}
# Receipts/result statuses that must never read as a grounded success, including
# the deterministic workflow blocks.
PLANNER_FAILURE_STATUSES = PLANNER_RETRYABLE_FAILURE_STATUSES | {"blocked"}
# ``call_tool`` is also used by the local HTTP bridge, so keep a small
# provider-independent boundary here instead of relying only on MCP schemas.
MAX_TOOL_NAME_LENGTH = 128
MAX_TOOL_FILE_NAME_LENGTH = 255
MAX_TOOL_INSPECTION_BYTES = 16 * 1024
MAX_TOOL_ORDER_BYTES = 64 * 1024
MAX_TOOL_SAFE_INTEGER = 2**53 - 1
MAX_PATCH_VALUE = 4096
WORKFLOW_LABELS = {
    "collect": "需求收集", "clarify": "品类澄清", "recommend": "方案选择",
    "preflight": "文件预检", "quote": "报价准备", "confirm": "订单确认",
    "export": "导出交接",
}
FIELD_SOURCE_LABELS = {
    "user": "用户输入", "rule": "规则识别", "model": "模型推断",
    "recommendation": "方案带入", "system": "系统默认",
}
# Keys a patch may touch on an order item; identity and delivery state are
# managed by the workflow, never written from user or model patches.
ITEM_PATCH_KEYS = {key for key in ITEM_DEFAULTS
                   if key not in {"itemId", "selectedOption", "orderGenerated"}}
ORDER_PATCH_KEYS = {key for key in ORDER_DEFAULTS if key not in {"productTypes", "items"}}


class Memory:
    """Compatibility facade over an injected or configured OrderStore.

    session_id == order_id in contract v1. Callers may keep load/save while new
    integrations use ``memory.store`` for paginated history and export.
    """
    BUSY_TIMEOUT_MS = 5000

    def __init__(self, path: str | Path = "data/agent.sqlite3", *, store: OrderStore | None = None,
                 mode: str | None = None) -> None:
        self.path = Path(path)
        self.store = store if store is not None else create_store(mode, self.path)

    def load(self, session_id: str) -> dict[str, Any]:
        record = self.store.load_order(session_id)
        return normalize_state(deepcopy(record["state"])) if record else self.fresh_state()

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        """Legacy fixture/import API. Agent mutations use save_if_revision."""
        existing = self.store.load_order(session_id)
        normalized = normalize_state(deepcopy(state))
        record = {"orderId": session_id, "sessionId": session_id, "state": normalized}
        if state != normalized:
            record["events"] = [{"eventType": "legacy_state_normalized", "actor": "migration",
                                 "payload": {"original": deepcopy(state)}}]
        saved = self.store.save_order(record, existing["revision"] if existing else 0)
        state["revision"] = saved["revision"]

    def save_if_revision(self, session_id: str, state: dict[str, Any], expected_revision: int,
                         *, new_messages: list[dict[str, Any]] | None = None,
                         events: list[dict[str, Any]] | None = None) -> tuple[bool, int | None]:
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or not 0 <= expected_revision <= MAX_STATE_REVISION):
            return False, None
        normalized = normalize_state(deepcopy(state))
        record = {"orderId": session_id, "sessionId": session_id, "state": normalized}
        if new_messages is not None:
            record["newMessages"] = new_messages
        if events:
            record["events"] = events
        try:
            saved = self.store.save_order(record, expected_revision)
        except StoreConflictError as error:
            return False, error.current
        state.clear()
        state.update(normalized)
        state["revision"] = saved["revision"]
        return True, saved["revision"]

    def _db(self):
        """Legacy SQLite inspection seam; not part of the backend contract."""
        if not hasattr(self.store, "_db"):
            raise TypeError("the selected store is not SQLite")
        return self.store._db()

    @staticmethod
    def fresh_state() -> dict[str, Any]:
        order = deepcopy(ORDER_DEFAULTS)
        normalize_order_dimensions(order)
        normalize_order_items(order)
        return {"order": order, "messages": [], "stage": "collect",
                "selectedOption": None, "orderGenerated": False, "uploadedFile": None,
                "uploadedFiles": [],
                "handoff": None, "confirmation": {"status": "not_ready"},
                "fieldMeta": {}, "conflicts": [], "lastRun": None, "runHistory": [],
                "rejectedFields": [],
                "toolReceipts": [],
                "planMeta": {},
                "workflowStage": "collect", "activeItemIndex": None, "itemOptions": {},
                "processOptions": [], "preflightResults": {},
                "quoteRequests": [], "activeQuoteRequestId": None,
                "schemaVersion": STATE_SCHEMA_VERSION, "revision": 0}


class RevisionConflictError(RuntimeError):
    """Raised when an optimistic session write loses a cross-process race."""

    def __init__(self, expected: int, current: int | None) -> None:
        super().__init__("session revision changed")
        self.expected = expected
        self.current = current

