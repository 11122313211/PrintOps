"""Small, dependency-free print order agent.

Flow: perceive -> remember -> plan -> call tools -> respond.
The contracts are intentionally compatible with a future LangGraph/FastAPI layer.
"""

from contextlib import contextmanager
import json
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
                         STATE_SCHEMA_VERSION, _multi_product_info,
                         default_quantity_unit,
                         merge_dimension_patch, migrate_dimension_field_meta,
                         normalize_order_dimensions, normalize_order_items,
                         normalize_state, parse_quantity,
                         quote_idempotency_key, required_order_keys)
from product_knowledge import KNOWLEDGE_MANIFEST, KNOWLEDGE_VERSION, parameter_state
from supplier_adapters import (ADAPTERS, PLATFORMS, SUPPLIER_PROFILE_VERSION, SupplierAdapter,
                               get_adapter)
from tools import (TOOLS, TOOL_META, TOOL_SCHEMAS, estimate_price, explain_print_term,
                   match_supplier_capability, preflight_file, prepare_handoff,
                   recommend_processes, request_supplier_quote, validate_order)

HISTORY_LIMIT = 80
MAX_PLANNER_TOOL_ROUNDS = 2
MAX_RUN_EVENTS = 64
MAX_RUN_HISTORY = 20
MAX_QUOTE_REQUESTS = 40
QUOTE_ACTIVE_STATUSES = {"awaiting_human_confirmation", "confirmed"}
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
                "workflowStage": "collect", "activeItemIndex": None, "itemOptions": {},
                "quoteRequests": [], "activeQuoteRequestId": None,
                "schemaVersion": STATE_SCHEMA_VERSION}

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


