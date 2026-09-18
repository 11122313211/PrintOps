"""Small, dependency-free print order agent.

Flow: perceive -> remember -> plan -> call tools -> respond.
The contracts are intentionally compatible with a future LangGraph/FastAPI layer.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import math
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nlu import perceive
from order_model import (DIMENSION_DEFAULTS, ITEM_DEFAULTS, LABELS,
                         MATERIAL_SPEC_PRODUCTS, ORDER_DEFAULTS,
                         QUANTITY_UNIT_ALIASES,
                         DEFAULT_QUANTITY_UNITS, RECOMMENDATION_FIELDS, REQUIRED,
                         STATE_SCHEMA_VERSION, MAX_STATE_REVISION, _multi_product_info,
                         default_quantity_unit,
                         merge_dimension_patch, migrate_dimension_field_meta,
                         migrate_state_item_references, normalize_order_dimensions,
                         normalize_order_items,
                         normalize_state, parse_quantity,
                         quote_idempotency_key, required_order_keys)
from product_knowledge import (KNOWLEDGE_MANIFEST, KNOWLEDGE_VERSION,
                               known_product_spec_keys, parameter_state)
from supplier_adapters import (ADAPTERS, PLATFORMS, SUPPLIER_PROFILE_VERSION, SupplierAdapter,
                               get_adapter)
from tools import (TOOLS, TOOL_META, TOOL_SCHEMAS, estimate_price, explain_print_term,
                   match_supplier_capability, preflight_file, prepare_handoff,
                   recommend_processes, request_supplier_quote, validate_order)

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
    """SQLite-backed session memory; survives server restarts."""

    BUSY_TIMEOUT_MS = 5000

    def __init__(self, path: str | Path = "data/agent.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    def load(self, session_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return self.fresh_state()
        try:
            state = json.loads(row[0])
            if not isinstance(state, dict):
                raise ValueError("会话状态不是 JSON 对象")
            return normalize_state(state)
        except (TypeError, ValueError, AttributeError, KeyError, json.JSONDecodeError) as error:
            return self._quarantine(session_id, row[0], error)

    def _quarantine(self, session_id: str, raw: str, error: Exception) -> dict[str, Any]:
        """Self-heal a corrupted session: back it up, drop the row, start fresh."""
        try:
            backup_dir = self.path.parent / "corrupted"
            backup_dir.mkdir(parents=True, exist_ok=True)
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:64] or "session"
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            (backup_dir / f"{safe_id}-{stamp}.json").write_text(
                json.dumps({"sessionId": session_id, "error": str(error), "raw": raw},
                           ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        print(f"PrintOps: 会话 {session_id} 的持久化状态损坏，已备份到 data/corrupted/ 并重置该会话"
              f"（{error}）", file=sys.stderr)
        return self.fresh_state()

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        data = json.dumps(state, ensure_ascii=False)
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (session_id, data))

    def save_if_revision(self, session_id: str, state: dict[str, Any],
                         expected_revision: int) -> tuple[bool, int | None]:
        """Atomically persist a state only when its stored revision matches.

        MCP instances can live in separate processes, so the in-process
        session locks are insufficient for optimistic concurrency. SQLite's
        IMMEDIATE transaction serializes the read/compare/write sequence while
        keeping the existing sessions table/API unchanged.
        """
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError, OverflowError):
            return False, None
        if expected < 0 or expected > MAX_STATE_REVISION:
            return False, None
        try:
            data = json.dumps(state, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                if expected != 0:
                    return False, 0
                db.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, data))
                saved = state.get("revision") if isinstance(state, dict) else None
                return True, saved if isinstance(saved, int) else 0
            try:
                raw = json.loads(row[0])
                current = raw.get("revision", 0) if isinstance(raw, dict) else None
                if (not isinstance(current, int) or isinstance(current, bool)
                        or current < 0 or current > MAX_STATE_REVISION):
                    return False, None
            except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                return False, None
            if current != expected:
                return False, current
            db.execute("UPDATE sessions SET state = ? WHERE id = ?", (data, session_id))
            saved = state.get("revision") if isinstance(state, dict) else None
            return True, saved if isinstance(saved, int) else current

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

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=self.BUSY_TIMEOUT_MS / 1000)
        try:
            # WAL lets concurrent reads proceed during writes; busy_timeout keeps
            # concurrent writers from failing with "database is locked".
            db.execute("PRAGMA busy_timeout = 5000")
            db.execute("PRAGMA journal_mode = WAL")
            with db:
                yield db
        finally:
            db.close()


class RevisionConflictError(RuntimeError):
    """Raised when an optimistic session write loses a cross-process race."""

    def __init__(self, expected: int, current: int | None) -> None:
        super().__init__("session revision changed")
        self.expected = expected
        self.current = current


class Agent:
    # Keep the deterministic recovery path aligned with the provider prompt
    # and the legacy no-planner path.  The model is allowed to choose tools,
    # but these intent signals are the local safety net when it returns text
    # only.  Negative wording is handled by ``_planner_positive_intent``.
    PLANNER_INTENT_PATTERNS = {
        "quote": r"询价|问价|报个价|报一下价|报价(?!估算)|报价单|报价请求|供应商.{0,8}(?:报价|价格)",
        "price": r"多少钱|费用(?:估算|多少|是多少)?|价格(?:估算|多少|是多少)?|成本(?:估算|多少|是多少)?|估(?:个|一下|算)?价|报价估算",
        "capability": r"平台能做吗|平台支持吗|供应商支持吗|能力匹配|支持哪些|能不能(?:做|印)|可以(?:做|印)吗|能印吗|能否印刷|可(?:以)?生产吗|能生产吗",
        "validation": r"检查订单|校验订单|核对订单|订单(?:是否)?完整|信息(?:是否)?完整|还缺什么|缺什么参数|检查一下",
        "recommendation": r"方案|推荐|工艺|怎么印|如何印|怎么做",
        "handoff": r"交接单|交接草稿|订单草稿|生成订单|生成草稿|准备下单|下单",
    }

    def __init__(self, memory: Memory, session_id: str | None = None, planner: Any = None) -> None:
        self.memory, self.id, self.planner = memory, session_id or uuid.uuid4().hex[:12], planner
        self.state = memory.load(self.id)
        # ``rejectedFields`` was added after the original state schema; keep
        # older SQLite sessions readable without requiring a schema bump.
        if not isinstance(self.state.get("rejectedFields"), list):
            self.state["rejectedFields"] = []
        if not isinstance(self.state.get("planMeta"), dict):
            self.state["planMeta"] = {}
        if not isinstance(self.state.get("toolReceipts"), list):
            self.state["toolReceipts"] = []
        if not isinstance(self.state.get("processOptions"), (list, dict)):
            self.state["processOptions"] = deepcopy(self.state.get("itemOptions") or []) \
                if isinstance(self.state.get("itemOptions"), (list, dict)) else []
        if not isinstance(self.state.get("preflightResults"), (dict, list)):
            self.state["preflightResults"] = {}
        # The persisted revision advances by _save only when the state
        # (excluding the counter itself) changed. Keeping a canonical
        # snapshot prevents helper methods that save twice during one
        # operation from spuriously advancing the CAS token.
        raw_revision = self.state.get("revision")
        self.state["revision"] = (raw_revision if isinstance(raw_revision, int)
                                   and not isinstance(raw_revision, bool)
                                   and 0 <= raw_revision <= MAX_STATE_REVISION else 0)
        self._saved_state_digest = self._state_digest(self.state)
        self.trace: list[str] = []
        self.run_id = ""
        self.run_operation = ""
        self.run_events: list[dict[str, Any]] = []
        self._cas_expected_revision: int | None = None
        self._planner_tool_receipts: list[dict[str, Any]] = []
        # Planner tool calls are nested inside one chat run.  Public tool
        # calls still finalize their own run, while planner calls must leave
        # the outer run open so its trace contains every round.
        self._nested_tool_execution = False

    @staticmethod
    def _state_digest(state: dict[str, Any]) -> str | None:
        """Return a stable digest for revision tracking, excluding revision."""
        state_copy = deepcopy(state)
        state_copy.pop("revision", None)
        try:
            encoded = json.dumps(state_copy, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            # An unusual direct-caller state is treated as changed rather than
            # accidentally reusing a stale optimistic-concurrency token.
            return None
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _begin_run(self, operation: str) -> None:
        """Start a bounded, inspectable run while keeping the old trace API."""
        self.run_id = uuid.uuid4().hex[:16]
        self.run_operation = operation
        self.run_events = []
        self._planner_tool_receipts = []
        self._event("run", "started", f"开始{operation}")

    def _invalidate_delivery_state(self) -> None:
        """Invalidate generated handoff data after an order-affecting change."""
        self._mark_quote_requests_stale("订单字段、文件或目标平台发生变化")
        self.state["orderGenerated"] = False
        self.state["handoff"] = None
        self.state["confirmation"] = {"status": "not_ready"}

    def _store_process_options(self, options: Any, item_index: int | None = None) -> None:
        """Persist recommendation results in a shape that survives a restart."""
        if not isinstance(options, list):
            return
        clean = deepcopy(options[:16])
        items = self.state.get("order", {}).get("items")
        if isinstance(items, list) and len(items) > 1 and isinstance(item_index, int) \
                and 0 <= item_index < len(items) and isinstance(items[item_index], dict):
            item_id = items[item_index].get("itemId") or f"item-{item_index + 1}"
            process = self.state.get("processOptions")
            if not isinstance(process, dict):
                process = {}
            process[item_id] = clean
            self.state["processOptions"] = process
            self.state["itemOptions"] = deepcopy(process)
            return
        self.state["processOptions"] = clean
        # ``itemOptions`` is retained as a compatibility alias for old UI
        # clients; single-item callers historically expected an empty map.
        self.state["itemOptions"] = {}

    def _clear_process_options(self, item_index: int | None = None) -> None:
        """Drop cached recommendations invalidated by an order-field change."""
        items = self.state.get("order", {}).get("items")
        process = self.state.get("processOptions")
        if isinstance(items, list) and len(items) > 1 and isinstance(item_index, int) \
                and 0 <= item_index < len(items) and isinstance(items[item_index], dict):
            item_id = items[item_index].get("itemId") or f"item-{item_index + 1}"
            if isinstance(process, dict):
                process.pop(item_id, None)
            legacy = self.state.get("itemOptions")
            if isinstance(legacy, dict):
                legacy.pop(item_id, None)
            self.state["processOptions"] = process if isinstance(process, dict) else {}
            self.state["itemOptions"] = deepcopy(self.state["processOptions"])
            return
        self.state["processOptions"] = []
        self.state["itemOptions"] = {}

    def _mark_preflight_stale(self, item_index: int | None = None,
                              reason: str = "订单字段发生变化") -> None:
        """Mark cached file checks stale after fields affecting production change."""
        stored = self.state.get("preflightResults")
        now = self._timestamp()

        def mark(value: Any, index: int | None = None, item_id: str | None = None) -> None:
            if not isinstance(value, dict):
                return
            value["stale"] = True
            value["status"] = "stale"
            value["staleReason"] = reason[:512]
            value["updatedAt"] = now
            if index is not None:
                value["itemIndex"] = index
            if item_id:
                value["itemId"] = item_id[:128]

        items = self.state.get("order", {}).get("items")
        if isinstance(items, list) and len(items) > 1:
            if isinstance(stored, dict) and not any(key in stored for key in ("ok", "status", "fileName")):
                if isinstance(item_index, int) and 0 <= item_index < len(items):
                    item = items[item_index] if isinstance(items[item_index], dict) else {}
                    item_id = item.get("itemId") or f"item-{item_index + 1}"
                    mark(stored.get(item_id), item_index, item_id)
                else:
                    for index, item in enumerate(items):
                        if not isinstance(item, dict):
                            continue
                        item_id = item.get("itemId") or f"item-{index + 1}"
                        mark(stored.get(item_id), index, item_id)
            elif isinstance(stored, list):
                for entry in stored:
                    if not isinstance(entry, dict):
                        continue
                    entry_index = entry.get("itemIndex")
                    if item_index is None or entry_index == item_index:
                        mark(entry, entry_index if isinstance(entry_index, int) else None,
                             entry.get("itemId") if isinstance(entry.get("itemId"), str) else None)
            return
        if isinstance(stored, dict) and any(key in stored for key in ("ok", "status", "fileName")):
            mark(stored)

    def _store_preflight_result(self, result: Any, item_index: int | None = None) -> None:
        """Persist the metadata-only result of a local PDF preflight."""
        if not isinstance(result, dict):
            return
        stored_result = deepcopy(result)
        stored_result["stale"] = False
        stored_result["updatedAt"] = self._timestamp()
        items = self.state.get("order", {}).get("items")
        if isinstance(items, list) and len(items) > 1 and isinstance(item_index, int) \
                and 0 <= item_index < len(items) and isinstance(items[item_index], dict):
            item_id = items[item_index].get("itemId") or f"item-{item_index + 1}"
            stored_result["itemId"] = item_id
            stored_result["itemIndex"] = item_index
            current = self.state.get("preflightResults")
            if not isinstance(current, dict) or any(key in current for key in ("ok", "status", "fileName")):
                current = {}
            current[item_id] = stored_result
            self.state["preflightResults"] = current
        else:
            self.state["preflightResults"] = stored_result

    def _mark_quote_requests_stale(self, reason: str) -> None:
        """Mark pending quote requests stale so changed orders cannot be reused."""
        requests = self.state.get("quoteRequests")
        if not isinstance(requests, list):
            self.state["quoteRequests"] = []
            return
        now = self._timestamp()
        for request in requests:
            if not isinstance(request, dict) or request.get("status") not in QUOTE_ACTIVE_STATUSES:
                continue
            request["status"] = "stale"
            request["updatedAt"] = now
            request["staleAt"] = now
            request["staleReason"] = reason
        active_id = self.state.get("activeQuoteRequestId")
        if active_id and not any(request.get("requestId") == active_id and request.get("status") in QUOTE_ACTIVE_STATUSES
                                 for request in requests if isinstance(request, dict)):
            self.state["activeQuoteRequestId"] = None

    def _quote_request(self, request_id: str | None = None) -> dict[str, Any] | None:
        requests = self.state.get("quoteRequests")
        if not isinstance(requests, list):
            return None
        target = request_id or self.state.get("activeQuoteRequestId")
        if target:
            return next((item for item in requests if isinstance(item, dict) and item.get("requestId") == target), None)
        return next((item for item in reversed(requests) if isinstance(item, dict)), None)

    def _persist_quote_request(self, result: dict[str, Any], order: dict[str, Any],
                               platform_id: str, item_index: int | None = None) -> dict[str, Any]:
        """Persist a quote preparation result and reuse active identical requests."""
        if result.get("status") != "awaiting_human_confirmation":
            return result
        item_id = None
        if item_index is not None:
            item_id = str(order.get("itemId") or f"item-{item_index + 1}")
        key = quote_idempotency_key(order, platform_id, item_id)
        requests = self.state.setdefault("quoteRequests", [])
        if not isinstance(requests, list):
            requests = []
            self.state["quoteRequests"] = requests
        existing = next((item for item in reversed(requests)
                         if isinstance(item, dict) and item.get("idempotencyKey") == key
                         and item.get("status") in QUOTE_ACTIVE_STATUSES), None)
        if existing:
            reused = deepcopy(existing)
            reused["idempotent"] = True
            reused["message"] = "相同订单已经存在待确认询价请求，已复用原请求，不会重复提交。"
            return reused
        now = self._timestamp()
        request = deepcopy(result)
        request.update({
            "requestId": f"quote-{uuid.uuid4().hex[:16]}",
            "idempotencyKey": key,
            "status": "awaiting_human_confirmation",
            "platformId": platform_id,
            "itemId": item_id,
            "itemIndex": item_index,
            "orderFingerprint": key.removeprefix("quote:"),
            "createdAt": now,
            "updatedAt": now,
            "idempotent": False,
        })
        requests.append(request)
        self.state["quoteRequests"] = requests[-MAX_QUOTE_REQUESTS:]
        self.state["activeQuoteRequestId"] = request["requestId"]
        return deepcopy(request)

    def _bind_uploaded_file(self, file_name: str, item_index: int | None = None) -> None:
        """Bind a checked file to one product item, or to the single-order draft."""
        items = self.state["order"].get("items")
        if isinstance(items, list) and len(items) > 1:
            if item_index is None or not (0 <= item_index < len(items)):
                return
            item = items[item_index]
            item["uploadedFile"] = file_name
            item_id = item.get("itemId") or f"item-{item_index + 1}"
            files = [entry for entry in (self.state.get("uploadedFiles") or [])
                     if isinstance(entry, dict) and entry.get("itemId") != item_id]
            files.append({"itemId": item_id, "itemIndex": item_index, "fileName": file_name})
            self.state["uploadedFiles"] = files
            # Keep the legacy field useful for the currently focused item.
            self.state["uploadedFile"] = file_name
            return
        self.state["uploadedFile"] = file_name
        self.state["uploadedFiles"] = [{"itemId": None, "itemIndex": None, "fileName": file_name}]

    def _event(self, step: str, status: str = "ok", detail: str = "", **extra: Any) -> None:
        if not self.run_id:
            return
        event = {"step": step, "status": status, "detail": detail, "at": self._timestamp()}
        event.update({key: value for key, value in extra.items() if value is not None})
        self.run_events.append(event)
        if len(self.run_events) > MAX_RUN_EVENTS:
            self.run_events = self.run_events[-MAX_RUN_EVENTS:]

    def _finish_run(self, status: str = "completed") -> dict[str, Any] | None:
        if not self.run_id:
            return self.state.get("lastRun")
        self._event("run", status, "运行完成" if status == "completed" else "运行结束")
        record = {"runId": self.run_id, "operation": self.run_operation, "status": status,
                  "startedAt": self.run_events[0]["at"] if self.run_events else self._timestamp(),
                  "finishedAt": self._timestamp(), "events": deepcopy(self.run_events)}
        history = list(self.state.get("runHistory") or [])
        history.append(record)
        self.state["runHistory"] = history[-MAX_RUN_HISTORY:]
        self.state["lastRun"] = record
        self.run_id = ""
        self.run_operation = ""
        self.run_events = []
        return record

    def _item_index(self, value: Any = None) -> int | None:
        """Resolve an explicit or active product-item index safely."""
        items = self.state["order"].get("items")
        candidate = self.state.get("activeItemIndex") if value is None else value
        try:
            candidate = int(candidate)
        except (OverflowError, TypeError, ValueError):
            return None
        return candidate if isinstance(items, list) and len(items) > 1 and 0 <= candidate < len(items) else None

    @staticmethod
    def _validate_call_tool_payload(name: str, payload: dict[str, Any]) -> str | None:
        """Validate the small public-tool envelope used by HTTP and planners.

        MCP performs the same checks at its transport boundary, but the local
        browser API also calls ``Agent.call_tool`` directly.  Keep this gate
        strict enough that stringly typed values cannot silently change the
        meaning of a preflight or quote request.
        """
        if any(not isinstance(key, str) for key in payload):
            return "工具参数名必须是字符串"
        allowed: dict[str, set[str]] = {
            "validate_order": {"order"},
            # The provider-neutral legacy schema lists ``order`` as required.
            # It is accepted only as a bounded compatibility envelope and is
            # discarded before dispatch; the session state remains authoritative.
            "recommend_processes": {"order", "itemIndex"},
            "explain_print_term": {"question"},
            "estimate_price": {"order", "itemIndex"},
            "prepare_handoff": {"order", "platformId", "itemIndex"},
            "match_supplier_capability": {"order", "platformId", "itemIndex"},
            "request_supplier_quote": {"order", "platformId", "itemIndex"},
            "preflight_file": {"fileName", "sizeBytes", "pageCount", "encrypted",
                                "readable", "inspection", "expectedSize", "itemIndex"},
        }
        if name in allowed:
            unknown = [key for key in payload if key not in allowed[name]]
            if unknown:
                return f"工具参数不支持：{'、'.join(sorted(key[:128] for key in unknown))}"

        if "itemIndex" in payload:
            value = payload.get("itemIndex")
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                return "参数 itemIndex 必须是整数或 null"
            if isinstance(value, int) and (value < 0 or value > MAX_TOOL_SAFE_INTEGER):
                return "参数 itemIndex 超出数值范围"

        if "order" in payload:
            order = payload.get("order")
            if not isinstance(order, dict):
                return "参数 order 必须是 JSON 对象"
            try:
                order_size = len(json.dumps(order, ensure_ascii=False,
                                            allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, RecursionError):
                return "参数 order 不是有效 JSON"
            if order_size > MAX_TOOL_ORDER_BYTES:
                return "参数 order 超过大小限制"

        if name in {"match_supplier_capability", "request_supplier_quote", "prepare_handoff"} \
                and "platformId" in payload:
            value = payload.get("platformId")
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
                    return "参数 platformId 必须是有效字符串"
                if value.strip() not in PLATFORMS:
                    return "未知目标平台"

        if name == "explain_print_term" and "question" in payload:
            value = payload.get("question")
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 2000:
                return "参数 question 必须是非空字符串且不超过 2000 字符"

        if name != "preflight_file":
            return None

        # A public tool call must carry the scanner's explicit state.  The
        # legacy ``upload()`` method keeps defaults for the browser path, but
        # a model/API caller must not silently turn missing metadata into
        # ``encrypted=False`` or ``readable=True``.
        missing_flags = [key for key in ("encrypted", "readable") if key not in payload]
        if missing_flags:
            return "预检必须明确提供 encrypted 和 readable"

        file_name = payload.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            return "参数 fileName 必须是非空字符串"
        file_name = file_name.strip()
        if len(file_name) > MAX_TOOL_FILE_NAME_LENGTH:
            return "参数 fileName 超过长度限制"
        if any(char in file_name for char in ("/", "\\", "\x00")) or file_name in {".", ".."}:
            return "参数 fileName 不能包含路径"

        size_bytes = payload.get("sizeBytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            return "参数 sizeBytes 必须是整数"
        if size_bytes < 0 or size_bytes > MAX_TOOL_SAFE_INTEGER:
            return "参数 sizeBytes 超出数值范围"

        if "pageCount" in payload:
            page_count = payload.get("pageCount")
            if page_count is not None and (isinstance(page_count, bool) or not isinstance(page_count, int)):
                return "参数 pageCount 必须是整数或 null"
            if isinstance(page_count, int) and (page_count < 0 or page_count > MAX_TOOL_SAFE_INTEGER):
                return "参数 pageCount 超出数值范围"
        for key in ("encrypted", "readable"):
            if key in payload and not isinstance(payload.get(key), bool):
                return f"参数 {key} 必须是布尔值"

        if "expectedSize" in payload:
            expected = payload.get("expectedSize")
            if expected is not None and (not isinstance(expected, str) or len(expected.strip()) > 128):
                return "参数 expectedSize 必须是字符串或 null且不超过 128 字符"
        if "inspection" in payload:
            inspection = payload.get("inspection")
            if inspection is not None and not isinstance(inspection, dict):
                return "参数 inspection 必须是对象或 null"
            try:
                encoded_size = len(json.dumps(inspection, ensure_ascii=False,
                                              allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, RecursionError):
                return "参数 inspection 不是有效 JSON"
            if encoded_size > MAX_TOOL_INSPECTION_BYTES:
                return "参数 inspection 超过大小限制"
        return None

    def _item_order(self, index: int) -> dict[str, Any] | None:
        """Build a standalone order view for one item and inherit only shared fields."""
        items = self.state["order"].get("items")
        if not isinstance(items, list) or not (0 <= index < len(items)) or not isinstance(items[index], dict):
            return None
        item = deepcopy(items[index])
        item["items"] = []
        item["productTypes"] = []
        item["platform"] = self.state["order"].get("platform") or "generic"
        for key in ("purpose", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget"):
            if not item.get(key) and self.state["order"].get(key):
                item[key] = deepcopy(self.state["order"][key])
        top_dimensions = self.state["order"].get("dimensions") if isinstance(self.state["order"].get("dimensions"), dict) else {}
        item_dimensions = item.get("dimensions") if isinstance(item.get("dimensions"), dict) else {}
        if "/" not in str(self.state["order"].get("size") or ""):
            item["dimensions"] = {
                key: item_dimensions.get(key) or top_dimensions.get(key) or ""
                for key in DIMENSION_DEFAULTS
            }
        return item

    def _item_validation(self, index: int) -> dict[str, Any] | None:
        item_order = self._item_order(index)
        if item_order is None:
            return None
        result = validate_order(item_order)
        return {
            "itemId": item_order.get("itemId") or f"item-{index + 1}", "index": index,
            "productType": item_order.get("productType") or "", "ok": bool(result.get("ok")) and not result.get("productMissing"),
            "missing": result.get("missing", []), "productMissing": result.get("productMissing", []),
            "warnings": result.get("warnings", []), "risks": result.get("risks", []),
            "parameters": [{"key": parameter.get("key"), "label": parameter.get("label"),
                            "value": parameter.get("value", ""), "filled": parameter.get("filled", False),
                            "required": parameter.get("required", False)}
                           for parameter in result.get("productProfile", {}).get("parameters", [])
                           if parameter.get("key")],
            "readiness": round((result.get("readiness", 0) + result.get("productReadiness", 0)) / 2),
            "productReadiness": result.get("productReadiness", 0),
        }

    def _recommend_item(self, index: int) -> list[dict[str, str]]:
        item_order = self._item_order(index)
        if item_order is None:
            return []
        validation = validate_order(item_order)
        if not validation.get("ok") or validation.get("productMissing"):
            return []
        options = self._call("recommend_processes", item_order)
        item_id = item_order.get("itemId") or f"item-{index + 1}"
        self.state.setdefault("itemOptions", {})[item_id] = deepcopy(options)
        self._store_process_options(options, index)
        self.state["workflowStage"] = "recommend"
        return options

    def _prepare_quote_request(self, item_index: int | None = None,
                               platform_id: str | None = None) -> dict[str, Any]:
        """Prepare and persist one quote request without contacting a supplier."""
        selected_id = str(platform_id or self.state["order"].get("platform") or "generic")
        resolved_index = self._item_index(item_index)
        item_order = self._item_order(resolved_index) if resolved_index is not None else None
        quote_order = item_order or self.state["order"]
        validation = validate_order(quote_order)
        missing = list(validation.get("missing") or []) + list(validation.get("productMissing") or [])
        selected = bool(item_order.get("selectedOption")) if item_order is not None else bool(self.state.get("selectedOption"))
        if missing or not selected:
            if missing:
                self.state["workflowStage"] = "collect" if validation.get("missing") else "clarify"
                message = f"当前信息还不完整，暂不能询价。请先补充：{'、'.join(missing)}。"
            else:
                self.state["workflowStage"] = "recommend"
                message = "请先选择工艺方案，再生成询价请求。"
            blocked_result = {
                "status": "blocked", "reason": "order_not_ready", "missing": missing,
                "requiresHumanConfirmation": True, "message": message,
            }
            if item_order is not None and resolved_index is not None:
                blocked_result.update({"itemId": item_order.get("itemId"), "itemIndex": resolved_index})
            return blocked_result
        result = self._call("request_supplier_quote", quote_order, selected_id)
        if resolved_index is not None and item_order is not None:
            result = {**result, "itemId": item_order.get("itemId"), "itemIndex": resolved_index}
        if result.get("status") == "blocked":
            self.state["workflowStage"] = "clarify"
            return result
        result = self._persist_quote_request(result, quote_order, selected_id, resolved_index)
        self.state["workflowStage"] = "quote"
        return result

    def _base_workflow_stage(self, validation: dict[str, Any] | None = None) -> str:
        validation = validation or validate_order(self.state["order"])
        if validation.get("multiProduct"):
            active_index = self._item_index()
            active_validation = next((item for item in validation.get("itemValidations", [])
                                      if item.get("index") == active_index), None)
            items = self.state["order"].get("items") or []
            item_validations = validation.get("itemValidations") or []
            if item_validations and all(item.get("ok") for item in item_validations) \
                    and all(item.get("selectedOption") for item in items if isinstance(item, dict)):
                return "confirm"
            active_item = items[active_index] if active_validation and isinstance(active_index, int) else None
            if active_validation and active_validation.get("ok") and active_item and not active_item.get("selectedOption"):
                return "recommend"
            return "clarify"
        if validation.get("missing"):
            return "collect"
        if validation.get("productMissing"):
            return "clarify"
        if not self.state.get("selectedOption"):
            return "recommend"
        return "confirm"

    def _workflow_stage(self, validation: dict[str, Any] | None = None) -> str:
        stored = self.state.get("workflowStage")
        if stored in {"preflight", "quote", "export"}:
            return stored
        return self._base_workflow_stage(validation)

    @staticmethod
    def _finite_confidence(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _is_patch_scalar(value: Any) -> bool:
        """Return whether a model/user patch value is safe to stringify.

        Order fields are text or numeric scalars.  Dicts/lists must never be
        coerced with ``str(...)`` because doing so can turn an attacker/model
        payload such as ``{"quantity": 500}`` into an executable field value.
        Booleans are intentionally excluded: none of the current order
        contracts are boolean fields, and ``True``/``False`` would otherwise
        become misleading production text.
        """
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return False
        return not (isinstance(value, float) and not math.isfinite(value))

    def _resolve_confidence(self, key: str, base: float, field_confidence: dict[str, float] | None) -> float:
        """Prefer a planner/NLU field grade over the scalar fallback."""
        fallback = self._finite_confidence(base)
        if fallback is None:
            # Malformed provenance must fail closed as low confidence, while
            # still allowing the rest of the conversation to continue.
            fallback = 0.0
        if not field_confidence:
            return fallback
        candidates = [key]
        if key.startswith("items."):
            remainder = ".".join(key.split(".")[2:])
            candidates.extend([remainder, ".".join(remainder.split(".")[-2:])])
        if key.startswith("productSpecs."):
            candidates.append("productSpecs")
        if key.startswith("dimensions."):
            name = key.split(".", 1)[1]
            candidates.extend([f"productSpecs.{name}", "dimensions"])
            if name == "finishedSize":
                candidates.append("size")
            elif name == "packageSize":
                candidates.extend(["productSpecs.boxSize", "productSpecs.bagSize", "size"])
        if key in {"quantityValue", "quantityUnit"}:
            candidates.append("quantity")
        for candidate in candidates:
            if candidate not in field_confidence:
                continue
            number = self._finite_confidence(field_confidence[candidate])
            if number is not None:
                return number
        return fallback

    @staticmethod
    def _normalize_evidence_entry(value: Any) -> dict[str, str] | None:
        """Normalize one dsh evidence record and bound untrusted strings."""
        if isinstance(value, str):
            quote = value.strip()
            source = ""
        elif isinstance(value, dict):
            raw_quote = value.get("quote", value.get("evidence", ""))
            # Evidence quotes are display/audit text, not arbitrary JSON.  Do
            # not stringify lists, mappings, or booleans into misleading
            # provenance that could later look like user confirmation.
            if not isinstance(raw_quote, str):
                return None
            quote = raw_quote.strip()
            raw_source = value.get("source", "")
            source = raw_source.strip() if isinstance(raw_source, str) else ""
        else:
            return None
        if not quote:
            return None
        entry = {"quote": quote[:2048]}
        if source:
            entry["source"] = source[:128]
        return entry

    @classmethod
    def _normalize_field_evidence(cls, value: Any) -> dict[str, dict[str, str]]:
        """Accept dsh's evidence array and compact field-to-quote maps."""
        if not isinstance(value, (dict, list)):
            return {}
        entries: list[tuple[Any, Any]] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    entries.append((item.get("field"), item))
        else:
            entries.extend((field, item) for field, item in value.items())
        result: dict[str, dict[str, str]] = {}
        for raw_field, raw_entry in entries:
            if not isinstance(raw_field, str):
                continue
            field = raw_field.strip()
            if not field:
                continue
            normalized = cls._normalize_evidence_entry(raw_entry)
            if normalized is None:
                continue
            result[field[:256]] = normalized
            if len(result) >= 128:
                break
        return result

    @classmethod
    def _resolve_evidence(cls, key: str,
                          field_evidence: dict[str, dict[str, str]] | None) -> dict[str, str] | None:
        if not field_evidence:
            return None
        candidates = [key]
        if key.startswith("items."):
            remainder = ".".join(key.split(".")[2:])
            candidates.extend([remainder, ".".join(remainder.split(".")[-2:])])
        if key.startswith("productSpecs."):
            candidates.append("productSpecs")
        if key.startswith("dimensions."):
            name = key.split(".", 1)[1]
            candidates.extend([f"productSpecs.{name}", "dimensions"])
            if name == "finishedSize":
                candidates.append("size")
            elif name == "packageSize":
                candidates.extend(["productSpecs.boxSize", "productSpecs.bagSize", "size"])
        if key in {"quantityValue", "quantityUnit"}:
            candidates.append("quantity")
        for candidate in candidates:
            if candidate in field_evidence:
                return deepcopy(field_evidence[candidate])
        return None

    def _has_field_signal(self, key: str,
                          field_confidence: dict[str, float] | None,
                          field_evidence: dict[str, dict[str, str]] | None) -> bool:
        """Tell whether a planner supplied provenance specifically for a field."""
        if self._resolve_evidence(key, field_evidence) is not None:
            return True
        if not field_confidence:
            return False
        candidates = [key]
        if key.startswith("items."):
            remainder = ".".join(key.split(".")[2:])
            candidates.extend([remainder, ".".join(remainder.split(".")[-2:])])
        if key.startswith("productSpecs."):
            candidates.append("productSpecs")
        if key.startswith("dimensions."):
            name = key.split(".", 1)[1]
            candidates.extend([f"productSpecs.{name}", "dimensions"])
            if name == "finishedSize":
                candidates.append("size")
            elif name == "packageSize":
                candidates.extend(["productSpecs.boxSize", "productSpecs.bagSize", "size"])
        if key in {"quantityValue", "quantityUnit"}:
            candidates.append("quantity")
        return any(candidate in field_confidence
                   and self._finite_confidence(field_confidence[candidate]) is not None
                   for candidate in candidates)

    def _set_field_meta(self, key: str, value: Any, source: str, confidence: float,
                        field_confidence: dict[str, float] | None = None,
                        field_evidence: dict[str, dict[str, str]] | None = None) -> None:
        if value in (None, ""):
            self.state.setdefault("fieldMeta", {}).pop(key, None)
            return
        graded = self._resolve_confidence(key, confidence, field_confidence)
        entry: dict[str, Any] = {
            "value": deepcopy(value), "source": source,
            "sourceLabel": FIELD_SOURCE_LABELS.get(source, source),
            "confidence": round(max(0.0, min(1.0, float(graded))), 2),
            "runId": self.run_id or None, "updatedAt": self._timestamp(),
        }
        evidence = self._resolve_evidence(key, field_evidence)
        if evidence is None:
            # Re-confirming an unchanged value should not discard its prior
            # evidence, while a changed value naturally receives fresh data.
            previous = self.state.setdefault("fieldMeta", {}).get(key)
            if isinstance(previous, dict) and self._equivalent_value(previous.get("value"), value):
                prior_evidence = previous.get("evidence")
                if isinstance(prior_evidence, dict):
                    evidence = self._normalize_evidence_entry(prior_evidence)
        if evidence is not None:
            entry["evidence"] = evidence
        self.state.setdefault("fieldMeta", {})[key] = entry

    def _record_rejected_fields(self, fields: list[str] | set[str] | tuple[str, ...] | None) -> None:
        """Keep a bounded, de-duplicated audit list of rejected patch paths."""
        if not isinstance(fields, (list, set, tuple)) or not fields:
            return
        current = self.state.setdefault("rejectedFields", [])
        if not isinstance(current, list):
            current = self.state["rejectedFields"] = []
        for raw in fields:
            field = str(raw).strip()
            if field and field not in current:
                current.append(field[:256])
        del current[:-128]

    @staticmethod
    def _normalize_plan_annotation(value: Any) -> str | dict[str, str] | None:
        """Bound one advisory skill question/risk before it reaches a client."""
        if isinstance(value, str):
            text = value.strip()
            return text[:1024] if text else None
        if not isinstance(value, dict):
            return None
        # Keep a small, display-oriented subset; arbitrary nested model data
        # does not belong in the persisted run envelope.
        result: dict[str, str] = {}
        for key in ("field", "question", "text", "message", "risk", "severity", "source", "code"):
            raw = value.get(key)
            if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
                if isinstance(raw, float) and not math.isfinite(raw):
                    continue
                text = str(raw).strip()
                if text:
                    result[key] = text[:512]
        return result or None

    @classmethod
    def _normalize_plan_meta(cls, value: Any) -> dict[str, Any]:
        """Normalize the common dsh skill envelope without granting authority."""
        if not isinstance(value, dict):
            return {"questions": [], "risks": [], "knowledgeVersion": ""}
        normalized: dict[str, Any] = {"questions": [], "risks": [], "knowledgeVersion": ""}
        for key in ("questions", "risks"):
            raw_items = value.get(key)
            if not isinstance(raw_items, list):
                continue
            items: list[str | dict[str, str]] = []
            for raw_item in raw_items[:32]:
                item = cls._normalize_plan_annotation(raw_item)
                if item is not None:
                    items.append(item)
            normalized[key] = items
        version = value.get("knowledgeVersion")
        if isinstance(version, str):
            normalized["knowledgeVersion"] = version.strip()[:128]
        if isinstance(value.get("reportedKnowledgeVersion"), str):
            normalized["reportedKnowledgeVersion"] = value["reportedKnowledgeVersion"].strip()[:128]
        if normalized["knowledgeVersion"] and normalized["knowledgeVersion"] != KNOWLEDGE_VERSION:
            normalized["risks"].append({
                "code": "knowledge_version_mismatch",
                "severity": "review",
                "message": "模型知识版本与当前 PrintOps 知识版本不一致，需要复核。",
            })
        return normalized

    def _record_plan_meta(self, plan: Any) -> None:
        """Persist advisory skill output separately from the order state."""
        if not isinstance(plan, dict):
            return
        keys = {"questions", "risks", "knowledgeVersion", "reportedKnowledgeVersion"}
        if not keys.intersection(plan):
            return
        self.state["planMeta"] = self._normalize_plan_meta(plan)

    PRODUCTION_FIELD_KEYS = {"productType", "quantity", "quantityValue", "quantityUnit", "size",
                             "dimensions", "pages", "orientation", "paper", "printing",
                             "finishing", "binding", "deadline"}
    # ``_item_order`` inherits these values from the top-level compatibility
    # projection.  Their top-level provenance therefore applies to a focused
    # multi-product item when that item has no explicit value of its own.
    INHERITED_ITEM_FIELDS = (
        "purpose", "orientation", "paper", "printing", "finishing", "binding",
        "deadline", "budget",
    )
    READINESS_TOOLS = frozenset({"recommend_processes", "prepare_handoff", "request_supplier_quote"})
    CONFIRMATION_TOOLS = frozenset({"prepare_handoff", "request_supplier_quote"})

    @classmethod
    def _is_production_field(cls, key: str) -> bool:
        """Preference fields (budget/purpose/platform) never block generation."""
        if key.startswith("items."):
            key = ".".join(key.split(".")[2:])
        if key.startswith(("productSpecs.", "dimensions.")):
            return True
        return key in cls.PRODUCTION_FIELD_KEYS

    def _low_confidence_fields(self, production_only: bool = False) -> list[str]:
        metadata = self.state.get("fieldMeta") or {}
        if not isinstance(metadata, dict):
            return []
        fields = []
        for key, meta in metadata.items():
            if not isinstance(meta, dict) or meta.get("value") in (None, "", {}):
                continue
            confidence = self._finite_confidence(meta.get("confidence", 1))
            # A malformed confidence attached to a present field is treated as
            # uncertain instead of raising during generation.
            if confidence is None or confidence < 0.75:
                fields.append(str(key))
        if production_only:
            fields = [key for key in fields if self._is_production_field(key)]
        return fields

    @staticmethod
    def _meta_low_confidence(meta: Any) -> bool:
        """Return whether present field provenance is below the approval bar."""
        if not isinstance(meta, dict) or meta.get("value") in (None, "", {}):
            return False
        try:
            confidence = float(meta.get("confidence", 1))
        except (TypeError, ValueError, OverflowError):
            return True
        return not math.isfinite(confidence) or confidence < 0.75

    def _uncertain_fields_for_item(self, item_order: dict[str, Any] | None,
                                   item_index: int | None) -> list[str]:
        """Find low-confidence production fields effective for one item.

        A multi-product item can inherit shared values from the top-level
        compatibility order.  Check explicit item provenance first, then only
        apply a top-level grade when the inherited value is equal to the
        selected item's effective value.
        """
        field_meta = self.state.get("fieldMeta") or {}
        if not isinstance(field_meta, dict):
            return []
        if item_order is None or not isinstance(item_index, int):
            return [str(key) for key, meta in field_meta.items()
                    if not str(key).startswith("items.")
                    and self._is_production_field(str(key))
                    and self._meta_low_confidence(meta)]

        item_id = item_order.get("itemId") or f"item-{item_index + 1}"
        prefix = f"items.{item_id}."
        uncertain: list[str] = []

        # Explicit item provenance takes precedence over shared fallback.
        for raw_key, meta in field_meta.items():
            key = str(raw_key)
            if key.startswith(prefix) and self._is_production_field(key) \
                    and self._meta_low_confidence(meta):
                uncertain.append(key)

        top_order = self.state.get("order") or {}

        def top_value_for(key: str) -> Any:
            if key.startswith("dimensions."):
                dimensions = top_order.get("dimensions")
                if isinstance(dimensions, dict):
                    return dimensions.get(key.split(".", 1)[1])
                return None
            return top_order.get(key)

        def has_value(value: Any) -> bool:
            return value not in (None, "", {})

        def inherited_meta(key: str, item_value: Any) -> None:
            if not has_value(item_value) or key not in field_meta:
                return
            # An item-specific grade was already considered above.
            if f"{prefix}{key}" in field_meta:
                return
            top_value = top_value_for(key)
            if has_value(top_value) and self._equivalent_value(item_value, top_value) \
                    and self._is_production_field(key) \
                    and self._meta_low_confidence(field_meta.get(key)):
                uncertain.append(key)

        for key in self.INHERITED_ITEM_FIELDS:
            inherited_meta(key, item_order.get(key))

        # ``size`` and canonical dimensions can also be inherited from a
        # single top-level phrase by ``_item_order``.
        inherited_meta("size", item_order.get("size"))
        item_dimensions = item_order.get("dimensions") if isinstance(item_order.get("dimensions"), dict) else {}
        for key in DIMENSION_DEFAULTS:
            inherited_meta(f"dimensions.{key}", item_dimensions.get(key))

        return list(dict.fromkeys(uncertain))

    def _tool_precondition(self, name: str, item_index: int | None = None) -> dict[str, Any] | None:
        """Return a bounded readiness decision before a public production tool.

        This is deliberately separate from ``_call``: internal workflow steps
        may call domain tools as part of a validated operation, while every
        external ``call_tool``/MCP invocation must pass this gate first.
        """
        if name not in self.READINESS_TOOLS:
            return None
        item_order = self._item_order(item_index) if isinstance(item_index, int) else None
        order = item_order or self.state.get("order", {})
        validation = validate_order(order)
        missing = list(validation.get("missing") or []) + list(validation.get("productMissing") or [])
        if missing:
            return {
                "sessionId": self.id,
                "toolResult": {"status": "blocked", "reason": "order_not_ready", "missing": missing,
                                "requiresHumanConfirmation": True},
                "validation": validation,
                "workflowStage": "collect" if validation.get("missing") else "clarify",
                "confirmation": deepcopy(self.state.get("confirmation") or {"status": "not_ready"}),
                "decision": {"stage": "clarify", "humanConfirmationRequired": True,
                             "reason": "订单字段或品类参数尚未完整"},
            }
        uncertain = self._uncertain_fields_for_item(item_order, item_index)
        if uncertain and name in self.CONFIRMATION_TOOLS:
            return {
                "sessionId": self.id,
                "toolResult": {"status": "blocked", "reason": "low_confidence", "uncertain": uncertain,
                                "requiresHumanConfirmation": True},
                "validation": validation,
                "workflowStage": "clarify",
                "confirmation": deepcopy(self.state.get("confirmation") or {"status": "not_ready"}),
                "decision": {"stage": "clarify", "humanConfirmationRequired": True,
                             "reason": "存在低置信度生产字段"},
            }
        if name in self.CONFIRMATION_TOOLS:
            selected = (item_order or {}).get("selectedOption") if item_order is not None else self.state.get("selectedOption")
            if not selected:
                return {
                    "sessionId": self.id,
                    "toolResult": {"status": "blocked", "reason": "selection_required",
                                    "requiresHumanConfirmation": True},
                    "validation": validation,
                    "workflowStage": "recommend",
                    "confirmation": deepcopy(self.state.get("confirmation") or {"status": "not_ready"}),
                    "decision": {"stage": "recommend", "humanConfirmationRequired": True,
                                 "reason": "请先选择工艺方案"},
                }
        return None

    def _record_conflict(self, key: str, previous: Any, current: Any, source: str) -> None:
        if previous in (None, "", {}) or current in (None, "", {}) or previous == current:
            return
        conflicts = list(self.state.get("conflicts") or [])
        conflicts.append({"field": key, "label": LABELS.get(key, key), "previous": deepcopy(previous),
                          "current": deepcopy(current), "source": source,
                          "sourceLabel": FIELD_SOURCE_LABELS.get(source, source),
                          "resolved": True, "runId": self.run_id or None, "at": self._timestamp()})
        self.state["conflicts"] = conflicts[-20:]

    @staticmethod
    def _equivalent_value(left: Any, right: Any) -> bool:
        """Ignore harmless spacing/full-width differences from model output."""
        if left in (None, "") or right in (None, ""):
            return left == right
        normalize = lambda value: re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).lower()
        return normalize(left) == normalize(right)

    @classmethod
    def _planner_compact_value(cls, value: Any, depth: int = 0) -> Any:
        """Bound a tool receipt before it is persisted or sent back to a model."""
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:2048] if len(value) <= 2048 else value[:1021] + "...[已截断]..." + value[-1010:]
        if depth >= 5:
            return "[上下文层级已省略]"
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 64:
                    result["_truncatedKeys"] = len(value) - index
                    break
                result[str(key)[:128]] = cls._planner_compact_value(item, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            result = [cls._planner_compact_value(item, depth + 1) for item in value[:24]]
            if len(value) > 24:
                result.append({"_truncatedItems": len(value) - 24})
            return result
        return str(value)[:2048]

    @classmethod
    def _planner_fit_receipts(cls, receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the newest receipts within both entry and byte budgets."""
        selected: list[dict[str, Any]] = []
        total = 0
        for receipt in reversed(receipts[-MAX_PLANNER_LEDGER_ENTRIES:]):
            try:
                size = len(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            except (TypeError, ValueError, OverflowError, RecursionError):
                continue
            if selected and total + size > MAX_PLANNER_LEDGER_BYTES:
                break
            selected.append(receipt)
            total += size
        return list(reversed(selected))

    def _planner_order_fingerprint(self, item_index: int | None = None) -> str:
        """Return a stable version for detecting stale tool observations."""
        order: Any = self._item_order(item_index) if isinstance(item_index, int) else self.state.get("order", {})
        payload = {
            "order": order,
            "selectedOption": self.state.get("selectedOption"),
            "activeItemIndex": self.state.get("activeItemIndex"),
            "uploadedFile": self.state.get("uploadedFile"),
            "uploadedFiles": self.state.get("uploadedFiles") or [],
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            encoded = repr(payload)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _planner_signature(self, name: str, arguments: dict[str, Any],
                           item_index: int | None = None) -> str:
        try:
            args = json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            args = repr(arguments)
        return f"{name}:{self._planner_order_fingerprint(item_index)}:{args}"

    def _mark_planner_receipts_stale(self) -> None:
        """Mark observations produced for an older order version as stale."""
        dependent = {"validate_order", "recommend_processes", "estimate_price",
                     "match_supplier_capability", "prepare_handoff",
                     "request_supplier_quote"}
        for receipt in self._planner_tool_receipts:
            item_index = receipt.get("itemIndex")
            current = self._planner_order_fingerprint(item_index if isinstance(item_index, int) else None)
            if receipt.get("name") in dependent and receipt.get("inputFingerprint") != current:
                receipt["stale"] = True
        self.state["toolReceipts"] = self._planner_fit_receipts(self._planner_tool_receipts)

    def _record_planner_receipt(self, name: str, arguments: dict[str, Any],
                                response: dict[str, Any], input_fingerprint: str,
                                *, call_id: str = "", round_number: int = 0,
                                signature: str = "", status: str | None = None) -> dict[str, Any]:
        result = response.get("toolResult") if isinstance(response, dict) else None
        if status is None:
            if isinstance(result, dict) and isinstance(result.get("status"), str):
                status = result["status"]
            elif isinstance(response, dict) and response.get("messages"):
                status = "ok"
            else:
                status = "error"
        receipt: dict[str, Any] = {
            "name": name[:128], "arguments": self._planner_compact_value(arguments),
            "result": self._planner_compact_value(result), "status": status,
            "inputFingerprint": input_fingerprint, "stale": False,
            "round": round_number, "runId": self.run_id or None,
        }
        item_index = arguments.get("itemIndex") if isinstance(arguments, dict) else None
        if isinstance(item_index, int):
            receipt["itemIndex"] = item_index
        if call_id:
            receipt["callId"] = call_id[:256]
        if signature:
            receipt["signature"] = signature[:512]
        self._planner_tool_receipts.append(receipt)
        self._planner_tool_receipts = self._planner_fit_receipts(self._planner_tool_receipts)
        self.state["toolReceipts"] = deepcopy(self._planner_tool_receipts)
        self._event("tool_result", "ok" if status not in {"error", "invalid_arguments", "blocked"}
                    else status, "已记录工具结果", tool=name, round=round_number,
                    callId=call_id or None, stale=False)
        self._save()
        return receipt

    def _planner_dispatch_tool(self, name: str, arguments: dict[str, Any],
                               *, call_id: str = "", round_number: int = 0,
                               signature: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
        """Execute one planner call without closing the surrounding chat run."""
        item_index = arguments.get("itemIndex") if isinstance(arguments, dict) else None
        input_fingerprint = self._planner_order_fingerprint(item_index if isinstance(item_index, int) else None)
        previous_nested = self._nested_tool_execution
        self._nested_tool_execution = True
        try:
            try:
                response = self.call_tool(name, arguments, preserve_trace=True, remember=False)
            except Exception as error:  # Provider-supplied arguments must never escape the loop.
                self._event("tool", "error", "工具执行异常，已转换为可恢复结果", tool=name,
                            error=error.__class__.__name__, round=round_number)
                response = self._tool_reply(
                    "工具执行失败，未完成该步骤。",
                    remember=False,
                    tool_result={"status": "error", "reason": "tool_exception",
                                 "errorType": error.__class__.__name__},
                )
        finally:
            self._nested_tool_execution = previous_nested
        receipt = self._record_planner_receipt(
            name, arguments, response, input_fingerprint,
            call_id=call_id, round_number=round_number, signature=signature,
        )
        return response, receipt

    def _planner_tool_attempted(self, name: str, *, include_stale: bool = False) -> bool:
        return any(item.get("name") == name and (include_stale or not item.get("stale"))
                   for item in self._planner_tool_receipts)

    def _planner_tool_satisfied(self, name: str) -> bool:
        for receipt in reversed(self._planner_tool_receipts):
            if receipt.get("name") != name or receipt.get("stale"):
                continue
            status = receipt.get("status")
            if status in {"error", "invalid_arguments", "duplicate_blocked"}:
                continue
            result = receipt.get("result")
            if name == "recommend_processes":
                return (status == "ready" or isinstance(result, list) and bool(result)
                        or isinstance(result, dict) and bool(result.get("options")))
            if name in {"prepare_handoff", "request_supplier_quote"}:
                return status in {"ready", "awaiting_human_confirmation", "confirmed"}
            if name == "estimate_price":
                return status not in {"blocked"} and isinstance(result, dict) and bool(result.get("range"))
            if name == "match_supplier_capability":
                return status not in {"blocked"}
            # Validation and explanation are useful observations even when they
            # report missing fields; repeating them wastes a planner round.
            return True
        return False

    def _planner_latest_receipt(self, name: str) -> dict[str, Any] | None:
        for receipt in reversed(self._planner_tool_receipts):
            if receipt.get("name") == name:
                return receipt
        return None

    @staticmethod
    def _planner_positive_intent(text: str, pattern: str) -> bool:
        """Match an action while respecting nearby Chinese negation."""
        for match in re.finditer(pattern, text or ""):
            prefix = (text or "")[max(0, match.start() - 12):match.start()]
            if re.search(
                    r"(?:不要|不用|无需|不需要|别|先不|暂不|取消)[^，,。；;！？!?]{0,5}$",
                    prefix):
                continue
            return True
        return False

    def _planner_intent(self, text: str, name: str) -> bool:
        """Apply one shared, negation-aware intent rule."""
        pattern = self.PLANNER_INTENT_PATTERNS.get(name)
        return bool(pattern and self._planner_positive_intent(text, pattern))

    def _has_explicit_action_intent(self, text: str) -> bool:
        """Return whether a chat turn asks for work beyond changing item focus."""
        raw = (text or "").strip()
        return self._is_explanation_request(raw) or any(
            self._planner_intent(raw, name) for name in self.PLANNER_INTENT_PATTERNS
        )

    def _planner_artifacts(self) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Recover UI artifacts from every current tool observation."""
        options: list[dict[str, Any]] = []
        handoff: dict[str, Any] | None = None
        for receipt in self._planner_tool_receipts:
            if receipt.get("stale"):
                continue
            result = receipt.get("result")
            if receipt.get("name") == "recommend_processes":
                candidate = result.get("options") if isinstance(result, dict) else result
                if isinstance(candidate, list) and candidate:
                    options = [deepcopy(item) for item in candidate if isinstance(item, dict)]
            elif (receipt.get("name") == "prepare_handoff" and isinstance(result, dict)
                  and result.get("status") == "ready"):
                handoff = deepcopy(result)
        return options, handoff

    def _planner_receipts_fallback(self) -> str:
        """Summarize the bounded ledger when model synthesis is unavailable."""
        messages: list[str] = []
        seen_names: set[str] = set()
        for receipt in reversed(self._planner_tool_receipts):
            name = receipt.get("name")
            if (receipt.get("stale") or not isinstance(name, str)
                    or name in seen_names or receipt.get("status") == "duplicate_blocked"):
                continue
            seen_names.add(name)
            message = self._planner_tool_fallback(name, receipt.get("result"))
            if message and message not in messages:
                messages.append(message)
            if len(messages) >= 4:
                break
        return "\n".join(reversed(messages))[:6000] or "当前步骤未完成，请检查订单信息后重试。"

    def _planner_required_tools(self, text: str, initial_validation: dict[str, Any] | None = None) -> list[str]:
        """Derive hard local obligations from intent and authoritative state."""
        raw = (text or "").strip()
        required: list[str] = []

        def add(name: str) -> None:
            if name not in required:
                required.append(name)

        quote_intent = self._planner_intent(raw, "quote")
        price_intent = self._planner_intent(raw, "price")
        capability_intent = self._planner_intent(raw, "capability")
        handoff_intent = self._planner_intent(raw, "handoff")
        validation_intent = self._planner_intent(raw, "validation")
        recommendation_intent = self._planner_intent(raw, "recommendation")

        if validation_intent:
            add("validate_order")
        if self._is_explanation_request(raw):
            add("explain_print_term")
        if recommendation_intent:
            add("recommend_processes")
        if capability_intent:
            add("match_supplier_capability")
        if price_intent:
            add("estimate_price")

        active_index = self._item_index()
        active_order = self._item_order(active_index) if active_index is not None else None
        validation = validate_order(active_order or self.state.get("order", {}))
        ready = bool(validation.get("ok") and not validation.get("productMissing"))
        selected = (bool(active_order.get("selectedOption")) if active_order is not None
                    else bool(self.state.get("selectedOption")))
        # Quote and handoff are explicit local actions.  When the order is
        # otherwise ready, surface selectable options first instead of only
        # returning a selection_required block with no options in the UI.
        if (quote_intent or handoff_intent) and ready and not selected:
            add("recommend_processes")
        if quote_intent:
            add("request_supplier_quote")
        if handoff_intent:
            add("prepare_handoff")

        explicit_action = any((quote_intent, price_intent, capability_intent,
                               handoff_intent, validation_intent,
                               recommendation_intent, self._is_explanation_request(raw)))
        if not ready and not explicit_action:
            add("validate_order")
        initial_ready = bool(initial_validation and initial_validation.get("ok")
                            and not initial_validation.get("productMissing"))
        generic_make = self._planner_positive_intent(raw, r"做|印刷|制作")
        if ready and not selected and not explicit_action \
                and (generic_make or (initial_validation is not None and not initial_ready)):
            add("recommend_processes")
        return required[:MAX_PLANNER_REQUIRED_CALLS]

    def chat(self, text: str, patch: dict[str, str] | None = None, item_index: int | None = None) -> dict[str, Any]:
        self._begin_run("chat")
        self.trace = ["感知需求"]
        self._event("perceive", "ok", "已完成规则感知")
        perceived, perceived_confidence = self._perceive_full(
            text, self.state["order"].get("productType") or "")
        focus_match = re.search(r"第\s*(\d+)\s*项", text or "")
        explicit_action = self._has_explicit_action_intent(text)
        target_supplied = item_index is not None or focus_match is not None
        requested_index: Any = item_index
        if requested_index is None and focus_match:
            try:
                requested_index = int(focus_match.group(1)) - 1
            except ValueError:
                requested_index = -1
        if target_supplied:
            items = self.state["order"].get("items")
            if (isinstance(requested_index, bool)
                    or not isinstance(requested_index, int)
                    or not isinstance(items, list)
                    or not (0 <= requested_index < len(items))):
                self._remember("user", text)
                message = "没有找到指定的产品项，请重新选择后再继续。"
                self._remember("assistant", message)
                self._save()
                return self._result([message], tool_result={"status": "blocked", "reason": "item_not_found"})
            self.state["activeItemIndex"] = requested_index
            # A focus-only command should not trigger a second perception pass
            # or overwrite any product fields. Action turns continue below in
            # the newly selected item's context.
            if not perceived and not patch and not explicit_action:
                self._remember("user", text)
                product = items[requested_index].get("productType") if isinstance(items[requested_index], dict) else ""
                message = f"已切换到第 {requested_index + 1} 项{f'：{product}' if product else ''}。请继续补充或确认这一项的参数。"
                self._remember("assistant", message)
                self._save()
                return self._result([message])
        if patch is not None and not isinstance(patch, dict):
            patch = {}
        items = self.state["order"].get("items")
        active_item = self.state.get("activeItemIndex")
        target_item = (isinstance(items, list) and isinstance(active_item, int)
                       and 0 <= active_item < len(items) and len(items) > 1)
        if target_item:
            changed_item = self._update_item(active_item, perceived, source="rule", confidence=0.84,
                                             field_confidence=perceived_confidence)
            if patch:
                changed_item |= self._update_item(active_item, patch, source="user", confidence=1.0)
            changed_fields = [f"items.{items[active_item].get('itemId', f'item-{active_item + 1}')}.{key}" for key in changed_item]
        else:
            changed = self._update_order(perceived, source="rule", confidence=0.84,
                                         field_confidence=perceived_confidence)
            if patch:
                changed |= self._update_order(patch, source="user", confidence=1.0)
            changed_fields = list(changed)
        self.state["workflowStage"] = self._base_workflow_stage()
        self._event("memory", "ok", "已更新订单记忆", changedFields=changed_fields)
        self._remember("user", text)

        multi_products = _multi_product_info(self.state["order"])
        if multi_products and not explicit_action:
            self.state["stage"] = "collect"
            self.state["workflowStage"] = "clarify"
            validation = self._call("validate_order", self.state["order"])
            item_validations = validation.get("itemValidations") or []
            current_item = next((item for item in item_validations if item.get("index") == active_item), None)
            item_options: list[dict[str, str]] = []
            if target_item and current_item:
                self._event("clarify", "ok" if current_item.get("ok") else "blocked", "已更新独立产品项",
                            itemId=current_item.get("itemId"), readiness=current_item.get("readiness"))
                missing = list(current_item.get("missing") or []) + list(current_item.get("productMissing") or [])
                item_name = f"（{current_item.get('productType')}）" if current_item.get("productType") else ""
                if missing:
                    message = (f"已更新第 {active_item + 1} 项{item_name}，"
                               f"当前信息度 {current_item.get('readiness', 0)}%。还需确认：{'、'.join(missing)}。")
                else:
                    item_options = self._recommend_item(active_item)
                    message = (f"已更新第 {active_item + 1} 项{item_name}，当前信息度 100%。"
                               f"我为这一项生成了 {len(item_options)} 个工艺方案，请选择后再继续询价。")
            else:
                self._event("clarify", "blocked", "检测到多个产品，需要拆分订单项", products=multi_products)
                message = (f"我识别到多个印刷品：{'、'.join(multi_products)}。"
                            "它们的尺寸、材料和后道不同，当前不能合并成一张订单或直接报价。"
                            "请先分别确认每个产品的数量、尺寸、材料和交期，我会按独立订单项继续。")
            self._remember("assistant", message)
            self._save()
            return self._result([message], options=item_options, tool_result=item_options or validation)

        # Generating a multi-product order remains an aggregate operation. An
        # ordinal only establishes which item the UI should show if generation
        # is blocked; it never creates a partial handoff that bypasses the other
        # items' readiness and selection gates.
        if multi_products and self._planner_positive_intent(
                text, r"生成订单|生成草稿|下单"):
            self._save()
            return self.generate()

        # An optional LLM planner improves language understanding while field
        # patches and tool names remain constrained by this Agent.
        if self.planner and getattr(self.planner, "enabled", False):
            # A new chat owns a fresh bounded receipt ledger.  The previous
            # run remains available through runHistory/lastRun, but must not
            # contaminate this provider request.
            self._planner_tool_receipts = []
            self.state["toolReceipts"] = []
            initial_validation = validate_order(self.state["order"])
            self.trace.append(f"调用模型：{getattr(self.planner, 'model', '已配置模型')}")
            self._event("plan", "started", "请求模型规划", model=getattr(self.planner, "model", "已配置模型"))
            plan = self._ask_planner(text, tool_ledger=[])
            last_tool_name = ""
            last_tool_response: dict[str, Any] | None = None
            last_receipt: dict[str, Any] | None = None
            final_reply = ""
            seen_signatures: set[str] = set()
            tool_call_count = 0
            limit_reached = False
            forced_names: list[str] = []
            post_force_synthesized = False

            def plan_calls(value: dict[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any] | None]]:
                """Normalize JSON and native batch calls to one local shape."""
                raw_batch = value.get("_nativeToolCalls")
                calls: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
                if isinstance(raw_batch, list):
                    for raw_call in raw_batch:
                        if not isinstance(raw_call, dict):
                            continue
                        function = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
                        name = function.get("name")
                        arguments = function.get("arguments", {})
                        if isinstance(name, str) and isinstance(arguments, dict):
                            calls.append((name.strip(), arguments, raw_call))
                if calls:
                    return calls
                raw_tool = value.get("tool")
                if not isinstance(raw_tool, dict):
                    return []
                name = raw_tool.get("name")
                arguments = raw_tool.get("arguments", {})
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    return []
                native = value.get("_nativeToolCall") if isinstance(value.get("_nativeToolCall"), dict) else None
                return [(name.strip(), arguments, native)]

            # The model gets at most three local tool rounds.  A batch response
            # consumes one budget unit per call and is executed sequentially so
            # state-dependent tools observe the prior call's result.
            while isinstance(plan, dict):
                if not isinstance(plan, dict):
                    break
                changed = self._apply_plan(plan)
                if changed:
                    self._mark_planner_receipts_stale()
                calls = plan_calls(plan)
                if not calls:
                    if plan.get("reply"):
                        final_reply = str(plan["reply"])
                    break
                batch_native_calls: list[dict[str, Any]] = []
                stop_loop = False
                for tool_name, arguments, native_call in calls:
                    if tool_call_count >= MAX_PLANNER_TOOL_ROUNDS:
                        limit_reached = True
                        self._event("plan", "limit_reached", "工具调用达到本轮上限",
                                    tool=tool_name, round=tool_call_count + 1)
                        stop_loop = True
                        break
                    item_index = arguments.get("itemIndex") if isinstance(arguments, dict) else None
                    tool_signature = self._planner_signature(
                        tool_name, arguments, item_index if isinstance(item_index, int) else None)
                    if tool_signature in seen_signatures:
                        self.trace.append(f"阻止重复工具：{tool_name}")
                        self._event("plan", "duplicate_blocked", "模型重复请求相同工具和状态",
                                    tool=tool_name, round=tool_call_count + 1,
                                    signature=tool_signature[:512])
                        self._planner_tool_receipts.append({
                            "name": tool_name[:128], "arguments": self._planner_compact_value(arguments),
                            "result": {"status": "duplicate_blocked"}, "status": "duplicate_blocked",
                            "inputFingerprint": self._planner_order_fingerprint(
                                item_index if isinstance(item_index, int) else None),
                            "stale": False, "round": tool_call_count + 1,
                            "runId": self.run_id or None, "signature": tool_signature[:512],
                        })
                        self._planner_tool_receipts = self._planner_fit_receipts(self._planner_tool_receipts)
                        self.state["toolReceipts"] = deepcopy(self._planner_tool_receipts)
                        stop_loop = True
                        break
                    seen_signatures.add(tool_signature)
                    last_tool_name = tool_name
                    call_id = ""
                    if isinstance(native_call, dict) and isinstance(native_call.get("id"), str):
                        call_id = native_call["id"]
                        batch_native_calls.append(deepcopy(native_call))
                    self._event("plan", "tool_requested", "模型请求调用工具", tool=tool_name,
                                round=tool_call_count + 1, callId=call_id or None)
                    try:
                        last_tool_response, last_receipt = self._planner_dispatch_tool(
                            tool_name, arguments, call_id=call_id,
                            round_number=tool_call_count + 1, signature=tool_signature)
                    except Exception as error:
                        # _planner_dispatch_tool normally catches this; keep a
                        # second boundary in case a custom gateway fails while
                        # constructing its response.
                        self._event("tool", "error", "工具结果封装失败", tool=tool_name,
                                    error=error.__class__.__name__)
                        last_tool_response = {"messages": ["工具执行失败，未完成该步骤。"],
                                              "toolResult": {"status": "error", "reason": "tool_exception"}}
                        last_receipt = None
                    tool_call_count += 1
                    if last_receipt is not None:
                        self._mark_planner_receipts_stale()
                    result_value = last_tool_response.get("toolResult") if isinstance(last_tool_response, dict) else None
                    if stop_loop:
                        break
                if stop_loop:
                    break
                if tool_call_count >= MAX_PLANNER_TOOL_ROUNDS:
                    limit_reached = True
                    self._event("plan", "limit_reached", "工具调用达到本轮上限",
                                round=tool_call_count)
                    break
                self.trace.append(f"调用模型总结工具结果：{getattr(self.planner, 'model', '已配置模型')}")
                plan = self._ask_planner(
                    text,
                    {"name": last_tool_name, "result": result_value},
                    tool_call=(batch_native_calls[-1] if batch_native_calls else None),
                    tool_calls=batch_native_calls or None,
                    tool_ledger=self._planner_tool_receipts,
                )

            # A hard round limit is followed by one tools-disabled synthesis
            # request.  This keeps the final answer model-generated without
            # allowing an unbounded continuation loop.
            if limit_reached:
                self.trace.append(f"调用模型最终总结：{getattr(self.planner, 'model', '已配置模型')}")
                synthesis = self._ask_planner(
                    text,
                    {"name": last_tool_name, "result": (last_tool_response or {}).get("toolResult")},
                    tool_call=None, tool_calls=None,
                    tool_ledger=self._planner_tool_receipts,
                    allow_tools=False, synthesis=True,
                )
                if isinstance(synthesis, dict):
                    # This turn has tools disabled and is summary-only.  A late
                    # patch would make every completed observation stale with
                    # no remaining re-plan budget, so it is deliberately not
                    # applied.
                    if synthesis.get("patch"):
                        self._event("plan", "patch_ignored", "最终总结中的字段更新未应用")
                    if synthesis.get("reply"):
                        final_reply = str(synthesis["reply"])

            self._mark_planner_receipts_stale()
            required_names = self._planner_required_tools(text, initial_validation)
            # A bare production request gets one automatic recommendation when
            # the provider emitted only text/validation.  If it already chose a
            # different successful domain observation, keep that choice unless
            # the user explicitly asked for a scheme (compound intents remain
            # hard obligations).
            explicit_recommendation = self._planner_intent(text, "recommendation")
            if required_names == ["recommend_processes"] and not explicit_recommendation \
                    and any(item.get("name") != "validate_order" and not item.get("stale")
                            and item.get("status") not in {
                                "error", "invalid_arguments", "duplicate_blocked", "blocked",
                            } for item in self._planner_tool_receipts):
                required_names = []
            for required_name in required_names:
                # A readiness gate already performs the same authoritative
                # validation before returning its blocked receipt.  Do not
                # replace that useful blocking result with a second generic
                # validation response.
                if required_name == "validate_order" and any(
                        isinstance(item.get("result"), dict)
                        and item.get("result", {}).get("status") == "blocked"
                        and item.get("result", {}).get("reason") == "order_not_ready"
                        for item in self._planner_tool_receipts):
                    continue
                if self._planner_tool_satisfied(required_name):
                    continue
                latest_required = self._planner_latest_receipt(required_name)
                retryable_failure = bool(
                    latest_required and not latest_required.get("stale")
                    and latest_required.get("status") in {
                        "error", "invalid_arguments", "duplicate_blocked",
                    }
                )
                # A deterministic blocked result is already authoritative.  A
                # malformed/error attempt is not: retry it once with canonical
                # local arguments below.
                if (latest_required and not latest_required.get("stale")
                        and not retryable_failure):
                    continue
                forced_arguments: dict[str, Any] = {}
                if required_name == "explain_print_term":
                    forced_arguments = {"question": text}
                elif required_name in {
                        "estimate_price", "recommend_processes", "prepare_handoff",
                        "match_supplier_capability", "request_supplier_quote"}:
                    active_index = self._item_index()
                    if active_index is not None:
                        forced_arguments = {"itemIndex": active_index}
                if required_name in {
                        "request_supplier_quote", "match_supplier_capability", "prepare_handoff"}:
                    platform = self.state["order"].get("platform")
                    if platform:
                        forced_arguments["platformId"] = platform
                self._event("plan", "fallback_tool", "模型未请求必要工具，按本地意图执行",
                            tool=required_name)
                forced_signature = self._planner_signature(required_name, forced_arguments)
                if forced_signature in seen_signatures and not retryable_failure:
                    self._event("plan", "duplicate_blocked", "必要工具调用与本轮已有状态重复",
                                tool=required_name)
                    continue
                seen_signatures.add(forced_signature)
                forced_response, forced_receipt = self._planner_dispatch_tool(
                    required_name, forced_arguments, round_number=tool_call_count + 1,
                    signature=forced_signature)
                tool_call_count += 1
                last_tool_name, last_tool_response, last_receipt = required_name, forced_response, forced_receipt
                forced_names.append(required_name)
                self._mark_planner_receipts_stale()

            # A forced recovery call happens after the provider's earlier reply,
            # so that reply cannot be considered grounded in the new results.
            # Give capable adapters one tools-disabled turn over the complete
            # bounded ledger.  Older/custom planners may return None, in which
            # case the deterministic ledger summary below remains authoritative.
            if forced_names:
                self.trace.append(f"调用模型最终总结：{getattr(self.planner, 'model', '已配置模型')}")
                synthesis = self._ask_planner(
                    text,
                    {"name": last_tool_name,
                     "result": (last_tool_response or {}).get("toolResult")},
                    tool_call=None, tool_calls=None,
                    tool_ledger=self._planner_tool_receipts,
                    allow_tools=False, synthesis=True,
                )
                if isinstance(synthesis, dict):
                    if synthesis.get("patch"):
                        self._event("plan", "patch_ignored", "最终总结中的字段更新未应用")
                    if synthesis.get("reply"):
                        final_reply = str(synthesis["reply"])
                        post_force_synthesized = True

            if getattr(self.planner, "last_error", "") and not any(
                    event.get("step") == "plan" and event.get("status") == "fallback"
                    for event in self.run_events):
                self.trace.append(f"模型回退：{self.planner.last_error}")
                self._event("plan", "fallback", self.planner.last_error)

            if last_tool_response is not None:
                result = last_tool_response.get("toolResult")
                stale_latest = bool(last_receipt and last_receipt.get("stale"))
                forced_latest = [self._planner_latest_receipt(name) for name in forced_names]
                forced_failure = any(
                    receipt and not receipt.get("stale") and receipt.get("status") in {
                        "blocked", "error", "invalid_arguments", "duplicate_blocked",
                    }
                    for receipt in forced_latest
                )
                if final_reply.strip() and (not forced_names or post_force_synthesized) \
                        and not stale_latest and not forced_failure:
                    message = final_reply.strip()
                else:
                    message = self._planner_receipts_fallback()
                self._remember("assistant", message)
                self._save()
                ledger_options, ledger_handoff = self._planner_artifacts()
                options = ledger_options or last_tool_response.get("options", [])
                handoff_value = ledger_handoff or last_tool_response.get("handoff")
                if isinstance(handoff_value, dict) and handoff_value.get("status") != "ready":
                    handoff_value = None
                return self._result(
                    [message], options=options,
                    handoff=handoff_value, tool_result=result,
                )
            if final_reply:
                self.state["stage"] = "collect" if self._missing_fields() else "recommend"
                self._remember("assistant", final_reply)
                self._save()
                return self._result([final_reply])
            if getattr(self.planner, "last_error", ""):
                self.trace.append(f"模型回退：{self.planner.last_error}")
                self._event("plan", "fallback", self.planner.last_error)

        if self._is_explanation_request(text):
            result = self._call("explain_print_term", text)
            message = f"{result['topic']}：{result['answer']}\n\n{result['next']}"
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)

        # Explicit intents let users invoke a tool before the order is complete.
        # Supplier-quote language wins over the generic estimate keywords.
        if self._planner_intent(text, "quote"):
            active_index = self._item_index()
            result = self._prepare_quote_request(active_index, self.state["order"].get("platform"))
            message = result.get("message", "已准备询价请求，正式发送前需要人工确认。")
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if self._planner_intent(text, "price"):
            active_index = self._item_index()
            item_order = self._item_order(active_index) if active_index is not None else None
            result = self._call("estimate_price", item_order or self.state["order"])
            if item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": active_index}
            if result.get("status") == "blocked":
                message = result["assumptions"]
            else:
                message = (f"按当前已填写信息，费用只能做区间估算：{result['range']}。\n{result['assumptions']}"
                           if result["range"] else f"我调用了费用估算工具，但信息还不足。\n还需要：{'、'.join(result['missing'])}。")
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if self._planner_intent(text, "capability"):
            platform = self.state["order"].get("platform")
            active_index = self._item_index()
            item_order = self._item_order(active_index) if active_index is not None else None
            result = self._call("match_supplier_capability", item_order or self.state["order"], platform)
            if active_index is not None and item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": active_index}
            message = "已完成供应商能力匹配，请查看支持项与待确认项。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if self._planner_intent(text, "validation"):
            result = self._call("validate_order", self.state["order"])
            message = self._validation_message(result)
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if self._planner_intent(text, "handoff"):
            self._save()
            return self.generate()

        missing = self._missing_fields()
        if missing:
            self.state["stage"] = "collect"
            validation = self._call("validate_order", self.state["order"])
            message = f"{self._summary()}\n\n还需要确认：{self._question(missing[0])}"
            quick = self._quick_replies(missing[0], self.state["order"].get("productType"))
            options: list[dict[str, str]] = []
            tool_result: Any = validation
        else:
            self.state["stage"] = "recommend"
            options = self._call("recommend_processes", self.state["order"])
            self._store_process_options(options)
            profile = parameter_state(self.state["order"])
            quick = self._product_quick_replies(profile["missing"][0]["key"]) if profile["missing"] else []
            product_note = (f"基础订单信息已经齐了。为了让{profile.get('category', '该品类')}对接更准确，建议补充：{profile['missing'][0]['question']}"
                            if profile["missing"] else "信息已经齐了。")
            message = f"{self._summary()}\n\n{product_note}\n我调用工艺推荐工具生成了 3 个可执行方案，请选择一个。"
            tool_result = options
        self._remember("assistant", message)
        self._save()
        return self._result([message], quick, options, tool_result=tool_result)

    def call_tool(self, name: str, payload: dict[str, Any] | None = None, preserve_trace: bool = False,
                  remember: bool = True) -> dict[str, Any]:
        """Public tool gateway used by a UI, MCP bridge, or an LLM planner."""
        valid_name = isinstance(name, str) and bool(name.strip()) and len(name.strip()) <= MAX_TOOL_NAME_LENGTH
        if not valid_name:
            if not preserve_trace:
                self._begin_run("tool:invalid")
                self.trace = []
            self.trace.append("工具名无效")
            self._event("tool", "rejected", "工具名必须是有限长度的非空字符串", tool="invalid")
            return self._tool_reply("工具名无效，未执行。", remember=remember,
                                    tool_result={"status": "invalid_arguments", "reason": "tool_name"})
        name = name.strip()
        if not preserve_trace:
            self._begin_run(f"tool:{name}")
            self.trace = []
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            self.trace.append(f"工具参数无效：{name}")
            self._event("tool", "rejected", f"工具 {name} 参数不是 JSON 对象", tool=name)
            return self._tool_reply("工具参数需要使用 JSON 对象，未执行。", remember=remember)
        payload = dict(payload)
        payload_error = self._validate_call_tool_payload(name, payload)
        if payload_error:
            self.trace.append(f"工具参数无效：{name}")
            self._event("tool", "rejected", payload_error, tool=name)
            return self._tool_reply(payload_error + "，未执行。", remember=remember,
                                    tool_result={"status": "invalid_arguments", "reason": payload_error})
        # Match MCP's normalization so a valid value padded by a transport
        # does not silently select a different platform or get persisted as a
        # path-looking filename.
        for key in ("platformId", "question", "fileName", "expectedSize"):
            if isinstance(payload.get(key), str):
                payload[key] = payload[key].strip()
        # ``order`` is a legacy provider-neutral argument.  The gateway always
        # resolves the authoritative order from this session, so never forward
        # or persist the caller-supplied copy.
        payload.pop("order", None)
        if (name in TOOL_SCHEMAS and payload.get("itemIndex") is not None):
            explicit_item_index = self._item_index(payload.get("itemIndex"))
            if explicit_item_index is not None:
                self.state["activeItemIndex"] = explicit_item_index
        # High-risk public tools must use the same readiness decision as MCP.
        # Internal ``_call`` invocations intentionally do not pass through this
        # branch; they are made only after the surrounding workflow validates
        # the order and avoids recursive tool dispatch.
        if name in self.READINESS_TOOLS:
            item_index = self._item_index(payload.get("itemIndex"))
            blocked = self._tool_precondition(name, item_index)
            if blocked is not None:
                blocked_result = blocked.get("toolResult")
                if not isinstance(blocked_result, dict):
                    blocked_result = {"status": "blocked", "reason": "order_not_ready"}
                blocked_stage = blocked.get("workflowStage")
                if blocked_stage in WORKFLOW_LABELS:
                    self.state["workflowStage"] = blocked_stage
                reason = blocked_result.get("reason")
                if reason == "low_confidence":
                    fields = "、".join(str(field) for field in blocked_result.get("uncertain", []))
                    message = f"存在低置信度生产字段，请先确认：{fields or '订单字段'}。"
                elif reason == "selection_required":
                    action = {
                        "request_supplier_quote": "询价",
                        "recommend_processes": "生成工艺方案",
                    }.get(name, "生成交接单")
                    message = f"请先选择工艺方案，再{action}。"
                else:
                    missing = "、".join(str(field) for field in blocked_result.get("missing", []))
                    action = {
                        "request_supplier_quote": "询价",
                        "recommend_processes": "生成工艺方案",
                    }.get(name, "生成交接单")
                    message = (f"当前信息还不完整，暂不能{action}。请先补充：{missing}。"
                               if missing else f"当前信息还不完整，暂不能{action}。")
                self.trace.append(f"工具阻断：{name}")
                self._event("tool", "blocked", message, tool=name,
                            reason=str(reason or "precondition"))
                response = self._tool_reply(message, remember=remember,
                                            tool_result=deepcopy(blocked_result))
                # ``_result`` normally derives these fields from global state;
                # a selected item or a low-confidence gate can require the
                # more precise MCP decision in the public response.
                for key in ("validation", "workflowStage", "confirmation", "decision"):
                    if key in blocked:
                        response[key] = deepcopy(blocked[key])
                if isinstance(blocked_stage, str) and blocked_stage in WORKFLOW_LABELS:
                    response["workflowLabel"] = WORKFLOW_LABELS[blocked_stage]
                return response
        if name == "recommend_processes":
            multi_product = _multi_product_info(self.state["order"])
            if multi_product:
                item_index = self._item_index(payload.get("itemIndex"))
                item_order = self._item_order(item_index) if item_index is not None else None
                item_validation = self._item_validation(item_index) if item_index is not None else None
                if item_order is None or not item_validation or not item_validation.get("ok"):
                    label = "、".join(multi_product) if len(multi_product) > 1 else "多个订单项"
                    self.state["stage"] = "collect"
                    self.state["workflowStage"] = "clarify"
                    return self._tool_reply(
                        f"当前订单包含{label}，请先选择一个产品项并补齐该项字段，不能生成合并工艺方案。",
                        tool_result={"status": "blocked", "reason": "multi_product", "multiProduct": multi_product,
                                     "itemValidation": item_validation},
                        remember=remember,
                    )
                result = self._recommend_item(item_index)
                self.state["stage"] = "recommend"
                return self._tool_reply(
                    f"已为第 {item_index + 1} 项生成工艺方案。", options=result,
                    tool_result={"status": "ready", "itemId": item_order.get("itemId"),
                                 "itemIndex": item_index, "options": result}, remember=remember)
            result = self._call(name, self.state["order"])
            self._store_process_options(result)
            self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
            return self._tool_reply("已调用工艺推荐工具。", options=result, tool_result=result, remember=remember)
        if name == "validate_order":
            result = self._call(name, self.state["order"])
            return self._tool_reply(self._validation_message(result), tool_result=result, remember=remember)
        if name == "explain_print_term":
            result = self._call(name, str(payload.get("question", "印刷工艺怎么选？")))
            message = f"{result['topic']}：{result['answer']}\n\n{result['next']}"
            return self._tool_reply(message, tool_result=result, remember=remember)
        if name == "estimate_price":
            item_index = self._item_index(payload.get("itemIndex"))
            item_order = self._item_order(item_index) if item_index is not None else None
            result = self._call(name, item_order or self.state["order"])
            if item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            self.state["workflowStage"] = "clarify" if result.get("status") == "blocked" else "quote"
            message = (result.get("assumptions") if result.get("status") == "blocked"
                       else f"已调用费用估算工具：{result['range']}。" if result["range"]
                       else f"费用估算工具需要更多信息：{'、'.join(result['missing'])}。")
            return self._tool_reply(message, tool_result=result, remember=remember)
        if name == "prepare_handoff":
            item_index = self._item_index(payload.get("itemIndex"))
            item_order = self._item_order(item_index) if item_index is not None else None
            handoff_order = deepcopy(item_order or self.state["order"])
            # ``platformId`` is a per-call target override.  It is useful when
            # the model has just matched an alternative supplier, but it must
            # never silently rewrite the session's selected platform.
            if payload.get("platformId"):
                handoff_order["platform"] = payload["platformId"]
            result = self._call(name, handoff_order)
            if item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            message = (result.get("text") if result.get("status") == "blocked"
                       else "已调用订单交接工具，生成平台适配文本。")
            # A capability rejection is a tool result, not a deliverable
            # handoff.  Keeping it out of the handoff slot prevents the UI,
            # HTTP bridge and follow-up planner from treating a blocked draft
            # as exportable order data.
            handoff = result if result.get("status") == "ready" else None
            return self._tool_reply(message, tool_result=result, handoff=handoff, remember=remember)
        if name == "match_supplier_capability":
            platform_id = payload.get("platformId") or self.state["order"].get("platform")
            item_index = self._item_index(payload.get("itemIndex"))
            item_order = self._item_order(item_index) if item_index is not None else None
            result = self._call(name, item_order or self.state["order"], str(platform_id) if platform_id else None)
            if item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            return self._tool_reply("已完成供应商能力匹配，请查看支持项与待确认项。", tool_result=result, remember=remember)
        if name == "request_supplier_quote":
            platform_id = payload.get("platformId") or self.state["order"].get("platform")
            item_index = self._item_index(payload.get("itemIndex"))
            result = self._prepare_quote_request(item_index, str(platform_id) if platform_id else None)
            message = result.get("message", "已准备询价请求，正式发送前需要人工确认。")
            return self._tool_reply(message, tool_result=result, remember=remember)
        if name == "preflight_file":
            try:
                size_bytes = int(payload.get("sizeBytes", 0))
            except (TypeError, ValueError):
                size_bytes = 0
            page_count = payload.get("pageCount")
            try:
                page_count = int(page_count) if page_count is not None else None
            except (TypeError, ValueError):
                page_count = None
            has_multi = isinstance(self.state["order"].get("items"), list) and len(self.state["order"]["items"]) > 1
            item_index = self._item_index(payload.get("itemIndex"))
            if has_multi and item_index is None:
                return self._tool_reply("当前订单包含多个产品项，请先选择具体产品项再做文件预检。",
                                        tool_result={"status": "blocked", "reason": "item_required"}, remember=remember)
            item_order = self._item_order(item_index) if item_index is not None else self.state["order"]
            result = self._call(name, str(payload.get("fileName", "")), size_bytes, page_count,
                                payload.get("encrypted") is True, payload.get("readable") is not False,
                                payload.get("inspection"), payload.get("expectedSize") or item_order.get("size"))
            self.state["workflowStage"] = "preflight"
            self._store_preflight_result(result, item_index)
            if result.get("ok"):
                self._bind_uploaded_file(str(payload.get("fileName", "")), item_index)
                self._invalidate_delivery_state()
            if item_index is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            return self._tool_reply(result["message"], tool_result=result, remember=remember)
        self._event("tool", "rejected", f"工具 {name} 不在白名单中", tool=name)
        return self._tool_reply(f"工具 {name} 不在白名单中，未执行。", remember=remember)

    def choose(self, option_id: str, item_index: int | None = None) -> dict[str, Any]:
        self._begin_run("choose")
        self.trace = []
        validation = self._call("validate_order", self.state["order"])
        if validation.get("multiProduct"):
            index = self._item_index(item_index)
            if item_index is not None and index is not None:
                self.state["activeItemIndex"] = index
            item_order = self._item_order(index) if index is not None else None
            item_validation = self._item_validation(index) if index is not None else None
            if item_order is None or not item_validation or not item_validation.get("ok"):
                return self._result(["还不能选择方案。请先选择一个产品项，并补齐该项的基础字段和品类参数。"], tool_result=item_validation or validation)
            item_id = item_order.get("itemId") or f"item-{index + 1}"
            options = self._context_options(self.state, item_order)
            if not options:
                options = self._recommend_item(index)
            option = next((item for item in options if item["id"] == option_id), None)
            if not option:
                return self._result(["没有找到这个产品项的方案。"], [], options)
            updates = {"finishing": option["finishing"], "binding": option.get("binding", item_order.get("binding", ""))}
            if item_order.get("productType") not in MATERIAL_SPEC_PRODUCTS:
                updates["paper"] = option["paper"]
            self._update_item(index, updates, source="recommendation", confidence=0.9)
            self.state["order"]["items"][index]["selectedOption"] = option_id
            self._store_process_options(options, index)
            all_selected = all(item.get("selectedOption") for item in self.state["order"].get("items", []) if isinstance(item, dict))
            self.state["workflowStage"] = "confirm" if all_selected else "clarify"
            message = f"已为第 {index + 1} 项选择{option['title']}，该项参数已更新。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], [], options, tool_result={"status": "selected", "itemId": item_id,
                                                                       "itemIndex": index, "option": option})
        if not validation["ok"]:
            return self._result([f"还不能选择方案。{self._validation_message(validation)}"], tool_result=validation)
        options = self._context_options(self.state)
        if not options:
            options = self._call("recommend_processes", self.state["order"])
            self._store_process_options(options)
        option = next((item for item in options if item["id"] == option_id), None)
        if not option:
            return self._result(["没有找到这个方案。"], [], options)
        updates = {"finishing": option["finishing"], "binding": option.get("binding", self.state["order"]["binding"])}
        if self.state["order"].get("productType") not in MATERIAL_SPEC_PRODUCTS:
            updates["paper"] = option["paper"]
        self._update_order(updates, source="recommendation", confidence=0.9)
        self.state["selectedOption"] = option_id
        self._store_process_options(options)
        self.state["workflowStage"] = "confirm"
        message = f"已选择{option['title']}，订单参数已更新。"
        self._remember("assistant", message)
        self._save()
        return self._result([message], [], options)

    def generate(self) -> dict[str, Any]:
        self._begin_run("generate")
        self.trace = []
        if self.state["orderGenerated"]:
            message = ("订单交接单已经确认，可继续导出或交给受控平台适配器。"
                       if (self.state.get("confirmation") or {}).get("status") == "confirmed"
                       else "订单草稿已经生成，正式提交前请确认文件、价格和交期。")
            return self._result([message], handoff=self.state.get("handoff"))
        validation = self._call("validate_order", self.state["order"])
        if validation.get("multiProduct"):
            item_validations = validation.get("itemValidations") or []
            pending = [item for item in item_validations if not item.get("ok")]
            if pending:
                details = []
                for item in pending:
                    missing = list(item.get("missing") or []) + list(item.get("productMissing") or [])
                    item_name = f"（{item.get('productType')}）" if item.get("productType") else ""
                    details.append(f"第 {item.get('index', 0) + 1} 项{item_name}：{'、'.join(missing) or '请检查风险'}")
                message = "订单还不能生成。请先逐项补齐：" + "；".join(details) + "。"
                self._remember("assistant", message)
                self._save()
                return self._result([message], tool_result=validation)
            uncertain = [field for field in self._low_confidence_fields(production_only=True) if field.startswith("items.")]
            if uncertain:
                self._event("approval", "blocked", "产品项存在低置信度字段", fields=uncertain)
                labels = [LABELS.get(field.rsplit(".", 1)[-1], field) for field in uncertain]
                message = f"订单还不能生成。请先确认产品项字段：{'、'.join(labels)}。"
                self._remember("assistant", message)
                self._save()
                return self._result([message], tool_result={"ok": False, "uncertain": uncertain})
            unselected = [item for item in self.state["order"].get("items", [])
                          if isinstance(item, dict) and not item.get("selectedOption")]
            if unselected:
                active_index = self._item_index()
                options: list[dict[str, str]] = []
                if active_index is not None:
                    current = next((item for item in item_validations if item.get("index") == active_index), None)
                    if current and current.get("ok"):
                        options = self._recommend_item(active_index)
                message = "请为每个产品项分别选择工艺方案后，再生成整体交接单。"
                self._remember("assistant", message)
                self._save()
                return self._result([message], options=options, tool_result={"status": "needs_selection",
                                                                                "items": unselected})
            capabilities = []
            handoffs = []
            unsupported: list[str] = []
            for index, item in enumerate(self.state["order"].get("items", [])):
                item_order = self._item_order(index)
                if item_order is None:
                    continue
                capability = self._call("match_supplier_capability", item_order)
                capabilities.append({"itemId": item_order.get("itemId"), "itemIndex": index, **capability})
                unsupported.extend(f"第 {index + 1} 项：{entry.get('field', '能力')}" for entry in capability.get("unsupported", []))
                if not capability.get("unsupported"):
                    handoffs.append(self._call("prepare_handoff", item_order))
            if unsupported:
                message = f"订单还不能生成。目标平台存在不支持项：{'、'.join(unsupported)}。请切换平台或先向供应商确认。"
                self._event("capability", "blocked", "产品项供应商能力不匹配", fields=unsupported)
                self._remember("assistant", message)
                self._save()
                return self._result([message], tool_result={"status": "blocked", "unsupported": unsupported,
                                                             "items": capabilities})
            sections = []
            for index, handoff in enumerate(handoffs):
                product = self.state["order"]["items"][index].get("productType") or f"产品项 {index + 1}"
                sections.append(f"【第 {index + 1} 项：{product}】\n{handoff.get('text', '')}")
                self.state["order"]["items"][index]["orderGenerated"] = True
            aggregate = {"status": "ready", "items": handoffs, "supplierReadiness": capabilities,
                         "text": "\n\n".join(sections), "requiresHumanConfirmation": True}
            self.state.update({"stage": "confirm", "workflowStage": "confirm", "orderGenerated": True,
                               "handoff": deepcopy(aggregate), "confirmation": {"status": "pending"}})
            message = "所有产品项已完成并生成整体交接单。正式提交前仍需要人工确认价格、文件和交期。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], [], [], handoff=aggregate, tool_result=aggregate)
        if not validation["ok"]:
            message = f"订单还不能生成。{self._validation_message(validation)}"
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=validation)
        if validation.get("productMissing"):
            missing = "、".join(validation["productMissing"])
            message = f"订单还不能生成。{self.state['order']['productType']}还缺少品类参数：{missing}。先补充后再生成交接单。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=validation)
        uncertain = self._low_confidence_fields(production_only=True)
        if uncertain:
            labels = [LABELS.get(key, key) for key in uncertain]
            message = f"订单还不能生成。请先确认低置信度字段：{'、'.join(labels)}。确认后再生成订单草稿。"
            self._event("approval", "blocked", "存在低置信度字段", fields=uncertain)
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result={"ok": False, "uncertain": uncertain})
        capability = match_supplier_capability(self.state["order"])
        if capability.get("unsupported"):
            fields = [item["field"] for item in capability["unsupported"]]
            message = f"订单还不能生成。目标平台暂不支持或未登记：{'、'.join(fields)}。请切换平台或先向供应商确认。"
            self._event("capability", "blocked", "供应商能力不匹配", fields=fields)
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=capability)
        if not self.state["selectedOption"]:
            options = self._call("recommend_processes", self.state["order"])
            message = "请先选择工艺方案，再生成订单草稿。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], [], options, tool_result=options)
        handoff = self._call("prepare_handoff", self.state["order"])
        self.state.update({"stage": "confirm", "workflowStage": "confirm", "orderGenerated": True,
                           "handoff": deepcopy(handoff), "confirmation": {"status": "pending"}})
        message = "订单草稿已生成。正式提交前仍需要人工确认价格、文件和交期。"
        self._remember("assistant", message)
        self._save()
        return self._result([message], [], [], handoff)

    def confirm(self, note: str = "") -> dict[str, Any]:
        """Persist an explicit human approval without submitting externally."""
        self._begin_run("confirm")
        self.trace = []
        if not self.state.get("orderGenerated") or not self.state.get("handoff"):
            message = "当前没有可确认的订单交接单，请先完成订单并生成草稿。"
            self._event("approval", "blocked", "没有可确认的交接单")
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result={"status": "blocked", "reason": "handoff_not_ready"})
        confirmation = self.state.get("confirmation") or {}
        if confirmation.get("status") == "confirmed":
            return self._result(["该订单交接单已经确认，无需重复确认。"], handoff=self.state.get("handoff"),
                                tool_result={"status": "confirmed", **confirmation})
        confirmed_at = self._timestamp()
        self.state["confirmation"] = {"status": "confirmed", "confirmedAt": confirmed_at,
                                       "note": str(note or "").strip()[:240]}
        self.state["workflowStage"] = "export"
        self.state["stage"] = "confirm"
        self._event("approval", "ok", "人工确认已记录", confirmedAt=confirmed_at)
        quote_request = self._quote_request()
        if quote_request and quote_request.get("status") == "awaiting_human_confirmation":
            quote_request["status"] = "confirmed"
            quote_request["updatedAt"] = confirmed_at
            quote_request["confirmedAt"] = confirmed_at
            quote_request["confirmationNote"] = str(note or "").strip()[:240]
        self._remember("assistant", "已记录人工确认。当前不会自动向供应商提交，下一步可导出交接包或由受控适配器继续处理。")
        self._save()
        return self._result(["已记录人工确认。当前不会自动向供应商提交，下一步可导出交接包或由受控适配器继续处理。"],
                            handoff=self.state.get("handoff"),
                            tool_result={"status": "confirmed", **self.state["confirmation"]})

    def quote_status(self, request_id: str | None = None) -> dict[str, Any]:
        """Return a persisted quote request without performing any external call."""
        self._begin_run("quote_status")
        self.trace = []
        request = self._quote_request(str(request_id).strip() if request_id else None)
        if request is None:
            message = "当前没有找到询价请求。"
            result = {"status": "not_found", "requestId": request_id}
        else:
            status_labels = {
                "awaiting_human_confirmation": "待人工确认",
                "confirmed": "已确认，等待受控适配器提交",
                "cancelled": "已取消", "stale": "已失效",
                "submitted": "已提交", "failed": "提交失败",
            }
            message = f"询价请求 {request['requestId']} 当前状态：{status_labels.get(request.get('status'), request.get('status', '未知'))}。"
            result = deepcopy(request)
        self._remember("assistant", message)
        self._save()
        return self._result([message], tool_result=result)

    def cancel_quote(self, request_id: str | None = None, reason: str = "用户取消询价") -> dict[str, Any]:
        """Cancel a pending quote request locally; never contact a supplier."""
        self._begin_run("quote_cancel")
        self.trace = []
        request = self._quote_request(str(request_id).strip() if request_id else None)
        if request is None:
            message = "当前没有可取消的询价请求。"
            result = {"status": "not_found", "requestId": request_id}
        elif request.get("status") == "cancelled":
            message = "该询价请求已经取消，无需重复操作。"
            result = deepcopy(request)
            result["idempotent"] = True
        elif request.get("status") not in QUOTE_ACTIVE_STATUSES:
            message = f"该询价请求当前为“{request.get('status', '未知')}”，不能取消。"
            result = {"status": "not_cancellable", "request": deepcopy(request)}
        else:
            now = self._timestamp()
            request["status"] = "cancelled"
            request["updatedAt"] = now
            request["cancelledAt"] = now
            request["cancelReason"] = str(reason or "用户取消询价").strip()[:240]
            if self.state.get("activeQuoteRequestId") == request.get("requestId"):
                self.state["activeQuoteRequestId"] = None
            self.state["workflowStage"] = self._base_workflow_stage()
            message = f"询价请求 {request['requestId']} 已取消，不会向供应商发送。"
            result = deepcopy(request)
        self._remember("assistant", message)
        self._save()
        return self._result([message], tool_result=result)

    def upload(self, file_name: str, size_bytes: int, page_count: int | None = None,
               encrypted: bool = False, readable: bool = True,
               inspection: dict[str, Any] | None = None,
               expected_size: str | None = None,
               item_index: int | None = None) -> dict[str, Any]:
        self._begin_run("preflight")
        self.trace = []
        items = self.state["order"].get("items")
        if isinstance(items, list) and len(items) > 1:
            item_index = self._item_index(item_index)
            if item_index is None:
                message = "当前订单包含多个产品项，请先处理具体产品项，再上传对应 PDF。"
                self._event("preflight", "blocked", "多产品上传缺少产品项")
                self._remember("assistant", message)
                self._save()
                return self._result([message], tool_result={"status": "blocked", "reason": "item_required"})
            item_order = self._item_order(item_index) or self.state["order"]
        else:
            item_order = self.state["order"]
        check = self._call("preflight_file", file_name, size_bytes, page_count, encrypted, readable, inspection,
                           expected_size or item_order.get("size"))
        self.state["workflowStage"] = "preflight"
        self._store_preflight_result(check, item_index)
        if check["ok"]:
            self._bind_uploaded_file(file_name, item_index)
            self._invalidate_delivery_state()
            self._save()
        if item_index is not None:
            check = {**check, "itemId": item_order.get("itemId"), "itemIndex": item_index}
        return self._result([check["message"]], tool_result=check)

    def set_platform(self, platform_id: str) -> dict[str, Any]:
        self._begin_run("set_platform")
        platform_id = platform_id if platform_id in PLATFORMS else "generic"
        self._update_order({"platform": platform_id}, source="user", confidence=1.0)
        self.state["workflowStage"] = self._base_workflow_stage()
        self._save()
        return self._result([f"目标平台已切换为{PLATFORMS[platform_id]['name']}。订单核心字段保持不变。"])

    def snapshot(self) -> dict[str, Any]:
        self._begin_run("snapshot")
        options: list[dict[str, str]] = []
        active_index = self._item_index()
        if active_index is not None:
            item = self._item_order(active_index) or {}
            options = self._context_options(self.state, item)
        elif self.state["stage"] != "collect":
            options = self._context_options(self.state)
            if not options:
                options = self._call("recommend_processes", self.state["order"])
                self._store_process_options(options)
        self.trace = ["恢复会话记忆"] if self.state["messages"] else []
        return self._result([], [], options)

    @staticmethod
    def _context_order_projection(order: dict[str, Any] | None,
                                  *, include_items: bool = True) -> dict[str, Any]:
        """Project only production fields needed to continue a conversation.

        This boundary is intentionally independent from ``_result``.  The
        normal browser response is user-facing and may contain history and
        diagnostics; an MCP/model context snapshot must stay small and must
        never accidentally grow a secret, file payload, or absolute path.
        """
        if not isinstance(order, dict):
            return {}
        scalar_keys = (
            "productType", "purpose", "quantity", "quantityValue", "quantityUnit",
            "size", "pages", "orientation", "paper", "printing", "finishing",
            "binding", "deadline", "budget", "platform",
        )
        projected: dict[str, Any] = {}
        for key in scalar_keys:
            value = order.get(key)
            if value not in (None, ""):
                projected[key] = deepcopy(value)
        dimensions = order.get("dimensions")
        if isinstance(dimensions, dict):
            clean_dimensions = {
                key: deepcopy(value) for key, value in dimensions.items()
                if key in DIMENSION_DEFAULTS and value not in (None, "")
            }
            if clean_dimensions:
                projected["dimensions"] = clean_dimensions
        specs = order.get("productSpecs")
        if isinstance(specs, dict):
            clean_specs = {
                str(key)[:128]: deepcopy(value) for key, value in specs.items()
                if isinstance(key, str) and value not in (None, "")
                and Agent._is_patch_scalar(value)
            }
            if clean_specs:
                projected["productSpecs"] = clean_specs
        if include_items:
            raw_items = order.get("items")
            if isinstance(raw_items, list) and len(raw_items) > 1:
                item_summaries: list[dict[str, Any]] = []
                for raw_item in raw_items[:32]:
                    if not isinstance(raw_item, dict):
                        continue
                    item = Agent._context_order_projection(raw_item, include_items=False)
                    item_id = raw_item.get("itemId")
                    if isinstance(item_id, str) and item_id:
                        item["itemId"] = item_id[:128]
                    selected = raw_item.get("selectedOption")
                    if isinstance(selected, str) and selected:
                        item["selectedOption"] = selected[:128]
                    if isinstance(raw_item.get("orderGenerated"), bool):
                        item["orderGenerated"] = raw_item["orderGenerated"]
                    item_summaries.append(item)
                if item_summaries:
                    projected["items"] = item_summaries
        return projected

    @staticmethod
    def _context_validation_summary(validation: dict[str, Any] | None) -> dict[str, Any]:
        """Keep validation useful while dropping duplicate profile payloads."""
        if not isinstance(validation, dict):
            return {"ok": False, "missing": [], "productMissing": [], "warnings": [], "risks": []}
        result: dict[str, Any] = {
            "ok": bool(validation.get("ok")),
            "missing": [str(item)[:256] for item in (validation.get("missing") or [])
                        if isinstance(item, (str, int, float))][:32],
            "productMissing": [str(item)[:256] for item in (validation.get("productMissing") or [])
                               if isinstance(item, (str, int, float))][:32],
            "warnings": [str(item)[:512] for item in (validation.get("warnings") or [])
                         if isinstance(item, (str, int, float))][:32],
            "risks": [str(item)[:512] for item in (validation.get("risks") or [])
                      if isinstance(item, (str, int, float))][:32],
        }
        for key in ("readiness", "productReadiness"):
            value = validation.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                result[key] = value
        if isinstance(validation.get("multiProduct"), bool):
            result["multiProduct"] = validation["multiProduct"]
        item_validations = validation.get("itemValidations")
        if isinstance(item_validations, list):
            summaries: list[dict[str, Any]] = []
            for raw in item_validations[:32]:
                if not isinstance(raw, dict):
                    continue
                item: dict[str, Any] = {}
                for key in ("itemId", "index", "productType", "ok", "readiness", "productReadiness"):
                    value = raw.get(key)
                    if key == "ok" and isinstance(value, bool):
                        item[key] = value
                    elif key in {"index", "readiness", "productReadiness"} and isinstance(value, (int, float)) \
                            and not isinstance(value, bool) and math.isfinite(float(value)):
                        item[key] = value
                    elif key in {"itemId", "productType"} and isinstance(value, str) and value:
                        item[key] = value[:128]
                for key in ("missing", "productMissing", "warnings", "risks"):
                    values = raw.get(key)
                    if isinstance(values, list):
                        item[key] = [str(value)[:256] for value in values
                                     if isinstance(value, (str, int, float))][:16]
                summaries.append(item)
            if summaries:
                result["itemValidations"] = summaries
        return result

    @staticmethod
    def _context_options(state: dict[str, Any], item: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Read cached process options without recomputing a recommendation."""
        item_id = item.get("itemId") if isinstance(item, dict) else None
        maps = state.get("processOptions")
        if isinstance(maps, dict) and item_id:
            value = maps.get(item_id)
            if isinstance(value, list):
                return deepcopy(value[:16])
        legacy = state.get("itemOptions")
        if isinstance(legacy, dict) and item_id:
            value = legacy.get(item_id)
            if isinstance(value, list):
                return deepcopy(value[:16])
        if isinstance(maps, list):
            return deepcopy(maps[:16])
        if isinstance(legacy, list):
            return deepcopy(legacy[:16])
        return []

    @staticmethod
    def _context_preflight(state: dict[str, Any], item: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return a metadata-only preflight result for the selected item."""
        item_id = item.get("itemId") if isinstance(item, dict) else None
        stored = state.get("preflightResults")
        value: Any = None
        if isinstance(stored, dict) and item_id and isinstance(stored.get(item_id), dict):
            value = stored.get(item_id)
        elif isinstance(stored, list):
            # A list is accepted for forward compatibility with clients that
            # keep one result per product item.
            for entry in stored:
                if isinstance(entry, dict) and (not item_id or entry.get("itemId") == item_id):
                    value = entry
                    break
        elif isinstance(stored, dict) and ("status" in stored or "ok" in stored):
            value = stored
        if not isinstance(value, dict):
            return None
        allowed = ("status", "ok", "fileName", "sizeBytes", "pageCount", "encrypted", "readable",
                   "inspectionLevel", "warnings", "suggestions", "checks", "stale", "staleReason",
                   "itemId", "itemIndex", "updatedAt")
        result: dict[str, Any] = {}
        for key in allowed:
            raw = value.get(key)
            if key in {"warnings", "suggestions"} and isinstance(raw, list):
                result[key] = [str(item)[:512] for item in raw if isinstance(item, (str, int, float))][:32]
            elif key == "checks" and isinstance(raw, list):
                checks: list[dict[str, Any]] = []
                for check in raw[:32]:
                    if not isinstance(check, dict):
                        continue
                    entry = {name: deepcopy(check[name]) for name in ("label", "status", "detail")
                             if name in check and isinstance(check[name], (str, int, float, bool))}
                    if entry:
                        checks.append(entry)
                result[key] = checks
            elif key == "fileName" and isinstance(raw, str):
                # Only a basename is ever exposed, even if an old state was
                # written by a caller that accidentally supplied a path.
                result[key] = Path(raw.replace("\\", "/")).name[:255]
            elif key in {"status", "inspectionLevel", "staleReason", "updatedAt"} and isinstance(raw, str):
                result[key] = raw[:512]
            elif key in {"ok", "encrypted", "readable", "stale"} and isinstance(raw, bool):
                result[key] = raw
            elif key in {"sizeBytes", "pageCount", "itemIndex"} and isinstance(raw, int) and not isinstance(raw, bool):
                result[key] = raw
            elif key == "itemId" and isinstance(raw, str):
                result[key] = raw[:128]
        return result or None

    @staticmethod
    def _context_capability(capability: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(capability, dict):
            return {"status": "unknown", "supported": [], "needsReview": [], "unsupported": []}
        result: dict[str, Any] = {}
        for key in ("platformId", "platform", "status", "confidence", "knowledgeVersion", "supplierProfileVersion"):
            value = capability.get(key)
            if key == "confidence" and isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = value
            elif isinstance(value, str):
                result[key] = value[:256]
        for key in ("supported", "needsReview", "unsupported"):
            values = capability.get(key)
            if isinstance(values, list):
                entries: list[dict[str, Any]] = []
                for raw in values[:32]:
                    if not isinstance(raw, dict):
                        continue
                    entry = {}
                    for name in ("field", "message", "value"):
                        value = raw.get(name)
                        if isinstance(value, (str, int, float, bool)):
                            entry[name] = deepcopy(value)
                    if entry:
                        entries.append(entry)
                result[key] = entries
        return result

    def context_snapshot(self, item_index: int | None = None) -> dict[str, Any]:
        """Return a bounded, read-only context for an MCP/model continuation.

        Unlike ``snapshot()``, this method never invokes recommendation tools,
        appends chat messages, or persists state.  A caller can therefore use
        it before every model turn without changing the workflow revision.
        """
        items = self.state.get("order", {}).get("items")
        selected_index = self._item_index(item_index) if item_index is not None else self._item_index()
        selected_item = self._item_order(selected_index) if selected_index is not None else None
        scoped_order = selected_item or self.state.get("order", {})
        validation = validate_order(scoped_order)
        workflow_stage = self._workflow_stage(validation)
        selected_option = (selected_item or {}).get("selectedOption") if selected_item is not None \
            else self.state.get("selectedOption")
        options = self._context_options(self.state, selected_item)
        capability = match_supplier_capability(scoped_order, scoped_order.get("platform"))
        handoff = self.state.get("handoff")
        handoff_summary = None
        if isinstance(handoff, dict):
            handoff_summary = {
                key: deepcopy(handoff[key]) for key in ("status", "platformId", "requiresHumanConfirmation", "updatedAt")
                if key in handoff and isinstance(handoff[key], (str, bool, int, float))
            }
        quote = self._quote_request()
        quote_summary = None
        if isinstance(quote, dict):
            quote_summary = {
                key: deepcopy(quote[key]) for key in ("requestId", "status", "platformId", "itemId", "itemIndex", "createdAt", "updatedAt")
                if key in quote and isinstance(quote[key], (str, bool, int, float))
            }
        snapshot: dict[str, Any] = {
            "sessionId": self.id,
            "revision": self.state.get("revision", 0),
            "workflowStage": workflow_stage,
            "activeItemIndex": selected_index,
            "order": self._context_order_projection(scoped_order, include_items=selected_item is None),
            "selectedOption": selected_option if isinstance(selected_option, str) else None,
            "availableOptions": options,
            "validation": self._context_validation_summary(validation),
            "preflight": self._context_preflight(self.state, selected_item),
            "supplierCapability": self._context_capability(capability),
            "handoff": handoff_summary,
            "quote": quote_summary,
            "knowledgeVersion": KNOWLEDGE_VERSION,
        }
        # In a multi-product order with no explicit focus, expose a compact
        # item index list so a model can ask for the right item without seeing
        # every item payload twice.
        if selected_item is None and isinstance(items, list) and len(items) > 1:
            snapshot["items"] = [
                {"itemIndex": index, "itemId": item.get("itemId"),
                 "productType": item.get("productType"),
                 "selectedOption": item.get("selectedOption")}
                for index, item in enumerate(items[:32]) if isinstance(item, dict)
            ]
        return snapshot

    def _call(self, name: str, *args: Any) -> Any:
        self.trace.append(f"调用工具：{name}")
        started = time.monotonic()
        self._event("tool", "started", f"开始调用 {name}", tool=name)
        try:
            result = TOOLS[name](*args)
        except Exception as error:
            self._event("tool", "failed", "工具执行失败", tool=name,
                        error=error.__class__.__name__, durationMs=round((time.monotonic() - started) * 1000))
            raise
        self._event("tool", "ok", "工具执行完成", tool=name,
                    durationMs=round((time.monotonic() - started) * 1000))
        return result

    def _result(self, messages: list[str], quick: list[dict[str, Any]] | None = None,
                options: list[dict[str, str]] | None = None, handoff: dict[str, Any] | None = None,
                tool_result: Any = None, finish_run: bool = True) -> dict[str, Any]:
        validation = validate_order(self.state["order"])
        if finish_run:
            run = self._finish_run()
        elif self.run_id:
            # Expose a live snapshot to the planner without persisting a
            # premature ``lastRun`` record.  The outer chat call owns the
            # finalization and will include these same events.
            run = {"runId": self.run_id, "operation": self.run_operation,
                   "status": "running", "events": deepcopy(self.run_events)}
        else:
            run = self.state.get("lastRun")
        # Some callers save before building the response; saving here as well
        # ensures the completed run and field provenance survive a restart.
        self._save()
        workflow_stage = self._workflow_stage(validation)
        supplier_capability = match_supplier_capability(self.state["order"])
        active_index = self._item_index()
        active_item = self.state["order"].get("items", [])[active_index] if isinstance(active_index, int) else None
        handoff_value = handoff if handoff is not None else self.state.get("handoff")
        quote_request = self._quote_request()
        receipts = self._planner_tool_receipts or self.state.get("toolReceipts") or []
        return {"sessionId": self.id, "messages": messages, "quickReplies": quick or [], "options": options or [],
                "order": deepcopy(self.state["order"]), "stage": self.state["stage"],
                "selectedOption": self.state["selectedOption"], "orderGenerated": self.state["orderGenerated"],
                "uploadedFile": self.state["uploadedFile"], "uploadedFiles": deepcopy(self.state.get("uploadedFiles") or []), "toolTrace": self.trace,
                "availableTools": self.available_tools(), "toolResult": tool_result,
                "toolReceipts": deepcopy(receipts), "toolResults": deepcopy(receipts),
                "processOptions": deepcopy(self.state.get("processOptions") or []),
                "preflightResults": deepcopy(self.state.get("preflightResults") or {}),
                "handoff": deepcopy(handoff_value),
                "confirmation": deepcopy(self.state.get("confirmation") or {"status": "not_ready"}),
                "quoteRequest": deepcopy(quote_request),
                "quoteRequests": deepcopy(self.state.get("quoteRequests") or []),
                "activeQuoteRequestId": self.state.get("activeQuoteRequestId"),
                "history": deepcopy(self.state["messages"]), "validation": validation,
                "readiness": validation["readiness"],
                "missingFields": validation["missing"],
                "llm": self.planner.public_config() if self.planner and hasattr(self.planner, "public_config") else None,
                "productProfile": parameter_state(self.state["order"]), "nextAction": self._next_action(validation),
                "workflowStage": workflow_stage, "workflowLabel": WORKFLOW_LABELS[workflow_stage],
                "runId": run["runId"] if run else None, "runTrace": deepcopy(run["events"] if run else []),
                "lastRun": deepcopy(run),
                "revision": self.state.get("revision", 0),
                "fieldMeta": deepcopy(self.state.get("fieldMeta") or {}),
                "rejectedFields": deepcopy(self.state.get("rejectedFields") or []),
                "planMeta": deepcopy(self.state.get("planMeta") or {}),
                "conflicts": deepcopy(self.state.get("conflicts") or []),
                "activeItemIndex": self.state.get("activeItemIndex"),
                "activeItemSelectedOption": active_item.get("selectedOption") if isinstance(active_item, dict) else None,
                "decision": self._decision_summary(validation, supplier_capability),
                "supplierCapability": supplier_capability,
                "knowledge": deepcopy(KNOWLEDGE_MANIFEST)}

    def _decision_summary(self, validation: dict[str, Any], supplier_capability: dict[str, Any] | None = None) -> dict[str, Any]:
        """Expose the next decision without leaking provider internals."""
        stage = self._workflow_stage(validation)
        if stage == "collect":
            reason = f"基础信息仍缺少：{'、'.join(validation.get('missing', []))}"
        elif stage == "clarify":
            item_validations = validation.get("itemValidations") or []
            pending = [item for item in item_validations if not item.get("ok")]
            if pending:
                names = "、".join(str(item.get("productType") or f"第 {item.get('index', 0) + 1} 项") for item in pending)
                reason = f"需要逐项补齐：{names}"
            else:
                items = self.state["order"].get("items") or []
                unselected = [(index, item.get("productType") or f"第 {index + 1} 项")
                              for index, item in enumerate(items)
                              if isinstance(item, dict) and not item.get("selectedOption")]
                reason = (f"还需为第 {unselected[0][0] + 1} 项（{unselected[0][1]}）选择工艺方案"
                          if unselected else f"需要补齐{self.state['order'].get('productType') or '当前品类'}专属参数")
        elif stage == "recommend":
            active_index = self._item_index()
            if validation.get("multiProduct") and active_index is not None:
                reason = f"第 {active_index + 1} 项基础信息可用，等待比较并选择工艺方案"
            else:
                reason = "基础信息可用，等待比较并选择工艺方案"
        elif stage == "preflight":
            reason = "正在检查文件基础信息，正式印前仍需人工签核"
        elif stage == "quote":
            reason = "已准备费用估算，正式报价仍以供应商回复为准"
        elif stage == "export":
            reason = "订单信息已整理，等待导出或人工交接"
        else:
            reason = "已选择方案，生成前需要人工确认文件、价格和交期"
        capability = supplier_capability or match_supplier_capability(self.state["order"])
        if capability.get("unsupported"):
            reason += "；目标平台存在不支持项"
        elif capability.get("needsReview"):
            reason += "；供应商能力仍需询价确认"
        confirmed = (self.state.get("confirmation") or {}).get("status") == "confirmed"
        return {"stage": stage, "label": WORKFLOW_LABELS[stage], "reason": reason,
                "humanConfirmationRequired": not confirmed and (stage == "confirm" or bool(validation.get("warnings"))
                or capability.get("status") != "ready")}

    def _tool_reply(self, message: str, remember: bool = True, **kwargs: Any) -> dict[str, Any]:
        if remember:
            self._remember("assistant", message)
        self._save()
        return self._result([message], finish_run=not self._nested_tool_execution, **kwargs)

    @staticmethod
    def available_tools() -> list[dict[str, Any]]:
        return [{**deepcopy(meta), **deepcopy(TOOL_SCHEMAS.get(name, {}))}
                for name, meta in TOOL_META.items()]

    @classmethod
    def planner_tools(cls) -> list[dict[str, Any]]:
        """Return the small chat-planner catalog, separate from UI/MCP tools.

        File preflight is driven by the browser upload flow and has no useful
        chat arguments.  Order objects are authoritative session state and are
        therefore removed from planner schemas; this keeps context small and
        prevents the model from attempting a full order replacement.
        """
        result: list[dict[str, Any]] = []
        for item in cls.available_tools():
            if item.get("name") == "preflight_file":
                continue
            item = deepcopy(item)
            schema = item.get("input")
            if isinstance(schema, dict):
                properties = schema.get("properties")
                if isinstance(properties, dict):
                    properties.pop("order", None)
                required = schema.get("required")
                if isinstance(required, list):
                    schema["required"] = [key for key in required if key != "order"]
                item["input"] = schema
            # Output schemas are useful to adapters/MCP but waste chat context.
            item.pop("output", None)
            result.append(item)
        return result

    def _summary(self) -> str:
        order = self.state["order"]
        parts = [f"{LABELS[key]}：{order[key]}" for key in ("productType", "quantity", "size", "pages", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget") if order.get(key)]
        profile = parameter_state(order)
        spec_parts = [f"{item['label']}：{item['value']}" for item in profile["parameters"] if item.get("value")]
        if spec_parts:
            parts.append("关键参数：" + "、".join(spec_parts[:3]))
        return "我理解的是：" + "；".join(parts) + "。" if parts else "我还没有识别到有效订单信息。"

    def _missing_fields(self) -> list[str]:
        return [key for key in required_order_keys(self.state["order"]) if not self.state["order"].get(key)]

    def _next_action(self, validation: dict[str, Any]) -> str:
        if self.state.get("orderGenerated"):
            if (self.state.get("confirmation") or {}).get("status") == "confirmed":
                return "下一步：导出交接包或由受控平台适配器继续处理"
            return "下一步：人工确认文件、价格和交期"
        if validation.get("multiProduct"):
            pending = [item for item in (validation.get("itemValidations") or []) if not item.get("ok")]
            if pending:
                item = pending[0]
                missing = list(item.get("missing") or []) + list(item.get("productMissing") or [])
                return f"下一步：处理第 {item.get('index', 0) + 1} 项并补充{missing[0] if missing else '产品参数'}"
            active_index = self._item_index()
            items = self.state["order"].get("items") or []
            active_item = items[active_index] if isinstance(active_index, int) and active_index < len(items) else None
            if active_item and not active_item.get("selectedOption"):
                return f"下一步：比较并选择第 {active_index + 1} 项的工艺方案"
            unselected = [index for index, item in enumerate(items)
                          if isinstance(item, dict) and not item.get("selectedOption")]
            if unselected:
                return f"下一步：处理第 {unselected[0] + 1} 项并选择工艺方案"
            if items and all(item.get("selectedOption") for item in items if isinstance(item, dict)):
                return "下一步：生成多产品交接单并人工确认"
            return "下一步：逐项确认工艺方案后再询价"
        if validation["missing"]:
            return f"下一步：补充{validation['missing'][0]}"
        if validation.get("productMissing"):
            return f"下一步：确认{validation['productMissing'][0]}（{self.state['order'].get('productType') or '当前品类'}专属参数）"
        if not self.state["selectedOption"]:
            return "下一步：比较并选择工艺方案"
        if not self.state["orderGenerated"]:
            return "下一步：确认方案后生成订单草稿"
        return "下一步：人工确认文件、价格和交期"

    def _apply_patch(self, target: dict[str, Any], previous: dict[str, Any],
                     changes: dict[str, Any], *, source: str, confidence: float,
                     field_confidence: dict[str, float] | None, allowed_keys: set[str],
                     meta_prefix: str = "", list_fields: dict[str, Any] | None = None,
                     settle=None, field_evidence: dict[str, dict[str, str]] | None = None) -> set[str]:
        """Shared constrained-patch engine for the whole order and each item.

        ``previous`` is the untouched pre-patch copy used for conflict and
        provenance records; ``meta_prefix`` scopes provenance fields for items;
        ``list_fields`` carries order-only list patches; ``settle`` runs after
        the values land but before provenance is recorded. Returns the set of
        keys whose value actually changed. Recommendation invalidation stays
        with the callers because order and items differ there.
        """
        valid: dict[str, Any] = {}
        rejected_fields: list[str] = []
        quantity_keys = {"quantity", "quantityValue", "quantityUnit"}
        # ``items``/``productTypes`` are order-only list patches handled by
        # ``_update_order`` before this function, even when their normalized
        # value happens to equal the current state.  Item-scoped patches do
        # not get this exception.
        list_patch_keys = {"items", "productTypes"} if list_fields is not None else set()
        permitted_change_keys = set(allowed_keys) | quantity_keys | set(list_fields or {}) | list_patch_keys
        # ``productSpecs`` is handled below as a nested allowlist.  Other
        # unknown top-level keys are retained only in the audit trail.
        for raw_key in changes:
            # JSON patches have string keys.  Keep the lower-level gateway
            # defensive for direct Python callers and custom mappings too:
            # checking membership on an unhashable key would otherwise raise
            # before the rest of the valid patch can be applied.
            if not isinstance(raw_key, str):
                rejected_fields.append(f"{meta_prefix}<non-string-patch-key>")
                continue
            key = raw_key.strip()
            if key and raw_key not in permitted_change_keys:
                rejected_fields.append(f"{meta_prefix}{key}")
        if any(key in changes for key in quantity_keys):
            invalid_quantity = [
                key for key in quantity_keys
                if key in changes and changes.get(key) is not None
                and (not self._is_patch_scalar(changes.get(key))
                     or (isinstance(changes.get(key), str)
                         and len(changes.get(key).strip()) > MAX_PATCH_VALUE))
            ]
            if invalid_quantity:
                rejected_fields.extend(f"{meta_prefix}{key}" for key in invalid_quantity)
            else:
                product = str(changes.get("productType") or target.get("productType") or "")
                raw_quantity = changes.get("quantity")
                if raw_quantity in (None, ""):
                    raw_quantity = changes.get("quantityValue", target.get("quantity"))
                unit_hint = changes.get("quantityUnit") or ""
                parsed_quantity = parse_quantity(raw_quantity, product, str(unit_hint))
                if parsed_quantity:
                    display, numeric, unit = parsed_quantity
                    valid.update({"quantity": display, "quantityValue": numeric, "quantityUnit": unit})
                    if all(target.get(key) == value for key, value in
                           (("quantity", display), ("quantityValue", numeric), ("quantityUnit", unit))):
                        # Re-stating an identical quantity is still a confirmation:
                        # it must be able to clear a low confidence grade.
                        for key, value in (("quantity", display), ("quantityValue", numeric), ("quantityUnit", unit)):
                            self._set_field_meta(f"{meta_prefix}{key}", value, source, confidence,
                                                 field_confidence, field_evidence)
                else:
                    # Keep malformed scalar quantities out of the order and
                    # expose the rejection to the bridge/audit caller.
                    rejected_fields.extend(
                        f"{meta_prefix}{key}" for key in quantity_keys
                        if key in changes and changes.get(key) is not None
                    )
        for key, value in changes.items():
            if not isinstance(key, str):
                # Already recorded above; do not perform set membership on a
                # possibly unhashable custom mapping key.
                continue
            if key not in allowed_keys or value is None or key in quantity_keys:
                continue
            if key == "dimensions":
                if not isinstance(value, dict):
                    rejected_fields.append(f"{meta_prefix}dimensions")
                    continue
                current_dimensions = dict(target.get("dimensions") or {})
                next_dimensions = dict(DIMENSION_DEFAULTS)
                next_dimensions.update({name: str(current_dimensions.get(name) or "").strip()
                                        for name in DIMENSION_DEFAULTS})
                for raw_name, dimension_value in value.items():
                    if not isinstance(raw_name, str):
                        rejected_fields.append(f"{meta_prefix}dimensions.<non-string-key>")
                        continue
                    name = raw_name.strip()
                    if name not in DIMENSION_DEFAULTS:
                        rejected_fields.append(f"{meta_prefix}dimensions.{name}")
                        continue
                    if (dimension_value is not None and (
                            not self._is_patch_scalar(dimension_value)
                            or (isinstance(dimension_value, float) and not math.isfinite(dimension_value)))):
                        rejected_fields.append(f"{meta_prefix}dimensions.{name}")
                        continue
                    next_dimensions[name] = str(dimension_value).strip() if dimension_value is not None else ""
                if next_dimensions != current_dimensions:
                    valid[key] = next_dimensions
            elif key == "productSpecs":
                if not isinstance(value, dict):
                    rejected_fields.append(f"{meta_prefix}productSpecs")
                    continue
                current_specs = dict(target.get("productSpecs") or {})
                next_specs = dict(current_specs)
                dimension_patch: dict[str, Any] = {}
                product_name = str(changes.get("productType") or target.get("productType") or "").strip()
                allowed_specs = known_product_spec_keys(product_name or None)
                for raw_name, spec_value in value.items():
                    if not isinstance(raw_name, str):
                        rejected_fields.append(f"{meta_prefix}productSpecs")
                        continue
                    name = raw_name.strip()
                    name = str(name).strip()
                    if not name:
                        continue
                    # Dimension aliases are normalized into the canonical
                    # ``dimensions`` object; all other spec names must belong
                    # to the selected product profile (or the union when the
                    # product has not been identified yet).
                    if name not in DIMENSION_DEFAULTS and name not in allowed_specs:
                        rejected_fields.append(f"{meta_prefix}productSpecs.{name}")
                        continue
                    if (not self._is_patch_scalar(spec_value)
                            or (isinstance(spec_value, float) and not math.isfinite(spec_value))):
                        rejected_fields.append(f"{meta_prefix}productSpecs.{name}")
                        continue
                    normalized = str(spec_value).strip() if spec_value is not None else ""
                    if name in DIMENSION_DEFAULTS:
                        dimension_patch[name] = normalized
                        continue
                    if normalized:
                        if self._equivalent_value(next_specs.get(name), normalized):
                            if source == "user" or self._has_field_signal(
                                    f"{meta_prefix}productSpecs.{name}", field_confidence, field_evidence):
                                self._set_field_meta(f"{meta_prefix}productSpecs.{name}",
                                                     next_specs.get(name), source, confidence,
                                                     field_confidence, field_evidence)
                            continue
                        next_specs[name] = normalized
                    else:
                        next_specs.pop(name, None)
                if next_specs != current_specs:
                    valid[key] = next_specs
                if dimension_patch:
                    valid["dimensions"] = merge_dimension_patch(target.get("dimensions"), dimension_patch)
            elif not self._is_patch_scalar(value):
                rejected_fields.append(f"{meta_prefix}{key}")
                continue
            else:
                try:
                    normalized = str(value).strip()
                except (OverflowError, ValueError):
                    rejected_fields.append(f"{meta_prefix}{key}")
                    continue
                normalized = normalized[:MAX_PATCH_VALUE]
                if not normalized:
                    continue
                if self._equivalent_value(target.get(key), normalized):
                    if source == "user" or self._has_field_signal(
                            f"{meta_prefix}{key}", field_confidence, field_evidence):
                        self._set_field_meta(f"{meta_prefix}{key}", target.get(key), source, confidence,
                                             field_confidence, field_evidence)
                    continue
                valid[key] = normalized
        if list_fields:
            valid.update(list_fields)
        changed = {key for key, value in valid.items() if target.get(key) != value}
        target.update(valid)
        previous_dimensions = previous.get("dimensions") or {}
        normalize_order_dimensions(target)
        if previous_dimensions != target.get("dimensions"):
            changed.add("dimensions")
        if "productType" in changed and previous.get("productType") != target.get("productType"):
            # Product-specific fields belong to the old item and must not leak into a new draft.
            # Preserve explicitly supplied fields for the *new* product when
            # the caller changes productType and specs in one atomic patch.
            # ``valid.productSpecs`` is normally a merge with the old map, so
            # capture only keys explicitly present in this patch.  Otherwise
            # changing a product while adding one new spec would resurrect
            # specs belonging to the previous product.
            incoming_specs: dict[str, Any] = {}
            requested_specs = changes.get("productSpecs") if isinstance(changes.get("productSpecs"), dict) else {}
            new_product_specs = known_product_spec_keys(str(target.get("productType") or "") or None)
            for raw_name, value in requested_specs.items():
                # ``requested_specs`` normally came from JSON, but direct
                # planner adapters can provide arbitrary Python mappings.
                # Keep restoration as strict as the main patch validator:
                # names must be strings and booleans are never production text.
                if (not isinstance(raw_name, str)):
                    continue
                name = raw_name.strip()
                if (name and name not in DIMENSION_DEFAULTS and name in new_product_specs
                        and value not in (None, "") and self._is_patch_scalar(value)):
                    normalized = str(value).strip()
                    if normalized:
                        incoming_specs[name] = normalized
            incoming_dimensions: dict[str, Any] | None = None
            requested_dimensions = changes.get("dimensions") if isinstance(changes.get("dimensions"), dict) else None
            if requested_dimensions is not None:
                incoming_dimensions = {
                    name: value for name, value in requested_dimensions.items()
                    if isinstance(name, str) and name in DIMENSION_DEFAULTS
                    and value not in (None, "") and self._is_patch_scalar(value)
                }
            for raw_name, value in requested_specs.items():
                if isinstance(raw_name, str) and raw_name in DIMENSION_DEFAULTS \
                        and value not in (None, "") and self._is_patch_scalar(value):
                    if incoming_dimensions is None:
                        incoming_dimensions = {}
                    incoming_dimensions[raw_name] = value
            target["productSpecs"] = {}
            target["dimensions"] = deepcopy(DIMENSION_DEFAULTS)
            if incoming_specs:
                target["productSpecs"] = incoming_specs
            if incoming_dimensions is not None:
                target["dimensions"] = merge_dimension_patch(target["dimensions"], incoming_dimensions)
            # Re-run migration after restoring the new product's specs so
            # package/expanded/die-cut aliases populate canonical dimensions.
            normalize_order_dimensions(target)
            changed.add("productSpecs")
            changed.add("dimensions")
            spec_prefix = f"{meta_prefix}productSpecs."
            for field in list(self.state.get("fieldMeta") or {}):
                if field.startswith(spec_prefix):
                    self.state["fieldMeta"].pop(field, None)
        if settle is not None:
            settle()
        for key in changed:
            if key == "productSpecs":
                before = previous.get("productSpecs") or {}
                after = target.get("productSpecs") or {}
                for name in set(before) | set(after):
                    field = f"{meta_prefix}productSpecs.{name}"
                    old_value, new_value = before.get(name, ""), after.get(name, "")
                    if old_value and new_value and old_value != new_value:
                        self._record_conflict(field, old_value, new_value, source)
                    self._set_field_meta(field, new_value, source, confidence, field_confidence, field_evidence)
            elif key == "dimensions":
                before = previous.get("dimensions") or {}
                after = target.get("dimensions") or {}
                for name in DIMENSION_DEFAULTS:
                    field = f"{meta_prefix}dimensions.{name}"
                    old_value, new_value = before.get(name, ""), after.get(name, "")
                    if old_value and new_value and old_value != new_value:
                        self._record_conflict(field, old_value, new_value, source)
                    self._set_field_meta(field, new_value, source, confidence, field_confidence, field_evidence)
            else:
                field = f"{meta_prefix}{key}"
                old_value, new_value = previous.get(key), target.get(key)
                if old_value and new_value and old_value != new_value:
                    self._record_conflict(field, old_value, new_value, source)
                self._set_field_meta(field, new_value, source, confidence, field_confidence, field_evidence)
        self._record_rejected_fields(rejected_fields)
        return changed

    def _update_item(self, index: int, changes: dict[str, Any], source: str = "rule",
                     confidence: float = 0.84,
                     field_confidence: dict[str, float] | None = None,
                     field_evidence: dict[str, dict[str, str]] | None = None) -> set[str]:
        """Apply a constrained patch to one product item in a multi-product order."""
        # A caller may have loaded or constructed state directly instead of
        # going through Memory.load.  Repair IDs before deriving the provenance
        # prefix so a duplicate item cannot record the patch under the wrong
        # occurrence.
        initial_mapping: list[dict[str, Any]] = []
        normalize_order_items(self.state["order"], id_mapping=initial_mapping)
        migrate_state_item_references(self.state, initial_mapping)
        items = self.state["order"].get("items")
        if not isinstance(items, list) or not (0 <= index < len(items)) or not isinstance(changes, dict):
            return set()
        previous = deepcopy(items[index])
        previous_items = deepcopy(items)
        item = deepcopy(previous)
        item_id = item.get("itemId") or f"item-{index + 1}"

        def settle() -> None:
            items[index] = item
            id_mapping: list[dict[str, Any]] = []
            normalize_order_items(self.state["order"], id_mapping=id_mapping)
            migrate_state_item_references(self.state, id_mapping, previous_items=previous_items)

        changed = self._apply_patch(
            item, previous, changes, source=source, confidence=confidence,
            field_confidence=field_confidence, field_evidence=field_evidence,
            allowed_keys=ITEM_PATCH_KEYS,
            meta_prefix=f"items.{item_id}.", settle=settle)
        # normalize_order_items rebuilt the items list with fresh dicts; work
        # on the live item from here on.
        item = self.state["order"]["items"][index]
        if changed & RECOMMENDATION_FIELDS:
            item["selectedOption"] = None
            item["orderGenerated"] = False
            self._clear_process_options(index)
            self._mark_preflight_stale(index, "该产品项订单字段发生变化")
            self._invalidate_delivery_state()
            self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        return changed

    def _update_order(self, changes: dict[str, Any], source: str = "rule", confidence: float = 0.84,
                      field_confidence: dict[str, float] | None = None,
                      field_evidence: dict[str, dict[str, str]] | None = None) -> set[str]:
        order = self.state["order"]
        previous = deepcopy(order)
        previous_items = deepcopy(order.get("items")) if isinstance(order.get("items"), list) else None
        # Order-only list patches: items must be normalized as whole items, and
        # a new items list derives the productTypes summary.
        list_fields: dict[str, Any] = {}
        for key in ("productTypes", "items"):
            if key not in changes or not isinstance(changes.get(key), list):
                continue
            candidate = deepcopy(order)
            candidate[key] = deepcopy(changes[key])
            if key == "items":
                normalize_order_items(candidate)
                normalized_items = candidate["items"]
            else:
                normalized_items = deepcopy(changes[key])
            if normalized_items != order.get(key):
                list_fields[key] = normalized_items
                if key == "items":
                    next_types = candidate.get("productTypes", [])
                    if next_types != order.get("productTypes", []):
                        list_fields["productTypes"] = next_types
        def settle() -> None:
            id_mapping: list[dict[str, Any]] = []
            normalize_order_items(order, id_mapping=id_mapping)
            migrate_state_item_references(self.state, id_mapping, previous_items=previous_items)

        changed = self._apply_patch(
            order, previous, changes, source=source, confidence=confidence,
            field_confidence=field_confidence, field_evidence=field_evidence,
            allowed_keys=ORDER_PATCH_KEYS,
            list_fields=list_fields, settle=settle)
        # Replacing the item list can be a pure identity repair/migration. In
        # that case ``migrate_state_item_references`` has already moved cached
        # options to the repaired IDs; preserve those buckets for unchanged
        # items instead of discarding the whole multi-item cache.
        order_core_changes = changed & (RECOMMENDATION_FIELDS - {"items", "productTypes"})
        if order_core_changes:
            self.state["selectedOption"] = None
            self._clear_process_options()
            self._mark_preflight_stale(reason="订单关键字段发生变化")
            self._invalidate_delivery_state()
            if self.state["stage"] == "confirm": self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        elif changed & {"items", "productTypes"}:
            self._mark_preflight_stale(reason="产品项列表发生变化")
            self._invalidate_delivery_state()
            if self.state["stage"] == "confirm": self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        elif "platform" in changed:
            # A platform switch can invalidate a previously mapped handoff even
            # when the production recommendation itself remains unchanged.
            self._invalidate_delivery_state()
            if self.state["stage"] == "confirm": self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        return changed

    @staticmethod
    def _is_explanation_request(text: str) -> bool:
        return bool(re.search(r"怎么选|如何选|什么区别|有什么区别|是什么|解释|为什么", text)) and bool(
            re.search(r"纸|出血|安全边|裁切|覆膜|哑膜|亮膜|专色|四色|黑白|装订|骑马钉|胶装|工艺", text, re.I)
        )

    @staticmethod
    def _validation_message(result: dict[str, Any]) -> str:
        if result["ok"] and not result["warnings"]:
            suffix = "；".join(result.get("suggestions", []))
            return f"订单字段目前完整（信息度 {result.get('readiness', 100)}%），暂未发现规则警告。" + (f"\n建议：{suffix}" if suffix else " 可以调用工艺推荐工具。")
        parts = []
        if result["missing"]: parts.append("还缺少：" + "、".join(result["missing"]))
        if result["warnings"]: parts.append("需要确认：" + "；".join(result["warnings"]))
        if result.get("suggestions"): parts.append("建议：" + "；".join(result["suggestions"]))
        return "。".join(parts) + "。"

    def _apply_plan(self, plan: dict[str, Any]) -> set[str]:
        self._record_plan_meta(plan)
        patch = plan.get("patch") if isinstance(plan, dict) else None
        if not isinstance(patch, dict):
            if isinstance(plan, dict):
                self._record_rejected_fields(plan.get("rejectedFields"))
            return set()
        field_confidence = plan.get("confidence") if isinstance(plan.get("confidence"), dict) else None
        field_evidence = self._normalize_field_evidence(plan.get("evidence"))
        self._record_rejected_fields(plan.get("rejectedFields"))
        items = self.state.get("order", {}).get("items")
        active_index = self._item_index() if isinstance(items, list) and len(items) > 1 else None
        if active_index is not None:
            changed = self._update_item(
                active_index, patch, source="model", confidence=0.68,
                field_confidence=field_confidence, field_evidence=field_evidence)
        else:
            changed = self._update_order(
                patch, source="model", confidence=0.68,
                field_confidence=field_confidence, field_evidence=field_evidence)
        if changed:
            event_data: dict[str, Any] = {"changedFields": sorted(changed)}
            rejected = self.state.get("rejectedFields") or []
            if rejected:
                event_data["rejectedFields"] = deepcopy(rejected)
            self._event("plan", "ok", "模型提出了受限字段更新", **event_data)
        return changed

    @staticmethod
    def _bridge_patch_paths(patch: dict[str, Any]) -> list[str]:
        """Return deterministic accepted field paths for bridge receipts."""
        paths: list[str] = []
        if not isinstance(patch, dict):
            return paths
        for key, value in patch.items():
            if not isinstance(key, str):
                continue
            if key in {"productSpecs", "dimensions"} and isinstance(value, dict):
                for name in value:
                    if isinstance(name, str) and name.strip():
                        paths.append(f"{key}.{name.strip()}"[:256])
                continue
            paths.append(key.strip()[:256])
        return list(dict.fromkeys(path for path in paths if path))

    def apply_order_patch(self, plan: dict[str, Any],
                          item_index: int | None = None,
                          expected_revision: int | None = None,
                          patch_id: str | None = None,
                          patch_digest: str | None = None,
                          source: str = "model") -> dict[str, Any]:
        """Apply one normalized dsh plan through the shared patch kernel.

        The MCP adapter performs transport/schema validation and calls this
        method while holding the session lock. This method deliberately does
        not accept a complete order replacement: callers can only provide the
        normalized patch returned by the planner validator.
        """
        if not isinstance(plan, dict) or not isinstance(plan.get("patch"), dict):
            raise ValueError("patch plan must be a normalized object")
        if source not in {"model", "user", "rule", "recommendation"}:
            raise ValueError("unsupported patch source")
        before_state = deepcopy(self.state)
        previous_run = (self.run_id, self.run_operation, deepcopy(self.run_events),
                        list(self.trace))
        previous_revision = self.state.get("revision", 0)
        self._begin_run("apply_order_patch")
        self.trace = []
        try:
            patch = plan.get("patch") or {}
            self._record_plan_meta(plan)
            self._record_rejected_fields(plan.get("rejectedFields"))
            rejected_before = set(self.state.get("rejectedFields") or [])
            field_confidence = plan.get("confidence") if isinstance(plan.get("confidence"), dict) else None
            field_evidence = self._normalize_field_evidence(plan.get("evidence"))
            accepted = self._bridge_patch_paths(patch)
            changed: set[str]
            if item_index is not None:
                items = self.state.get("order", {}).get("items")
                if not isinstance(items, list) or not (0 <= item_index < len(items)):
                    raise ValueError("itemIndex out of range")
                item_id = (items[item_index].get("itemId")
                           if isinstance(items[item_index], dict) else None) or f"item-{item_index + 1}"
                changed = self._update_item(
                    item_index, patch, source=source, confidence=0.68,
                    field_confidence=field_confidence, field_evidence=field_evidence)
                accepted = [f"items.{item_id}.{path}"[:256] for path in accepted]
                changed_fields = [
                    f"items.{item_id}.{key}"[:256] for key in sorted(changed)
                ]
            else:
                changed = self._update_order(
                    patch, source=source, confidence=0.68,
                    field_confidence=field_confidence, field_evidence=field_evidence)
                changed_fields = sorted(str(key)[:256] for key in changed)
            rejected = [str(value)[:256] for value in (plan.get("rejectedFields") or [])
                        if isinstance(value, str) and value.strip()]
            rejected = list(dict.fromkeys(rejected))
            # The planner validator may accept a product-spec key using the
            # union profile while the item-scoped kernel rejects it for the
            # selected product. Reflect the kernel's actual rejection in the
            # receipt instead of claiming that field was accepted.
            newly_rejected = [
                value for value in (self.state.get("rejectedFields") or [])
                if value not in rejected_before and isinstance(value, str)
            ]
            for value in newly_rejected:
                if value not in rejected:
                    rejected.append(value[:256])
            if item_index is not None:
                accepted = [
                    path for path in accepted
                    if path not in set(rejected)
                    and not any(path.endswith("." + value) for value in rejected)
                ]
            else:
                accepted = [path for path in accepted if path not in set(rejected)]
            predicted_revision = (
                min(int(previous_revision) + 1, MAX_STATE_REVISION)
                if isinstance(previous_revision, int) and not isinstance(previous_revision, bool)
                else None
            )
            self._event(
                "patch", "ok" if accepted else "rejected",
                "已通过受控 bridge 应用字段 patch" if accepted else "patch 没有可应用字段",
                patchId=patch_id, expectedRevision=expected_revision,
                patchDigest=patch_digest,
                previousRevision=previous_revision, revision=predicted_revision,
                acceptedFields=accepted, changedFields=changed_fields,
                rejectedFields=rejected,
                knowledgeVersion=plan.get("knowledgeVersion") or None,
            )
            applied = bool(accepted)
            tool_result = {
                "status": "applied" if applied else "rejected",
                **({} if applied else {"reason": "no_valid_fields"}),
                "changedFields": changed_fields,
                "acceptedFields": accepted,
                "rejectedFields": rejected,
                "knowledgeVersion": KNOWLEDGE_VERSION,
                "reportedKnowledgeVersion": plan.get("knowledgeVersion", ""),
            }
            message = ("已通过受控 bridge 应用订单字段 patch。"
                       if applied else "patch 中没有可应用的字段，未写入订单。")
            response = self._result([message],
                                    tool_result=tool_result)
            revision = self.state.get("revision", previous_revision)
            response["revision"] = revision
            response["acceptedFields"] = accepted
            response["rejectedFields"] = rejected
            if isinstance(response.get("toolResult"), dict):
                response["toolResult"]["revision"] = revision
            return response
        except Exception:
            self.state = before_state
            self.run_id, self.run_operation, self.run_events, self.trace = previous_run
            self._cas_expected_revision = None
            raise

    def _planner_order_digest(self) -> dict[str, Any]:
        """Compact order view for the planner: present fields only.

        The full order (empty defaults, whole items lists) wastes tokens on
        every call; the digest keeps the same field names so a proposed patch
        still lands through the normal whitelist.
        """
        order = self.state["order"]
        validation = validate_order(order)
        digest: dict[str, Any] = {key: order[key] for key in LABELS if order.get(key)}
        if order.get("quantityValue") is not None:
            digest["quantityValue"] = order["quantityValue"]
        digest["platform"] = order.get("platform") or "generic"
        dimensions = {key: value for key, value in (order.get("dimensions") or {}).items() if value}
        if dimensions:
            digest["dimensions"] = dimensions
        if order.get("productSpecs"):
            digest["productSpecs"] = order["productSpecs"]
        items = order.get("items") if isinstance(order.get("items"), list) else []
        if len(items) > 1:
            trimmed = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                entry = {key: item[key] for key in ("itemId", "productType", "quantity", "size", "pages",
                                                    "selectedOption") if item.get(key)}
                item_dimensions = {key: value for key, value in (item.get("dimensions") or {}).items() if value}
                if item_dimensions:
                    entry["dimensions"] = item_dimensions
                if item.get("productSpecs"):
                    entry["productSpecs"] = item["productSpecs"]
                trimmed.append(entry)
            digest["items"] = trimmed
        digest["missingFields"] = list(validation.get("missing") or []) + list(validation.get("productMissing") or [])
        digest["workflowStage"] = self._workflow_stage(validation)
        # Carry the small, persisted observations that matter on a later chat
        # turn.  This avoids re-sending history or recomputing local tools just
        # to remind the provider which options/file checks already exist.
        active_index = self._item_index()
        if active_index is not None:
            digest["activeItemIndex"] = active_index
        selected = (items[active_index].get("selectedOption")
                    if isinstance(active_index, int) and isinstance(items, list)
                    and 0 <= active_index < len(items) and isinstance(items[active_index], dict)
                    else self.state.get("selectedOption"))
        if isinstance(selected, str) and selected:
            digest["selectedOption"] = selected[:128]
        cached_options = self._context_options(self.state,
                                                self._item_order(active_index) if active_index is not None else None)
        if cached_options:
            digest["availableOptions"] = self._planner_compact_value(cached_options)
        cached_preflight = self._context_preflight(
            self.state, self._item_order(active_index) if active_index is not None else None)
        if cached_preflight:
            digest["preflight"] = self._planner_compact_value(cached_preflight)
        capability = self._context_capability(
            match_supplier_capability(self._item_order(active_index) if active_index is not None else order,
                                      order.get("platform")))
        digest["supplierCapability"] = self._planner_compact_value(capability)
        quote = self._quote_request()
        if isinstance(quote, dict):
            digest["quoteStatus"] = {
                key: quote[key] for key in ("requestId", "status", "platformId", "itemId", "itemIndex")
                if key in quote and isinstance(quote[key], (str, int, float, bool))
            }
        return digest

    def _ask_planner(self, text: str, tool_result: dict[str, Any] | None = None,
                     tool_call: dict[str, Any] | None = None,
                     tool_calls: list[dict[str, Any]] | None = None,
                     tool_ledger: list[dict[str, Any]] | None = None,
                     allow_tools: bool = True, synthesis: bool = False) -> dict[str, Any] | None:
        """Call a provider with bounded context; provider failures stay inside the Agent.

        The progressively reduced keyword calls keep compatibility with small
        local planners written before the ledger/native-batch protocol existed.
        """
        history = self.state["messages"][:-1]
        digest = self._planner_order_digest()
        kwargs: dict[str, Any] = {}
        if tool_result is not None:
            kwargs["tool_result"] = tool_result
        if tool_call is not None:
            kwargs["tool_call"] = tool_call
        if tool_calls is not None:
            kwargs["tool_calls"] = deepcopy(tool_calls)
        if tool_ledger is not None:
            kwargs["tool_ledger"] = deepcopy(self._planner_fit_receipts(tool_ledger))
        if not allow_tools:
            kwargs["allow_tools"] = False
        if synthesis:
            kwargs["synthesis"] = True
        try:
            return self.planner.plan(text, digest, self.planner_tools(), history, **kwargs)
        except TypeError:
            # Retry with the newest compatibility subset first, then fall back
            # to the original four positional arguments.
            for keys in (
                ("tool_result", "tool_call", "tool_calls", "tool_ledger"),
                ("tool_result", "tool_call"),
                ("tool_result",),
                (),
            ):
                reduced = {key: kwargs[key] for key in keys if key in kwargs}
                try:
                    return self.planner.plan(text, digest, self.planner_tools(), history, **reduced)
                except TypeError:
                    continue
                except Exception:
                    if hasattr(self.planner, "last_error"):
                        self.planner.last_error = "模型调用异常"
                    return None
            if hasattr(self.planner, "last_error"):
                self.planner.last_error = "模型调用参数不兼容"
            return None
        except Exception:
            if hasattr(self.planner, "last_error"):
                self.planner.last_error = "模型调用异常"
            return None

    def _planner_tool_fallback(self, name: str, result: Any) -> str:
        """Give the user a useful answer if the second synthesis call fails."""
        if isinstance(result, dict) and result.get("status") in {
                "blocked", "error", "invalid_arguments", "duplicate_blocked"}:
            if result.get("message"):
                return str(result["message"])
            if result.get("text"):
                return str(result["text"])
            if result.get("assumptions"):
                return str(result["assumptions"])
            if result.get("missing"):
                return f"当前步骤暂不能完成，请先补充：{'、'.join(str(item) for item in result['missing'])}。"
            if result.get("reason") == "selection_required":
                return "请先选择工艺方案，再继续当前步骤。"
            return "当前步骤未完成，系统没有把它当作成功结果。"
        if name == "recommend_processes":
            if isinstance(result, list) and result:
                return "已生成工艺方案，请在右侧比较效果、成本、交期和注意事项。"
            if isinstance(result, dict) and result.get("options"):
                return "已生成工艺方案，请在右侧比较效果、成本、交期和注意事项。"
            return "当前还没有生成可用工艺方案，请先补齐订单信息。"
        if name == "validate_order" and isinstance(result, dict):
            return self._validation_message(result)
        if name == "explain_print_term" and isinstance(result, dict):
            return f"{result.get('topic', '印刷基础')}：{result.get('answer', '')}\n\n{result.get('next', '')}".strip()
        if name == "estimate_price" and isinstance(result, dict):
            if result.get("status") == "blocked":
                return str(result.get("assumptions") or "当前订单暂不能合并估算。")
            return (f"按当前信息，费用区间为：{result['range']}。\n{result['assumptions']}"
                    if result.get("range") else f"费用估算还需要：{'、'.join(result.get('missing', []))}。")
        if name == "prepare_handoff":
            return ("订单交接信息已准备好，正式提交前请人工确认价格、文件和交期。"
                    if isinstance(result, dict) and result.get("status") == "ready"
                    else "当前订单暂不能生成交接单。")
        if name == "request_supplier_quote":
            return ("已准备询价请求，正式发送前请人工确认平台、订单字段和交期。"
                    if isinstance(result, dict)
                    and result.get("status") == "awaiting_human_confirmation"
                    else "当前订单暂不能生成询价请求。")
        if name == "match_supplier_capability":
            return "已完成供应商能力匹配，请查看支持项与待确认项。"
        if isinstance(result, dict) and result.get("message"):
            return str(result["message"])
        return "工具已完成处理，请查看订单面板中的结果。"

    def _planner_fallback_tool(self, text: str) -> tuple[str, dict[str, Any]] | None:
        """Choose a bounded local tool when a model reply skipped a necessary call.

        This is intentionally deterministic.  A provider may explain a result
        without emitting a function call, but it must not be able to suppress
        validation or an explicit price/term/quote request.  The normal
        ``call_tool`` gateway still enforces readiness and confirmation.
        """
        raw = (text or "").strip()
        if self._is_explanation_request(raw):
            return "explain_print_term", {"question": raw}
        if self._planner_intent(raw, "quote"):
            platform = self.state["order"].get("platform")
            return "request_supplier_quote", ({"platformId": platform} if platform else {})
        if self._planner_intent(raw, "price"):
            active_index = self._item_index()
            return "estimate_price", ({"itemIndex": active_index} if active_index is not None else {})
        if self._planner_intent(raw, "capability"):
            platform = self.state["order"].get("platform")
            return "match_supplier_capability", ({"platformId": platform} if platform else {})
        if self._planner_intent(raw, "validation"):
            return "validate_order", {}
        if self._missing_fields():
            return "validate_order", {}
        if any(term in raw for term in ("方案", "推荐", "工艺", "怎么印", "做", "印", "制作", "改成", "补充")):
            active_index = self._item_index()
            return "recommend_processes", ({"itemIndex": active_index} if active_index is not None else {})
        return None

    def _remember(self, role: str, text: str) -> None:
        self.state["messages"] = (self.state["messages"] + [{"role": role, "text": text}])[-HISTORY_LIMIT:]

    def _save(self) -> None:
        current_revision = self.state.get("revision")
        if (not isinstance(current_revision, int) or isinstance(current_revision, bool)
                or current_revision < 0 or current_revision > MAX_STATE_REVISION):
            current_revision = 0
            self.state["revision"] = current_revision
        digest = self._state_digest(self.state)
        if digest != self._saved_state_digest:
            self.state["revision"] = min(current_revision + 1, MAX_STATE_REVISION)
        expected_revision = self._cas_expected_revision
        if expected_revision is not None:
            ok, stored_revision = self.memory.save_if_revision(
                self.id, self.state, expected_revision)
            self._cas_expected_revision = None
            if not ok:
                raise RevisionConflictError(expected_revision, stored_revision)
        else:
            self.memory.save(self.id, self.state)
        self._saved_state_digest = self._state_digest(self.state)

    @staticmethod
    def _perceive(text: str, allow_multi: bool = True) -> dict[str, Any]:
        """Delegate to :mod:`nlu`; fields only, for legacy call sites/tests."""
        return perceive(text, allow_multi=allow_multi)[0]

    @staticmethod
    def _perceive_full(text: str, product_hint: str = "") -> tuple[dict[str, Any], dict[str, float]]:
        """Return fields plus the per-field evidence confidence grade."""
        return perceive(text, product_hint=product_hint)

    @staticmethod
    def _question(key: str) -> str:
        return {"productType": "你想做哪一种印刷品？", "quantity": "大约需要多少份？", "size": "成品尺寸是多少？",
                "paper": "对纸张有偏好吗？不确定可以选按效果推荐。", "printing": "需要单面、双面还是黑白印刷？",
                "deadline": "什么时候需要拿到成品？"}[key]

    @staticmethod
    def _quick_replies(key: str, product: str | None = None) -> list[dict[str, Any]]:
        choices = {
            "productType": [("宣传册", "宣传册"), ("折页", "折页"), ("名片", "名片"), ("包装盒", "包装盒")],
            "quantity": [(f"100 {default_quantity_unit(product)}", f"100 {default_quantity_unit(product)}"),
                         (f"500 {default_quantity_unit(product)}", f"500 {default_quantity_unit(product)}"),
                         (f"1,000 {default_quantity_unit(product)}", f"1000 {default_quantity_unit(product)}")],
            "size": [("A4", "A4"), ("A5", "A5"), ("210 × 285 mm", "210×285MM")],
            "paper": [("按效果推荐", "待推荐"), ("157g 哑粉纸", "157g 哑粉纸"), ("250g 铜版纸", "250g 铜版纸")],
            "printing": [("双面四色", "双面四色"), ("单面四色", "单面四色"), ("黑白/单色", "单色印刷")],
            "deadline": [("一周内", "一周内"), ("两周内", "两周内"), ("时间不紧", "时间不紧")],
        }
        return [{"label": label, "data": {key: value}} for label, value in choices.get(key, [])]

    @staticmethod
    def _product_quick_replies(key: str) -> list[dict[str, Any]]:
        choices = {
            "folding": [("二折", "二折"), ("三折", "三折"), ("风琴折", "风琴折")],
            "paperParts": [("二联", "二联"), ("三联", "三联"), ("四联", "四联")],
            "boxStructure": [("天地盖", "天地盖"), ("抽屉盒", "抽屉盒"), ("折叠盒", "折叠盒")],
            "labelMaterial": [("铜版不干胶", "铜版不干胶"), ("透明不干胶", "透明不干胶"), ("PET", "PET")],
            "labelShape": [("方形", "方形"), ("圆形", "圆形"), ("异形", "异形")],
            "cardType": [("智能卡", "智能卡"), ("人像证卡", "人像证卡"), ("滴胶卡", "滴胶卡")],
            "cardThickness": [("0.38mm", "0.38mm"), ("0.76mm", "0.76mm"), ("其他厚度", "需确认卡片厚度")],
            "hangHole": [("圆孔", "圆孔"), ("蝴蝶孔", "蝴蝶孔"), ("打孔", "打孔")],
            "string": [("棉绳", "棉绳"), ("扁绳", "扁绳"), ("不需要配绳", "无需配绳")],
            "bagMaterial": [("白卡纸", "白卡纸"), ("牛皮纸", "牛皮纸"), ("无纺布", "无纺布")],
            "handle": [("棉绳", "棉绳"), ("扁绳", "扁绳"), ("丝带", "丝带")],
            "cupMaterial": [("单 PE", "单 PE"), ("双 PE", "双 PE")],
            "innerCoating": [("需要内淋膜", "需要内淋膜"), ("不需要内淋膜", "不需要内淋膜")],
            "displayMaterial": [("背胶", "背胶"), ("灯片", "灯片"), ("车贴", "车贴")],
            "install": [("墙面张贴", "墙面张贴"), ("裱板", "裱板"), ("打孔包边", "打孔包边")],
            "boardThickness": [("3mm", "3mm"), ("5mm", "5mm"), ("10mm", "10mm")],
        }
        return [{"label": label, "data": {"productSpecs": {key: value}}} for label, value in choices.get(key, [])]
