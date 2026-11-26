"""Option selection, preflight binding, quote lifecycle, handoff generation, and confirmation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
import uuid

from printops.domain.order_contract import handoff_document, production_fingerprint
from printops.domain.order_model import (
    LABELS,
    MATERIAL_SPEC_PRODUCTS,
    _multi_product_info,
    quote_idempotency_key,
)
from printops.supplier.adapters import PLATFORMS, get_adapter
from printops.supplier.connectors import REGISTRY
from printops.supplier.contract import UnknownProviderError
from printops.tools.registry import match_supplier_capability, validate_order
from printops.agent.memory import MAX_QUOTE_REQUESTS, QUOTE_ACTIVE_STATUSES


class DeliveryLifecycleMixin:
    """Stateful lifecycle for process options, preflight checks, quotes, handoffs, and confirmation."""

    def _invalidate_delivery_state(self) -> None:
        """Invalidate generated handoff data after an order-affecting change."""
        self._mark_quote_requests_stale("订单字段、文件或目标平台发生变化")
        had_delivery = self.state.get("orderGenerated") or (self.state.get("confirmation") or {}).get("status") in {"confirmed", "pending", "stale"}
        self.state["orderGenerated"] = False
        self.state["handoff"] = None
        self.state["confirmation"] = ({"status": "stale", "staleReason": "订单字段、文件或目标平台发生变化"}
                                      if had_delivery else {"status": "not_ready"})

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
        return next((item for item in reversed(requests)
                     if isinstance(item, dict) and item.get("status") in QUOTE_ACTIVE_STATUSES), None)

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

    @staticmethod
    def _select_option(options: Any, option_id: Any) -> dict[str, Any] | None:
        """Find a cached option by id without trusting the persisted shape."""
        if not isinstance(options, list) or not isinstance(option_id, str):
            return None
        for item in options:
            if isinstance(item, dict) and item.get("id") == option_id:
                return item
        return None

    @staticmethod
    def _option_shape_errors(option: dict[str, Any], need_paper: bool) -> list[str]:
        """Return required option fields that are missing or malformed."""
        errors: list[str] = []
        finishing = option.get("finishing")
        if not isinstance(finishing, str) or not finishing.strip():
            errors.append("finishing")
        if need_paper:
            paper = option.get("paper")
            if not isinstance(paper, str) or not paper.strip():
                errors.append("paper")
        return errors

    @staticmethod
    def _option_title(option: dict[str, Any]) -> str:
        """Degrade a missing or malformed option title to a safe label."""
        title = option.get("title")
        if isinstance(title, str) and title.strip():
            return title
        option_id = option.get("id")
        return option_id if isinstance(option_id, str) and option_id.strip() else "所选方案"

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
            option = self._select_option(options, option_id)
            if not option:
                return self._result(["没有找到这个产品项的方案。"], [], options)
            need_paper = item_order.get("productType") not in MATERIAL_SPEC_PRODUCTS
            missing_fields = self._option_shape_errors(option, need_paper)
            if missing_fields:
                message = "该方案的数据不完整，无法直接应用。请重新获取推荐方案后再选择。"
                return self._result([message], [], options,
                                    tool_result={"status": "invalid_option", "itemId": item_id,
                                                 "itemIndex": index, "optionId": option_id,
                                                 "missingFields": missing_fields})
            updates = {"finishing": option["finishing"], "binding": option.get("binding", item_order.get("binding", ""))}
            if need_paper:
                updates["paper"] = option["paper"]
            self._update_item(index, updates, source="recommendation", confidence=0.9)
            self.state["order"]["items"][index]["selectedOption"] = option_id
            self._store_process_options(options, index)
            all_selected = all(item.get("selectedOption") for item in self.state["order"].get("items", []) if isinstance(item, dict))
            self.state["workflowStage"] = "confirm" if all_selected else "clarify"
            message = f"已为第 {index + 1} 项选择{self._option_title(option)}，该项参数已更新。"
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
        option = self._select_option(options, option_id)
        if not option:
            return self._result(["没有找到这个方案。"], [], options)
        need_paper = self.state["order"].get("productType") not in MATERIAL_SPEC_PRODUCTS
        missing_fields = self._option_shape_errors(option, need_paper)
        if missing_fields:
            message = "该方案的数据不完整，无法直接应用。请重新获取推荐方案后再选择。"
            return self._result([message], [], options,
                                tool_result={"status": "invalid_option", "optionId": option_id,
                                             "missingFields": missing_fields})
        updates = {"finishing": option["finishing"], "binding": option.get("binding", self.state["order"]["binding"])}
        if need_paper:
            updates["paper"] = option["paper"]
        self._update_order(updates, source="recommendation", confidence=0.9)
        self.state["selectedOption"] = option_id
        self._store_process_options(options)
        self.state["workflowStage"] = "confirm"
        message = f"已选择{self._option_title(option)}，订单参数已更新。"
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
            uncertain = list(dict.fromkeys(field for index in range(len(self.state["order"].get("items", [])))
                                           for field in self._uncertain_fields_for_item(self._item_order(index), index)))
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
                         "text": "\n\n".join(sections), "requiresHumanConfirmation": True,
                         "orderFingerprint": production_fingerprint(self.state), "generatedAt": self._timestamp()}
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
        if handoff.get("status") != "ready":
            return self._result(["当前参数未能生成有效交接草稿。"], tool_result=handoff)
        handoff.update({"orderFingerprint": production_fingerprint(self.state), "generatedAt": self._timestamp()})
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
        review = self.review()
        if not review["canExport"] or ((self.state.get("handoff") or {}).get("orderFingerprint")
                and self.state["handoff"]["orderFingerprint"] != production_fingerprint(self.state)):
            return self._result(["订单规格仍需核对或交接单已失效，请处理待办后重新生成。"],
                                tool_result={"status": "blocked", "reason": "review_required"})
        if confirmation.get("status") == "confirmed":
            return self._result(["该订单交接单已经确认，无需重复确认。"], handoff=self.state.get("handoff"),
                                tool_result={"status": "confirmed", **confirmation})
        confirmed_at = self._timestamp()
        self.state["confirmation"] = {"status": "confirmed", "confirmedAt": confirmed_at,
                                       "note": str(note or "").strip()[:240],
                                       "orderFingerprint": production_fingerprint(self.state)}
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
        self.state["confirmation"]["confirmedRevision"] = self.state["revision"] + 1
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
        try:
            provider = REGISTRY.get(platform_id).metadata()
        except UnknownProviderError:
            return self._result(["未找到供应商接口，请选择已注册的供应商。"],
                                tool_result={"status": "invalid_arguments", "reason": "unknown_provider"})
        self._update_order({"platform": platform_id}, source="user", confidence=1.0)
        self.state["workflowStage"] = self._base_workflow_stage()
        self._save()
        return self._result([f"目标平台已切换为{provider['name']}。"])
