"""Bounded LLM planner loop, receipt ledger, and deterministic intent dispatch."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from printops.domain.knowledge import KNOWLEDGE_VERSION, parameter_state
from printops.domain.order_model import LABELS, _multi_product_info, required_order_keys
from printops.runtime.deadline import check_deadline
from printops.tools.registry import (
    TOOLS,
    estimate_price,
    explain_print_term,
    match_supplier_capability,
    validate_order,
)
from printops.agent.memory import (
    MAX_PLANNER_LEDGER_BYTES,
    MAX_PLANNER_LEDGER_ENTRIES,
    MAX_PLANNER_REQUIRED_CALLS,
    MAX_PLANNER_TOOL_ROUNDS,
    PLANNER_FAILURE_STATUSES,
    PLANNER_RETRYABLE_FAILURE_STATUSES,
)


class PlannerOrchestratorMixin:
    """Dual-track intent dispatch and bounded multi-round LLM planner coordination."""

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
    # Ordered intent dispatch table.  Within one phase the first matcher that
    # fires owns the turn, mirroring the historical if-chain exactly, so the
    # entry order is load-bearing.  ``matcher`` names a method; ``None`` means
    # the shared negation-aware ``_planner_intent(text, key)``.  A ``None``
    # handler marks a query-only entry that never routes a turn.
    INTENT_DISPATCH: tuple[tuple[str, str, str | None, str | None], ...] = (
        # phase, intent key, matcher override, handler
        ("pre_planner", "multi_generate", "_matches_multi_generate", "_handle_multi_generate"),
        ("post_planner", "explanation", "_matches_explanation", "_handle_explanation"),
        # Supplier-quote language wins over the generic estimate keywords.
        ("post_planner", "quote", None, "_handle_quote"),
        ("post_planner", "price", None, "_handle_price"),
        ("post_planner", "capability", None, "_handle_capability"),
        ("post_planner", "validation", None, "_handle_validation"),
        ("post_planner", "handoff", None, "_handle_handoff"),
        # Query-only: the planner turn reads it to decide whether the automatic
        # recommendation fallback is still required.  It never routes a turn.
        ("planner", "recommendation", None, None),
    )

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
            if status in PLANNER_RETRYABLE_FAILURE_STATUSES:
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

    def _matches_multi_generate(self, text: str) -> bool:
        """Aggregate generation stays an all-items operation, never an ordinal one."""
        return bool(_multi_product_info(self.state["order"])) \
            and self._planner_positive_intent(text, r"生成订单|生成草稿|下单")

    def _matches_explanation(self, text: str) -> bool:
        return self._is_explanation_request(text)

    def _intent_fired(self, text: str, key: str) -> bool:
        """Evaluate one table intent (negation-aware) without routing the turn."""
        for _phase, entry_key, matcher, _handler in self.INTENT_DISPATCH:
            if entry_key != key:
                continue
            return bool(getattr(self, matcher)(text) if matcher else self._planner_intent(text, key))
        return self._planner_intent(text, key)

    def _dispatch_intents(self, text: str, phase: str) -> dict[str, Any] | None:
        """Run one ordered phase of the intent table; the first match owns the turn."""
        for entry_phase, key, matcher, handler in self.INTENT_DISPATCH:
            if entry_phase != phase or handler is None:
                continue
            matches = getattr(self, matcher)(text) if matcher else self._planner_intent(text, key)
            if matches:
                return getattr(self, handler)(text)
        return None

    def _handle_multi_generate(self, text: str) -> dict[str, Any]:
        self._save()
        return self.generate()

    def _handle_explanation(self, text: str) -> dict[str, Any]:
        result = self._call("explain_print_term", text)
        message = f"{result['topic']}：{result['answer']}\n\n{result['next']}"
        self._remember("assistant", message)
        self._save()
        return self._result([message], tool_result=result)

    def _handle_quote(self, text: str) -> dict[str, Any]:
        active_index = self._item_index()
        result = self._prepare_quote_request(active_index, self.state["order"].get("platform"))
        message = result.get("message", "已准备询价请求，正式发送前需要人工确认。")
        self._remember("assistant", message)
        self._save()
        return self._result([message], tool_result=result)

    def _handle_price(self, text: str) -> dict[str, Any]:
        active_index = self._item_index()
        item_order = self._item_order(active_index) if active_index is not None else None
        result = self._estimate_reference(item_order, active_index)
        if item_order is not None:
            result = {**result, "itemId": item_order.get("itemId"), "itemIndex": active_index}
        self.state["workflowStage"] = "clarify" if result.get("status") == "blocked" else "quote"
        if result.get("status") == "blocked":
            message = self._estimate_message(result)
        else:
            message = (self._estimate_message(result)
                       if result["range"] else f"我调用了费用估算工具，但信息还不足。\n还需要：{'、'.join(result['missing'])}。")
        self._remember("assistant", message)
        self._save()
        return self._result([message], tool_result=result)

    def _handle_capability(self, text: str) -> dict[str, Any]:
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

    def _handle_validation(self, text: str) -> dict[str, Any]:
        result = self._call("validate_order", self.state["order"])
        message = self._validation_message(result)
        self._remember("assistant", message)
        self._save()
        return self._result([message], tool_result=result)

    def _handle_handoff(self, text: str) -> dict[str, Any]:
        self._save()
        return self.generate()

    def _planner_chat_turn(self, text: str) -> dict[str, Any] | None:
        """Run one bounded provider-planner turn; ``None`` falls through to local intents.

        The planner may consume its tool rounds and still leave the turn to the
        deterministic intent dispatch when it produced neither a tool result nor
        a usable reply.
        """
        if not (self.planner and getattr(self.planner, "enabled", False)):
            return None
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
        explicit_recommendation = self._intent_fired(text, "recommendation")
        if required_names == ["recommend_processes"] and not explicit_recommendation \
                and any(item.get("name") != "validate_order" and not item.get("stale")
                        and item.get("status") not in PLANNER_FAILURE_STATUSES
                        for item in self._planner_tool_receipts):
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
                and latest_required.get("status") in PLANNER_RETRYABLE_FAILURE_STATUSES
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
                receipt and not receipt.get("stale")
                and receipt.get("status") in PLANNER_FAILURE_STATUSES
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
        return None

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
        if isinstance(result, dict) and result.get("status") in PLANNER_FAILURE_STATUSES:
            if result.get("message"):
                return str(result["message"])
            if result.get("text"):
                return str(result["text"])
            if result.get("assumptions"):
                # A list rendered with str() leaks Python syntax ("['…']")
                # into the chat; join it into one readable sentence.
                assumptions = result["assumptions"]
                items = assumptions if isinstance(assumptions, list) else [assumptions]
                return "；".join(str(item) for item in items)
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
                return self._estimate_message(result)
            return (self._estimate_message(result)
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

    @staticmethod
    def _is_explanation_request(text: str) -> bool:
        return bool(re.search(r"怎么选|如何选|什么区别|有什么区别|是什么|解释|为什么", text)) and bool(
            re.search(r"纸|出血|安全边|裁切|覆膜|哑膜|亮膜|专色|四色|黑白|装订|骑马钉|胶装|工艺", text, re.I)
        )
