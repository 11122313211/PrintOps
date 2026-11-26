"""Read-only response envelopes, workflow stage derivation, and MCP/UI context projection."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any

from printops.domain.knowledge import KNOWLEDGE_MANIFEST, KNOWLEDGE_VERSION, knowledge_review_status, parameter_state
from printops.domain.order_contract import canonical_order, handoff_document, review_contract
from printops.domain.order_model import (
    DIMENSION_DEFAULTS,
    LABELS,
    RECOMMENDATION_FIELDS,
    STATE_SCHEMA_VERSION,
    _multi_product_info,
    required_order_keys,
)
from printops.supplier.adapters import PLATFORMS, SUPPLIER_PROFILE_VERSION, get_adapter
from printops.tools.registry import match_supplier_capability, validate_order
from printops.agent.memory import FIELD_SOURCE_LABELS, WORKFLOW_LABELS


class ContextProjectorMixin:
    """Response envelope assembly, workflow stage projection, and review summaries."""

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

    @classmethod
    def _context_order_projection(cls, order: dict[str, Any] | None,
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
                and cls._is_patch_scalar(value)
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
                    item = cls._context_order_projection(raw_item, include_items=False)
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

    def _estimate_reference(self, item_order=None, item_index=None):
        uncertain = self._uncertain_fields_for_item(item_order, item_index)
        if uncertain:
            # Preserve the public estimate shape without calculating with an
            # unaccepted candidate. Explicit user confirmation unlocks it.
            result = self._call("estimate_price", {})
            result.update({"status": "blocked", "reason": "low_confidence", "range": None,
                           "displayRange": "请先确认计价参数", "uncertain": uncertain,
                           "missing": [f"确认{LABELS.get(key.split('.')[-1], key)}" for key in uncertain],
                           "requiresHumanConfirmation": True})
            return result
        return self._call("estimate_price", item_order or self.state["order"])

    @staticmethod
    def _estimate_message(result):
        assumptions = result.get("assumptions") or []
        details = "；".join(str(x) for x in assumptions) if isinstance(assumptions, list) else str(assumptions)
        if not result.get("range"):
            return "参考估算尚需补充：" + "、".join(result.get("missing") or []) + "。" + details
        return f"本地参考估算：{result.get('displayRange') or result.get('range')}。\n{details}\n{result.get('disclaimer', '')}"

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

    def _result(self, messages: list[str], quick: list[dict[str, Any]] | None = None,
                options: list[dict[str, str]] | None = None, handoff: dict[str, Any] | None = None,
                tool_result: Any = None, finish_run: bool = True) -> dict[str, Any]:
        validation = validate_order(self.state["order"])
        stored_version = self.state.get("schemaVersion")
        if isinstance(stored_version, int) and not isinstance(stored_version, bool) \
                and stored_version > STATE_SCHEMA_VERSION:
            # The session was written by a newer build.  Loading is deliberate
            # and non-blocking, but every order response keeps the hint visible.
            validation["risks"].append({
                "level": "info", "code": "state_version_newer",
                "message": f"会话状态版本 {stored_version} 高于当前支持的 {STATE_SCHEMA_VERSION}，已按兼容方式加载。",
                "suggestion": "如出现字段或方案异常，请重新核对订单字段，或升级 PrintOps 后再继续。",
            })
        # Advisory knowledge freshness (v1.3.0): a due/overdue review surfaces
        # once per order response as a hint-level risk; ok/unknown stay silent.
        # The lookup is wrapped because a broken manifest or rate card must
        # never change the response, and the hint is deliberately not deduped
        # or persisted.
        try:
            knowledge_review = knowledge_review_status()
            if knowledge_review.get("status") in ("due", "overdue"):
                state_label = "已到复核期" if knowledge_review.get("status") == "due" else "已超过复核期"
                validation["risks"].append({
                    "level": "info", "code": "knowledge_review_due",
                    "message": (f"知识库与费率表已 {knowledge_review.get('daysSinceReview')} 天未复核（{state_label}），"
                                "工艺建议与费用估算可能基于过期参数。"),
                    "suggestion": "请安排运行 python tools/knowledge_review.py，复核 rate card 与产品知识后更新清单。",
                })
        except Exception:
            pass
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
        if finish_run and not self._nested_tool_execution and getattr(self, "_operation_context", None):
            self._operation_complete = True
        self._save()
        workflow_stage = self._workflow_stage(validation)
        supplier_capability = match_supplier_capability(self.state["order"])
        active_index = self._item_index()
        active_item = self.state["order"].get("items", [])[active_index] if isinstance(active_index, int) else None
        handoff_value = handoff if handoff is not None else self.state.get("handoff")
        quote_request = self._quote_request()
        if quote_request is None:
            # The singular response field is a display contract: when no active
            # request exists it still surfaces the newest entry of any status
            # (cancelled/stale included) so the UI can explain what happened.
            requests = self.state.get("quoteRequests")
            if isinstance(requests, list):
                quote_request = next((item for item in reversed(requests)
                                      if isinstance(item, dict)), None)
        receipts = self._planner_tool_receipts or self.state.get("toolReceipts") or []
        review = self.review()
        return {"sessionId": self.id, "messages": messages, "quickReplies": quick or [], "options": options or [],
                "review": review, "handoffDocument": handoff_document(self.state, self.id, review),
                "activeItemId": active_item.get("itemId") if isinstance(active_item, dict) else None,
                "order": deepcopy(self.state["order"]), "stage": self.state["stage"],
                "canonicalOrder": canonical_order(self.state["order"], self.state.get("fieldMeta")),
                "fieldCandidates": deepcopy(self.state.get("fieldCandidates") or []),
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

    def review(self) -> dict[str, Any]:
        uncertain = self._low_confidence_fields(production_only=True)
        items = self.state["order"].get("items") or []
        if len(items) > 1:
            for index, item in enumerate(items):
                prefix = f"items.{item['itemId']}."
                uncertain.extend(key if key.startswith("items.") else prefix + key
                                 for key in self._uncertain_fields_for_item(self._item_order(index), index))
        return review_contract(
            self.state,
            uncertain_fields=uncertain,
            validator=validate_order,
            capability_matcher=match_supplier_capability,
        )
