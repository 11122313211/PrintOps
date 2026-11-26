"""OpenAI-compatible planner orchestrating transport, budget, protocol, probe, and validation."""

from __future__ import annotations

from copy import copy, deepcopy
import json
import os
import sys
from typing import Any
import urllib.error
from urllib.error import HTTPError, URLError
import urllib.request

from printops.runtime.deadline import (
    DeadlineExceeded,
    deadline_sleep as _default_deadline_sleep,
    operation_deadline,
    remaining,
)
from printops.llm.budget import BudgetMixin
from printops.llm.plan_validator import PlanValidatorMixin
from printops.llm.probe import ProbeMixin
from printops.llm.protocol import PLANNER_PROMPT_VERSION, ProtocolMixin
from printops.llm.transport import TransportMixin, _close_error_response, normalize_base_url


def _adapter_sleep(seconds: float, deadline: float | None) -> None:
    """Call adapter-level deadline_sleep so monkeypatches on llm_adapter.deadline_sleep work."""
    adapter_mod = sys.modules.get("printops.llm.adapter")
    sleep_fn = getattr(adapter_mod, "deadline_sleep", _default_deadline_sleep) if adapter_mod else _default_deadline_sleep
    sleep_fn(seconds, deadline)


class OpenAICompatiblePlanner(
    TransportMixin,
    BudgetMixin,
    ProtocolMixin,
    ProbeMixin,
    PlanValidatorMixin,
):
    MAX_ATTEMPTS = 2
    RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
    # TD-12: per-class deterministic backoff. A rate-limit reply (429) gets
    # exponential spacing; the remaining retryable HTTP codes keep short
    # linear spacing; connection failures back off exponentially. Values stay
    # small so the retries never eat the shared request deadline, and every
    # sleep goes through deadline_sleep so the deadline still wins.
    RETRY_BACKOFF_RATE_LIMIT_SECONDS = 0.5
    RETRY_BACKOFF_SERVER_SECONDS = 0.15
    RETRY_BACKOFF_NETWORK_SECONDS = 0.3
    PATCH_FIELDS = {
        "productType", "purpose", "quantity", "quantityValue", "quantityUnit", "size", "dimensions", "pages", "orientation", "paper",
        "printing", "finishing", "binding", "deadline", "budget", "platform", "productSpecs",
    }
    MAX_EVIDENCE_QUOTE = 2048
    MAX_EVIDENCE_SOURCE = 128
    MAX_PATCH_VALUE = 4096
    MAX_METADATA_FIELDS = 128
    MAX_PLAN_ANNOTATIONS = 32
    MAX_PLAN_ANNOTATION_TEXT = 1024
    MAX_PLAN_META_VERSION = 128
    MAX_CONTEXT_HISTORY = 8
    MAX_CONTEXT_MESSAGE_CHARS = 2000
    MAX_CONTEXT_VALUE_DEPTH = 5
    MAX_CONTEXT_LIST_ITEMS = 24
    MAX_CONTEXT_OBJECT_KEYS = 64
    MAX_CONTEXT_ORDER_BYTES = 12000
    MAX_CONTEXT_TOOL_RESULT_BYTES = 12000
    # The per-field limits above protect individual values.  This aggregate
    # cap protects the complete HTTP request (system prompt, history, tool
    # schemas and the bounded ledger) from silently growing beyond a provider
    # context window.  It is intentionally configurable for small gateways
    # and tests.
    MAX_CONTEXT_REQUEST_BYTES = 96 * 1024
    MAX_CONTEXT_LEDGER_ENTRIES = 8
    MAX_CONTEXT_LEDGER_BYTES = 32 * 1024
    MAX_TOOL_DESCRIPTION = 512
    CAPABILITY_PROBE_TOTAL_SECONDS = 26.0
    # One probe hop is the unit of probe latency.  The floor keeps a fast
    # endpoint from being judged inside a two-second window; the ceiling lets a
    # reasoning model answer within PRINTOPS_LLM_TIMEOUT without letting one
    # hung hop eat the whole probe (see docs/RUNTIME_BUDGET.md).
    CAPABILITY_PROBE_REQUEST_SECONDS = 6.0
    CAPABILITY_PROBE_REQUEST_MAX_SECONDS = 15.0
    # cc-switch's health check is a header-only GET on the base URL: any HTTP
    # answer proves the endpoint is live, and only a network-level failure is
    # decisive.  A refused port costs about two seconds on Windows and a
    # firewalled one the whole TCP retransmission budget, so liveness must be
    # settled by a cheap GET next to the chat hop instead of by waiting on a
    # generation request.  Both waits below are therefore deliberately short.
    CAPABILITY_PROBE_CONNECT_SECONDS = 2.5
    CAPABILITY_PROBE_GATE_SECONDS = 2.5
    MAX_CAPABILITY_RESPONSE_BYTES = 256 * 1024
    MAX_PLAN_RESPONSE_BYTES = 2 * 1024 * 1024
    CAPABILITY_TOOL_NAME = "printops_capability_probe"
    CAPABILITY_TOKEN = "printops-probe-v1"

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "", timeout: int = 20,
                 allow_private_hosts: bool = False) -> None:
        self.allow_private_hosts = bool(allow_private_hosts)
        try:
            safe_url = normalize_base_url(base_url, allow_private_hosts=self.allow_private_hosts)
            config_error = ""
        except ValueError:
            safe_url = ""
            config_error = "接口 URL 格式不正确"
        self.base_url, self.api_key, self.model, self.timeout = safe_url, (api_key or "").strip(), (model or "").strip(), timeout
        self.last_error = config_error
        self.last_protocol = ""

    def for_request(self) -> "OpenAICompatiblePlanner":
        """Freeze config without repeating DNS; diagnostics belong to this run."""
        planner = copy(self)
        planner.last_error = ""
        planner.last_protocol = ""
        return planner

    @classmethod
    def from_env(cls) -> "OpenAICompatiblePlanner":
        allow_private = os.getenv("PRINTOPS_ALLOW_PRIVATE_LLM_HOSTS", "").strip().lower() in {"1", "true", "yes", "on"}
        return cls(os.getenv("PRINTOPS_LLM_URL", ""), os.getenv("PRINTOPS_LLM_KEY", ""),
                   os.getenv("PRINTOPS_LLM_MODEL", ""), timeout=cls._timeout_from_env(),
                   allow_private_hosts=allow_private)

    def configure(self, base_url: str, model: str, api_key: str = "") -> None:
        self.base_url = normalize_base_url(base_url, allow_private_hosts=self.allow_private_hosts)
        self.model = (model or "").strip()
        self.api_key = (api_key or "").strip()
        self.last_error = ""
        self.last_protocol = ""

    def public_config(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "url": self.base_url, "model": self.model,
                "keyConfigured": bool(self.api_key), "lastError": self.last_error,
                "privateHostsAllowed": self.allow_private_hosts,
                "protocolMode": self.last_protocol or "auto"}

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    def plan(self, text: str, order: dict[str, Any], tools: list[dict[str, Any]],
             history: list[dict[str, str]] | None = None,
             tool_result: dict[str, Any] | None = None,
             tool_call: dict[str, Any] | None = None,
             tool_ledger: list[dict[str, Any]] | None = None,
             context_budget: int | None = None,
             **_kwargs: Any) -> dict[str, Any] | None:
        """Ask an OpenAI-compatible provider using native tools with JSON fallback.

        ``tool_ledger`` is optional so the adapter remains source-compatible
        with older Agent/custom planner implementations.  When supplied, the
        complete bounded ledger is replayed as native assistant/tool messages,
        or as one ``toolResults`` array in the JSON fallback envelope.
        """
        if not self.enabled:
            return None
        try:
            request_deadline = operation_deadline(self.timeout)
        except DeadlineExceeded:
            self.last_error, self.last_protocol = "请求总时限已到，模型结果未应用", "timeout"
            return None
        allow_tools = _kwargs.get("allow_tools", True) is not False
        if context_budget is None:
            candidate_budget = _kwargs.get("max_context_bytes", _kwargs.get("context_bytes"))
            if isinstance(candidate_budget, int) and not isinstance(candidate_budget, bool):
                context_budget = candidate_budget
        prompt = {"role": "system", "content": (
            "你是印刷订单助手，使用简洁自然的中文和非专业用户对话。"
            "本接口优先使用 OpenAI-compatible native function/tool calls；如果服务端不支持 tools，"
            "会改用 JSON fallback。两种协议都必须返回一个 PrintOps JSON 对象："
            "{reply:string,patch:object,confidence:object,evidence:array,questions:array,risks:array,"
            "knowledgeVersion:string,tool:{name:string,arguments:object}|null}。"
            "native tool call 的 arguments 必须是 JSON 对象；JSON fallback 也必须把 tool 写在该对象内。"
            "reply 是给用户看的自然语言，必须在需要时提出下一步问题；patch 只能使用订单字段 "
            "productType,purpose,quantity,quantityValue,quantityUnit,size,dimensions,pages,orientation,paper,"
            "printing,finishing,binding,deadline,budget,platform,productSpecs。quantity 是给用户看的数量文本，"
            "quantityValue 是数字，quantityUnit 是张、份、个等单位；三者不一致时只提交 quantity，让系统统一规范化。"
            "dimensions 只能包含 finishedSize、expandedSize、dieCutSize、packageSize。品类专属参数必须放在 "
            "productSpecs 对象中（例如 folding、paperParts、boxSize、boxStructure、labelMaterial、labelShape、"
            "bagSize、handle、cupVolume、displayMaterial、install、boardThickness）。confidence 是字段路径到 "
            "0~1 数字的对象；evidence 是 [{field,quote,source}]，quote 必须是支持该字段的原文短引文，source 填 "
            "user、rule 或 model。questions 和 risks 只能记录待确认事项，不得替代确定性校验；knowledgeVersion 必须来自 "
            "当前 PrintOps 知识上下文。未知品类参数不要放进 patch，可列入 rejectedFields。工具使用规则：信息不完整时优先调用 "
            "validate_order；订单核心字段完整且用户需要方案时调用 recommend_processes；用户问费用时调用 estimate_price；"
            "术语问题调用 explain_print_term；只有订单满足系统前置条件时才调用询价或交接工具。工具调用必须使用当前可用工具名和 "
            "JSON 对象参数，不要把完整 order 作为参数，系统会提供当前会话订单。收到工具结果后，要么调用下一步不同的必要工具，"
            "要么给出总结，不要重复同一工具。不要编造价格、供应商能力或已提交订单。"
        )}

        history_messages: list[dict[str, Any]] = []
        for item in (history or [])[-self.MAX_CONTEXT_HISTORY:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role") if item.get("role") in {"user", "assistant"} else "user"
            content = item.get("text", item.get("content", ""))
            content = str(content or "").strip()
            if content:
                history_messages.append({"role": role,
                                         "content": self._clip_context_text(content, self.MAX_CONTEXT_MESSAGE_CHARS)})

        provider_tools = self._provider_tools(tools) if allow_tools else []
        compact_catalog = self._compact_tool_catalog(tools) if allow_tools else []
        ledger_source: Any = tool_ledger
        raw_batch_calls = _kwargs.get("tool_calls")
        if not ledger_source and isinstance(raw_batch_calls, list):
            # A transitional Agent may pass batch call metadata before it has
            # started persisting receipts.  Keep every call visible and attach
            # the supplied result to the newest one.
            ledger_source = [
                {"call": call,
                 "result": tool_result if index == len(raw_batch_calls) - 1 else None}
                for index, call in enumerate(raw_batch_calls)
                if isinstance(call, dict)
            ]
        entries = self._normalize_tool_ledger(ledger_source, tool_result=tool_result, tool_call=tool_call)
        ledger_context = self._ledger_context(entries)

        # The current user payload is built once and then copied for each
        # protocol variant.  This is the key invariant that prevents a
        # native->JSON retry from appending a second copy of the user turn.
        common_user: dict[str, Any] = {
            "text": self._clip_context_text(str(text or ""), self.MAX_CONTEXT_MESSAGE_CHARS),
            "order": self._bounded_context_object(order, self.MAX_CONTEXT_ORDER_BYTES),
            "protocol": "native_tools_or_json_fallback",
        }

        def add_ledger(payload: dict[str, Any]) -> None:
            if not ledger_context:
                return
            payload["toolResults"] = deepcopy(ledger_context)
            # Keep the old singular field for adapters that only understand
            # one observation; the array remains the authoritative ledger.
            payload["toolResult"] = self._bounded_context_object(
                tool_result if tool_result is not None else ledger_context[-1].get("result"),
                self.MAX_CONTEXT_TOOL_RESULT_BYTES,
            )

        native_user = deepcopy(common_user)
        if provider_tools:
            # Keep the compact catalog on the first request for older
            # OpenAI-compatible gateways that inspect the user envelope
            # instead of the protocol-level ``tools`` field.  Once a ledger
            # exists, names are sufficient because the full schemas remain in
            # the protocol field and this avoids repeating descriptions.
            native_user["tools"] = (compact_catalog if not entries else
                                     [{"name": item["function"]["name"]} for item in provider_tools])
        else:
            native_user["tools"] = []
            # With native tools disabled (including the final synthesis turn),
            # the JSON user envelope is the only observation channel.
            add_ledger(native_user)
        native_messages: list[dict[str, Any]] = [prompt, *history_messages,
                                                 {"role": "user", "content": json.dumps(native_user, ensure_ascii=False)}]
        if entries and allow_tools:
            native_messages = self._native_ledger_messages(native_messages, entries)
        native_payload: dict[str, Any] = {"model": self.model, "messages": native_messages}
        if provider_tools:
            native_payload["tools"] = provider_tools
            native_payload["tool_choice"] = "auto"
        native_payload = self._fit_request_budget(native_payload, context_budget)

        payload_variants: list[dict[str, Any]] = [native_payload]
        if provider_tools:
            # Older gateways reject protocol-level ``tools``.  Rebuild from
            # system + history + exactly one current user payload; native
            # assistant/tool messages are represented by ``toolResults``.
            fallback_user = deepcopy(common_user)
            add_ledger(fallback_user)
            fallback_user["tools"] = compact_catalog
            fallback_messages: list[dict[str, Any]] = [prompt, *history_messages,
                                                        {"role": "user", "content": json.dumps(
                                                            fallback_user, ensure_ascii=False)}]
            fallback_payload = self._fit_request_budget(
                {"model": self.model, "messages": fallback_messages}, context_budget)
            payload_variants.append(fallback_payload)

        self.last_error = ""
        valid_tool_names = ({item.get("name") for item in tools if isinstance(item, dict)}
                            if allow_tools else set())
        protocol_before_request = self.last_protocol
        for variant_index, variant in enumerate(payload_variants):
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=json.dumps(variant, ensure_ascii=False).encode(), method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            )
            for attempt in range(self.MAX_ATTEMPTS):
                retryable = False
                backoff_error: Exception | None = None
                try:
                    result = self._request_json(request, request_deadline, self.MAX_PLAN_RESPONSE_BYTES)
                    native_plan = self._native_tool_plan(result) if variant_index == 0 and provider_tools else None
                    if native_plan is not None:
                        plan = self.validate_plan(native_plan, valid_tool_names)
                        if plan is not None:
                            self.last_protocol = native_plan.get("_nativeProtocol", "native_tools")
                            self.last_error = ""
                            return plan
                        # A malformed native envelope should get a JSON retry,
                        # rather than silently ending the Agent's tool loop.
                        self.last_error = "模型 native 工具响应无法识别"
                        if variant_index == 0 and len(payload_variants) > 1:
                            break
                    else:
                        # If the provider advertised native call fields but
                        # none could be decoded, force the JSON variant.  A
                        # malformed native call must not be mistaken for a
                        # successful text-only turn.
                        if variant_index == 0 and provider_tools and self._has_native_call_marker(result):
                            self.last_error = "模型 native 工具响应无法解析，尝试 JSON fallback"
                            if len(payload_variants) > 1:
                                break
                        content = self._response_content(result)
                        if content in (None, ""):
                            content = self._response_text(result)
                        plan = self.validate_plan(self._parse_plan(content), valid_tool_names)
                        if plan is not None:
                            if variant_index:
                                self.last_protocol = "json_fallback"
                            elif protocol_before_request == "native_tools":
                                # A native tool turn is commonly followed by
                                # a plain JSON synthesis response.  Preserve
                                # the session's effective protocol mode so UI
                                # diagnostics do not report a false downgrade.
                                self.last_protocol = "native_tools"
                            else:
                                self.last_protocol = "native_json"
                            self.last_error = ""
                            return plan
                        self.last_error = "模型返回内容无法识别"
                        if variant_index == 0 and len(payload_variants) > 1:
                            break
                except DeadlineExceeded:
                    self.last_error, self.last_protocol = "请求总时限已到，模型结果未应用", "timeout"
                    return None
                except HTTPError as error:
                    self.last_error = f"模型接口返回 HTTP {error.code}"
                    self.last_protocol = "error"
                    _close_error_response(error)
                    if variant_index == 0 and error.code in {400, 404, 405, 422}:
                        break
                    retryable = error.code in self.RETRYABLE_HTTP_CODES
                    backoff_error = error
                except (URLError, TimeoutError, OSError) as error:
                    self.last_error = "模型接口连接失败"
                    self.last_protocol = "error"
                    retryable = True
                    backoff_error = error
                except (KeyError, IndexError, TypeError, ValueError, RecursionError, json.JSONDecodeError):
                    self.last_error = "模型接口返回格式异常"
                    self.last_protocol = "error"
                    if variant_index == 0 and len(payload_variants) > 1:
                        break
                if retryable and attempt + 1 < self.MAX_ATTEMPTS:
                    try:
                        _adapter_sleep(self._retry_backoff_seconds(backoff_error, attempt), request_deadline)
                    except DeadlineExceeded:
                        self.last_error, self.last_protocol = "请求总时限已到，模型结果未应用", "timeout"
                        return None
        return None
