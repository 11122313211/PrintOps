"""Single authoritative tool execution gateway with validation, preconditions, and telemetry."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import math
import time
from typing import Any

from printops.domain.order_model import _multi_product_info
from printops.supplier.adapters import PLATFORMS
from printops.supplier.contract import UnknownProviderError
from printops.tools.registry import TOOLS, TOOL_META, TOOL_SCHEMAS, validate_order
from printops.agent.memory import (
    MAX_TOOL_FILE_NAME_LENGTH,
    MAX_TOOL_INSPECTION_BYTES,
    MAX_TOOL_NAME_LENGTH,
    MAX_TOOL_ORDER_BYTES,
    MAX_TOOL_SAFE_INTEGER,
    WORKFLOW_LABELS,
)


class ToolGatewayMixin:
    """Whitelist tool dispatch, payload guards, readiness gates, and telemetry recording."""

    READINESS_TOOLS = frozenset({"recommend_processes", "prepare_handoff", "request_supplier_quote"})
    CONFIRMATION_TOOLS = frozenset({"prepare_handoff", "request_supplier_quote"})

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

    def call_tool(self, name: str, payload: dict[str, Any] | None = None, preserve_trace: bool = False,
                  remember: bool = True) -> dict[str, Any]:
        """Public tool gateway used by a UI, MCP bridge, or an LLM planner.

        Every terminal state (ok, failed, blocked, rejected, or a raised tool
        error) is timed at this boundary and written to the optional store
        telemetry ring.  Recording is wrapped defensively: a statistics
        backend failure must never change the workflow outcome, so it is
        swallowed with a debug log.
        """
        started = time.monotonic()
        try:
            response = self._call_tool_impl(name, payload, preserve_trace, remember, started)
        except Exception as error:
            self._record_tool_call_telemetry(name, "failed", str(error) or error.__class__.__name__,
                                             started, payload)
            raise
        status, reason = self._tool_call_outcome(response)
        run_id = response.get("runId") if isinstance(response, dict) else None
        self._record_tool_call_telemetry(name, status, reason, started, payload,
                                         run_id=run_id if isinstance(run_id, str) else "")
        return response

    def _tool_call_outcome(self, response: Any) -> tuple[str, str]:
        """Map a gateway response to the four terminal telemetry states."""
        result = response.get("toolResult") if isinstance(response, dict) else None
        if isinstance(result, dict):
            status = result.get("status")
            if status == "blocked":
                return "blocked", str(result.get("reason") or "")
            if status == "error":
                return "failed", str(result.get("reason") or result.get("errorType") or "tool_error")
            if status == "invalid_arguments":
                return "rejected", str(result.get("reason") or "")
            return "ok", ""
        # Rejections without a tool payload record their terminal event in the
        # response run trace; the gateway appends it last, so scan from the end.
        for event in reversed(response.get("runTrace") or [] if isinstance(response, dict) else []):
            if isinstance(event, dict) and event.get("step") == "tool":
                status = event.get("status")
                if status in ("rejected", "blocked", "failed"):
                    return status, str(event.get("detail") or "")
                break
        return "rejected", ""

    def _record_tool_call_telemetry(self, name: Any, status: str, reason: Any, started: float,
                                    payload: Any = None, item_index: Any = None,
                                    run_id: Any = None) -> None:
        """Write one tool_call_log row; failures never propagate (v1.3.0)."""
        try:
            record = getattr(getattr(self.memory, "store", None), "record_tool_call", None)
            if not callable(record):
                return
            if isinstance(item_index, bool) or not isinstance(item_index, int):
                candidate = payload.get("itemIndex") if isinstance(payload, dict) else None
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    item_index = candidate
                else:
                    active = self.state.get("activeItemIndex")
                    item_index = active if isinstance(active, int) else None
            record({"tool_name": name if isinstance(name, str) and name.strip() else "unknown",
                    "status": status,
                    "reason": str(reason or ""),
                    "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
                    "session_id": self.id,
                    "run_id": run_id if isinstance(run_id, str) else self.run_id,
                    "item_index": item_index})
        except Exception as error:  # telemetry must never fail a tool call
            logging.getLogger(__name__).debug("tool call stats write skipped: %s",
                                              error.__class__.__name__)

    def _call_tool_impl(self, name: str, payload: dict[str, Any] | None, preserve_trace: bool,
                        remember: bool, started: float) -> dict[str, Any]:
        """Gateway body; ``started`` timestamps the rejected/blocked events."""
        valid_name = isinstance(name, str) and bool(name.strip()) and len(name.strip()) <= MAX_TOOL_NAME_LENGTH
        if not valid_name:
            if not preserve_trace:
                self._begin_run("tool:invalid")
                self.trace = []
            self.trace.append("工具名无效")
            self._event("tool", "rejected", "工具名必须是有限长度的非空字符串", tool="invalid",
                        durationMs=round((time.monotonic() - started) * 1000))
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
            self._event("tool", "rejected", f"工具 {name} 参数不是 JSON 对象", tool=name,
                        durationMs=round((time.monotonic() - started) * 1000))
            return self._tool_reply("工具参数需要使用 JSON 对象，未执行。", remember=remember)
        payload = dict(payload)
        payload_error = self._validate_call_tool_payload(name, payload)
        if payload_error:
            self.trace.append(f"工具参数无效：{name}")
            self._event("tool", "rejected", payload_error, tool=name,
                        durationMs=round((time.monotonic() - started) * 1000))
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
                            reason=str(reason or "precondition"),
                            durationMs=round((time.monotonic() - started) * 1000))
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
            result = self._estimate_reference(item_order, item_index)
            if item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            self.state["workflowStage"] = "clarify" if result.get("status") == "blocked" else "quote"
            message = (self._estimate_message(result) if result.get("status") == "blocked"
                       else self._estimate_message(result) if result["range"]
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
                                payload.get("inspection"), payload.get("expectedSize") or item_order.get("size"),
                                item_order.get("pages"), item_order.get("productType"))
            self.state["workflowStage"] = "preflight"
            self._store_preflight_result(result, item_index)
            if result.get("ok"):
                self._bind_uploaded_file(str(payload.get("fileName", "")), item_index)
                self._invalidate_delivery_state()
            if item_index is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            return self._tool_reply(result["message"], tool_result=result, remember=remember)
        self._event("tool", "rejected", f"工具 {name} 不在白名单中", tool=name,
                    durationMs=round((time.monotonic() - started) * 1000))
        return self._tool_reply(f"工具 {name} 不在白名单中，未执行。", remember=remember)

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