class Agent:
    def __init__(self, memory: Memory, session_id: str | None = None, planner: Any = None) -> None:
        self.memory, self.id, self.planner = memory, session_id or uuid.uuid4().hex[:12], planner
        self.state = memory.load(self.id)
        self.trace: list[str] = []
        self.run_id = ""
        self.run_operation = ""
        self.run_events: list[dict[str, Any]] = []

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _begin_run(self, operation: str) -> None:
        """Start a bounded, inspectable run while keeping the old trace API."""
        self.run_id = uuid.uuid4().hex[:16]
        self.run_operation = operation
        self.run_events = []
        self._event("run", "started", f"开始{operation}")

    def _invalidate_delivery_state(self) -> None:
        """Invalidate generated handoff data after an order-affecting change."""
        self._mark_quote_requests_stale("订单字段、文件或目标平台发生变化")
        self.state["orderGenerated"] = False
        self.state["handoff"] = None
        self.state["confirmation"] = {"status": "not_ready"}

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
        except (TypeError, ValueError):
            return None
        return candidate if isinstance(items, list) and len(items) > 1 and 0 <= candidate < len(items) else None

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
            return {
                "status": "blocked", "reason": "order_not_ready", "missing": missing,
                "requiresHumanConfirmation": True, "message": message,
            }
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

    def _resolve_confidence(self, key: str, base: float, field_confidence: dict[str, float] | None) -> float:
        """Prefer the perception-grade confidence for a field over the scalar."""
        if not field_confidence:
            return base
        if key in field_confidence:
            return float(field_confidence[key])
        if key.startswith("items."):
            remainder = ".".join(key.split(".")[2:])
            if remainder in field_confidence:
                return float(field_confidence[remainder])
        if key.startswith("productSpecs.") and "productSpecs" in field_confidence:
            return float(field_confidence["productSpecs"])
        return base

    def _set_field_meta(self, key: str, value: Any, source: str, confidence: float,
                        field_confidence: dict[str, float] | None = None) -> None:
        if value in (None, ""):
            self.state.setdefault("fieldMeta", {}).pop(key, None)
            return
        graded = self._resolve_confidence(key, confidence, field_confidence)
        self.state.setdefault("fieldMeta", {})[key] = {
            "value": deepcopy(value), "source": source,
            "sourceLabel": FIELD_SOURCE_LABELS.get(source, source),
            "confidence": round(max(0.0, min(1.0, float(graded))), 2),
            "runId": self.run_id or None, "updatedAt": self._timestamp(),
        }

    PRODUCTION_FIELD_KEYS = {"productType", "quantity", "quantityValue", "quantityUnit", "size",
                             "dimensions", "pages", "orientation", "paper", "printing",
                             "finishing", "binding", "deadline"}

    @classmethod
    def _is_production_field(cls, key: str) -> bool:
        """Preference fields (budget/purpose/platform) never block generation."""
        if key.startswith("items."):
            key = ".".join(key.split(".")[2:])
        if key.startswith(("productSpecs.", "dimensions.")):
            return True
        return key in cls.PRODUCTION_FIELD_KEYS

    def _low_confidence_fields(self, production_only: bool = False) -> list[str]:
        fields = [key for key, meta in (self.state.get("fieldMeta") or {}).items()
                  if meta.get("value") not in (None, "", {}) and float(meta.get("confidence", 1)) < 0.75]
        if production_only:
            fields = [key for key in fields if self._is_production_field(key)]
        return fields

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

    def chat(self, text: str, patch: dict[str, str] | None = None, item_index: int | None = None) -> dict[str, Any]:
        self._begin_run("chat")
        self.trace = ["感知需求"]
        self._event("perceive", "ok", "已完成规则感知")
        perceived, perceived_confidence = self._perceive_full(
            text, self.state["order"].get("productType") or "")
        focus_match = re.search(r"第\s*(\d+)\s*项", text or "")
        requested_index = item_index
        if requested_index is None and focus_match and re.search(r"处理|查看|编辑|切换|更新", text or ""):
            try:
                requested_index = int(focus_match.group(1)) - 1
            except ValueError:
                requested_index = -1
        if requested_index is not None:
            items = self.state["order"].get("items")
            if not isinstance(items, list) or not (0 <= requested_index < len(items)):
                requested_index = None
            else:
                self.state["activeItemIndex"] = requested_index
                # A focus-only command should not trigger a second perception
                # pass or overwrite the top-level compatibility fields.
                if not perceived and not patch:
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
        if multi_products:
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

        # An optional LLM planner improves language understanding while field
        # patches and tool names remain constrained by this Agent.
        if self.planner and getattr(self.planner, "enabled", False):
            self.trace.append(f"调用模型：{getattr(self.planner, 'model', '已配置模型')}")
            self._event("plan", "started", "请求模型规划", model=getattr(self.planner, "model", "已配置模型"))
            plan = self._ask_planner(text)
            last_tool_name = ""
            last_tool_response: dict[str, Any] | None = None
            final_reply = ""
            for tool_round in range(MAX_PLANNER_TOOL_ROUNDS):
                if not isinstance(plan, dict):
                    break
                self._apply_plan(plan)
                planned_tool = plan.get("tool")
                if not isinstance(planned_tool, dict) or planned_tool.get("name") not in TOOLS:
                    if plan.get("reply"):
                        final_reply = str(plan["reply"])
                    break
                tool_name = str(planned_tool["name"])
                if tool_name == last_tool_name:
                    self.trace.append(f"阻止重复工具：{tool_name}")
                    break
                last_tool_name = tool_name
                last_tool_response = self.call_tool(
                    tool_name, planned_tool.get("arguments", {}), preserve_trace=True, remember=False,
                )
                if tool_round + 1 >= MAX_PLANNER_TOOL_ROUNDS:
                    break
                self.trace.append(f"调用模型总结工具结果：{getattr(self.planner, 'model', '已配置模型')}")
                plan = self._ask_planner(text, {"name": tool_name, "result": last_tool_response.get("toolResult")})
            if last_tool_response is not None:
                result = last_tool_response.get("toolResult")
                message = final_reply.strip() or self._planner_tool_fallback(last_tool_name, result)
                self._remember("assistant", message)
                self._save()
                return self._result(
                    [message], options=last_tool_response.get("options", []),
                    handoff=last_tool_response.get("handoff"), tool_result=result,
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
        if any(term in text for term in ("多少钱", "价格", "报价", "预算估算")):
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
        if any(term in text for term in ("询价", "问价", "查报价", "查价格")):
            active_index = self._item_index()
            result = self._prepare_quote_request(active_index, self.state["order"].get("platform"))
            message = result.get("message", "已准备询价请求，正式发送前需要人工确认。")
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if any(term in text for term in ("检查订单", "校验订单", "还缺什么", "检查一下")):
            result = self._call("validate_order", self.state["order"])
            message = self._validation_message(result)
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if any(term in text for term in ("生成订单", "生成草稿", "下单")):
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
        if not preserve_trace:
            self._begin_run(f"tool:{name}")
            self.trace = []
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            self.trace.append(f"工具参数无效：{name}")
            self._event("tool", "rejected", f"工具 {name} 参数不是 JSON 对象", tool=name)
            return self._tool_reply("工具参数需要使用 JSON 对象，未执行。", remember=remember)
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
            result = self._call(name, item_order or self.state["order"])
            if item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            message = (result.get("text") if result.get("status") == "blocked"
                       else "已调用订单交接工具，生成平台适配文本。")
            return self._tool_reply(message, tool_result=result, handoff=result, remember=remember)
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
            item_order = self._item_order(index) if index is not None else None
            item_validation = self._item_validation(index) if index is not None else None
            if item_order is None or not item_validation or not item_validation.get("ok"):
                return self._result(["还不能选择方案。请先选择一个产品项，并补齐该项的基础字段和品类参数。"], tool_result=item_validation or validation)
            item_id = item_order.get("itemId") or f"item-{index + 1}"
            options = deepcopy(self.state.get("itemOptions", {}).get(item_id, []))
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
            all_selected = all(item.get("selectedOption") for item in self.state["order"].get("items", []) if isinstance(item, dict))
            self.state["workflowStage"] = "confirm" if all_selected else "clarify"
            message = f"已为第 {index + 1} 项选择{option['title']}，该项参数已更新。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], [], options, tool_result={"status": "selected", "itemId": item_id,
                                                                       "itemIndex": index, "option": option})
        if not validation["ok"]:
            return self._result([f"还不能选择方案。{self._validation_message(validation)}"], tool_result=validation)
        options = self._call("recommend_processes", self.state["order"])
        option = next((item for item in options if item["id"] == option_id), None)
        if not option:
            return self._result(["没有找到这个方案。"], [], options)
        updates = {"finishing": option["finishing"], "binding": option.get("binding", self.state["order"]["binding"])}
        if self.state["order"].get("productType") not in MATERIAL_SPEC_PRODUCTS:
            updates["paper"] = option["paper"]
        self._update_order(updates, source="recommendation", confidence=0.9)
        self.state["selectedOption"] = option_id
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
            options = deepcopy(self.state.get("itemOptions", {}).get(item.get("itemId"), []))
        elif self.state["stage"] != "collect":
            options = self._call("recommend_processes", self.state["order"])
        self.trace = ["恢复会话记忆"] if self.state["messages"] else []
        return self._result([], [], options)

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
                tool_result: Any = None) -> dict[str, Any]:
        validation = validate_order(self.state["order"])
        run = self._finish_run()
        # Some callers save before building the response; saving here as well
        # ensures the completed run and field provenance survive a restart.
        self._save()
        workflow_stage = self._workflow_stage(validation)
        supplier_capability = match_supplier_capability(self.state["order"])
        active_index = self._item_index()
        active_item = self.state["order"].get("items", [])[active_index] if isinstance(active_index, int) else None
        handoff_value = handoff if handoff is not None else self.state.get("handoff")
        quote_request = self._quote_request()
        return {"sessionId": self.id, "messages": messages, "quickReplies": quick or [], "options": options or [],
                "order": deepcopy(self.state["order"]), "stage": self.state["stage"],
                "selectedOption": self.state["selectedOption"], "orderGenerated": self.state["orderGenerated"],
                "uploadedFile": self.state["uploadedFile"], "uploadedFiles": deepcopy(self.state.get("uploadedFiles") or []), "toolTrace": self.trace,
                "availableTools": self.available_tools(), "toolResult": tool_result, "handoff": deepcopy(handoff_value),
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
                "fieldMeta": deepcopy(self.state.get("fieldMeta") or {}),
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
        return self._result([message], **kwargs)

    @staticmethod
    def available_tools() -> list[dict[str, Any]]:
        return [{**deepcopy(meta), **deepcopy(TOOL_SCHEMAS.get(name, {}))}
                for name, meta in TOOL_META.items()]

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
                     settle=None) -> set[str]:
        """Shared constrained-patch engine for the whole order and each item.

        ``previous`` is the untouched pre-patch copy used for conflict and
        provenance records; ``meta_prefix`` scopes provenance fields for items;
        ``list_fields`` carries order-only list patches; ``settle`` runs after
        the values land but before provenance is recorded. Returns the set of
        keys whose value actually changed. Recommendation invalidation stays
        with the callers because order and items differ there.
        """
        valid: dict[str, Any] = {}
        quantity_keys = {"quantity", "quantityValue", "quantityUnit"}
        if any(key in changes for key in quantity_keys):
            product = str(changes.get("productType") or target.get("productType") or "")
            raw_quantity = changes.get("quantity")
            if raw_quantity in (None, ""):
                raw_quantity = changes.get("quantityValue", target.get("quantity"))
            parsed_quantity = parse_quantity(raw_quantity, product, str(changes.get("quantityUnit") or ""))
            if parsed_quantity:
                display, numeric, unit = parsed_quantity
                valid.update({"quantity": display, "quantityValue": numeric, "quantityUnit": unit})
                if all(target.get(key) == value for key, value in
                       (("quantity", display), ("quantityValue", numeric), ("quantityUnit", unit))):
                    # Re-stating an identical quantity is still a confirmation:
                    # it must be able to clear a low confidence grade.
                    for key, value in (("quantity", display), ("quantityValue", numeric), ("quantityUnit", unit)):
                        self._set_field_meta(f"{meta_prefix}{key}", value, source, confidence, field_confidence)
        for key, value in changes.items():
            if key not in allowed_keys or value is None or key in quantity_keys:
                continue
            if key == "dimensions" and isinstance(value, dict):
                current_dimensions = dict(target.get("dimensions") or {})
                next_dimensions = dict(DIMENSION_DEFAULTS)
                next_dimensions.update({name: str(current_dimensions.get(name) or "").strip()
                                        for name in DIMENSION_DEFAULTS})
                for name, dimension_value in value.items():
                    if name not in DIMENSION_DEFAULTS:
                        continue
                    next_dimensions[name] = str(dimension_value).strip() if dimension_value is not None else ""
                if next_dimensions != current_dimensions:
                    valid[key] = next_dimensions
            elif key == "productSpecs" and isinstance(value, dict):
                current_specs = dict(target.get("productSpecs") or {})
                next_specs = dict(current_specs)
                dimension_patch: dict[str, Any] = {}
                for name, spec_value in value.items():
                    name = str(name).strip()
                    if not name:
                        continue
                    normalized = str(spec_value).strip() if spec_value is not None else ""
                    if name in DIMENSION_DEFAULTS:
                        dimension_patch[name] = normalized
                        continue
                    if normalized:
                        if self._equivalent_value(next_specs.get(name), normalized):
                            if source == "user":
                                self._set_field_meta(f"{meta_prefix}productSpecs.{name}",
                                                     next_specs.get(name), source, confidence, field_confidence)
                            continue
                        next_specs[name] = normalized
                    else:
                        next_specs.pop(name, None)
                if next_specs != current_specs:
                    valid[key] = next_specs
                if dimension_patch:
                    valid["dimensions"] = merge_dimension_patch(target.get("dimensions"), dimension_patch)
            elif str(value).strip():
                normalized = str(value).strip()
                if self._equivalent_value(target.get(key), normalized):
                    if source == "user":
                        self._set_field_meta(f"{meta_prefix}{key}", target.get(key), source, confidence, field_confidence)
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
        if "productType" in changed and previous.get("productType") and previous.get("productType") != target.get("productType"):
            # Product-specific fields belong to the old item and must not leak into a new draft.
            target["productSpecs"] = {}
            target["dimensions"] = deepcopy(DIMENSION_DEFAULTS)
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
                    self._set_field_meta(field, new_value, source, confidence, field_confidence)
            elif key == "dimensions":
                before = previous.get("dimensions") or {}
                after = target.get("dimensions") or {}
                for name in DIMENSION_DEFAULTS:
                    field = f"{meta_prefix}dimensions.{name}"
                    old_value, new_value = before.get(name, ""), after.get(name, "")
                    if old_value and new_value and old_value != new_value:
                        self._record_conflict(field, old_value, new_value, source)
                    self._set_field_meta(field, new_value, source, confidence, field_confidence)
            else:
                field = f"{meta_prefix}{key}"
                old_value, new_value = previous.get(key), target.get(key)
                if old_value and new_value and old_value != new_value:
                    self._record_conflict(field, old_value, new_value, source)
                self._set_field_meta(field, new_value, source, confidence, field_confidence)
        return changed

    def _update_item(self, index: int, changes: dict[str, Any], source: str = "rule",
                     confidence: float = 0.84,
                     field_confidence: dict[str, float] | None = None) -> set[str]:
        """Apply a constrained patch to one product item in a multi-product order."""
        items = self.state["order"].get("items")
        if not isinstance(items, list) or not (0 <= index < len(items)) or not isinstance(changes, dict):
            return set()
        previous = deepcopy(items[index])
        item = deepcopy(previous)
        item_id = item.get("itemId") or f"item-{index + 1}"

        def settle() -> None:
            items[index] = item
            normalize_order_items(self.state["order"])

        changed = self._apply_patch(
            item, previous, changes, source=source, confidence=confidence,
            field_confidence=field_confidence, allowed_keys=ITEM_PATCH_KEYS,
            meta_prefix=f"items.{item_id}.", settle=settle)
        # normalize_order_items rebuilt the items list with fresh dicts; work
        # on the live item from here on.
        item = self.state["order"]["items"][index]
        if changed & RECOMMENDATION_FIELDS:
            item["selectedOption"] = None
            item["orderGenerated"] = False
            self.state.setdefault("itemOptions", {}).pop(item_id, None)
            self._invalidate_delivery_state()
            self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        return changed

    def _update_order(self, changes: dict[str, Any], source: str = "rule", confidence: float = 0.84,
                      field_confidence: dict[str, float] | None = None) -> set[str]:
        order = self.state["order"]
        previous = deepcopy(order)
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
        changed = self._apply_patch(
            order, previous, changes, source=source, confidence=confidence,
            field_confidence=field_confidence, allowed_keys=ORDER_PATCH_KEYS,
            list_fields=list_fields, settle=lambda: normalize_order_items(order))
        if changed & RECOMMENDATION_FIELDS:
            self.state["selectedOption"] = None
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

    def _apply_plan(self, plan: dict[str, Any]) -> None:
        patch = plan.get("patch") if isinstance(plan, dict) else None
        if not isinstance(patch, dict):
            return
        changed = self._update_order(patch, source="model", confidence=0.68)
        if changed:
            self._event("plan", "ok", "模型提出了受限字段更新", changedFields=sorted(changed))

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
        return digest

    def _ask_planner(self, text: str, tool_result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Call a provider with bounded context; provider failures stay inside the Agent."""
        history = self.state["messages"][:-1]
        digest = self._planner_order_digest()
        try:
            if tool_result is None:
                return self.planner.plan(text, digest, self.available_tools(), history)
            try:
                return self.planner.plan(text, digest, self.available_tools(), history, tool_result=tool_result)
            except TypeError:
                # Keep compatibility with an older custom planner implementation.
                return self.planner.plan(text, digest, self.available_tools(), history)
        except Exception:
            if hasattr(self.planner, "last_error"):
                self.planner.last_error = "模型调用异常"
            return None

    def _planner_tool_fallback(self, name: str, result: Any) -> str:
        """Give the user a useful answer if the second synthesis call fails."""
        if name == "recommend_processes":
            return "已生成工艺方案，请在右侧比较效果、成本、交期和注意事项。"
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
            return "订单交接信息已准备好，正式提交前请人工确认价格、文件和交期。"
        if name == "request_supplier_quote":
            return "已准备询价请求，正式发送前请人工确认平台、订单字段和交期。"
        if isinstance(result, dict) and result.get("message"):
            return str(result["message"])
        return "工具已完成处理，请查看订单面板中的结果。"

    def _remember(self, role: str, text: str) -> None:
        self.state["messages"] = (self.state["messages"] + [{"role": role, "text": text}])[-HISTORY_LIMIT:]

    def _save(self) -> None:
        self.memory.save(self.id, self.state)

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
