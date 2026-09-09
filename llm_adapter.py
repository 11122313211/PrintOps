"""Optional OpenAI-compatible planner.

The deterministic Agent remains the fallback when no model is configured or
when a provider is temporarily unavailable.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import socket
import time
import urllib.request
from urllib.error import HTTPError, URLError
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from order_model import DIMENSION_DEFAULTS
from product_knowledge import known_product_spec_keys


def _reject_private_host(hostname: str | None) -> None:
    """SSRF guard: reject hosts that resolve to loopback/private/reserved space.

    Literal IPs are checked directly; resolvable hostnames are resolved once so
    an innocent-looking name pointing at internal space is rejected too. A name
    that cannot resolve at all is allowed through — it cannot reach an internal
    target, and the request itself will fail with a connection error.
    """
    name = (hostname or "").strip().lower().rstrip(".")
    if not name:
        raise ValueError("接口 URL 缺少主机名")
    if name == "localhost" or name.endswith(".localhost") or name.endswith(".local"):
        raise ValueError("接口 URL 不能指向本机或内网地址")
    try:
        addresses = [ipaddress.ip_address(name)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(name, None)
        except (OSError, UnicodeError):
            return
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    for ip in addresses:
        cgnat = ipaddress.ip_network("100.64.0.0/10") if ip.version == 4 else None
        if ip.is_loopback or ip.is_private or ip.is_reserved or ip.is_link_local \
                or ip.is_multicast or ip.is_unspecified or (cgnat and ip in cgnat):
            raise ValueError("接口 URL 不能指向本机或内网地址")


def normalize_base_url(value: str, allow_private_hosts: bool = False) -> str:
    """Validate an OpenAI-compatible HTTP endpoint without exposing credentials.

    Private/loopback targets remain blocked by default because this URL is
    user-controlled and the server makes the outbound request. Trusted local
    deployments can explicitly opt in for a company-internal model endpoint.
    """
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("接口 URL 必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise ValueError("接口 URL 缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("接口 URL 不应包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("接口 URL 不应包含查询参数或片段")
    if not allow_private_hosts:
        _reject_private_host(parsed.hostname)
    return value


def read_saved_config(path: str | Path) -> dict[str, str]:
    config_path = Path(path)
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data.get(key, "")) for key in ("url", "model", "key") if data.get(key) is not None}


def write_saved_config(path: str | Path, config: dict[str, str]) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass


class OpenAICompatiblePlanner:
    MAX_ATTEMPTS = 2
    RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
    RETRY_BACKOFF_SECONDS = 0.08
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
    MAX_TOOL_DESCRIPTION = 512

    @staticmethod
    def _is_patch_scalar(value: Any) -> bool:
        """Only admit finite text/numeric scalars into the model patch."""
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return False
        return not (isinstance(value, float) and not math.isfinite(value))

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

    @classmethod
    def from_env(cls) -> "OpenAICompatiblePlanner":
        allow_private = os.getenv("PRINTOPS_ALLOW_PRIVATE_LLM_HOSTS", "").strip().lower() in {"1", "true", "yes", "on"}
        return cls(os.getenv("PRINTOPS_LLM_URL", ""), os.getenv("PRINTOPS_LLM_KEY", ""),
                   os.getenv("PRINTOPS_LLM_MODEL", ""), allow_private_hosts=allow_private)

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

    @staticmethod
    def _provider_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate the internal tool catalog to OpenAI function schemas.

        The Agent remains the authority for session state, so the internal
        ``order`` argument is deliberately removed from model-facing schemas.
        The planner may request a tool and small selectors such as ``itemIndex``
        or ``platformId``; the gateway supplies the current order itself.
        """
        provider_tools: list[dict[str, Any]] = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            schema = item.get("input")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            schema = json.loads(json.dumps(schema, ensure_ascii=False))
            properties = schema.get("properties")
            if not isinstance(properties, dict):
                properties = {}
                schema["properties"] = properties
            properties.pop("order", None)
            required = schema.get("required")
            if isinstance(required, list):
                schema["required"] = [key for key in required if key != "order"]
            provider_tools.append({
                "type": "function",
                "function": {
                    "name": name.strip(),
                    "description": str(item.get("description") or "").strip(),
                    "parameters": schema,
                },
            })
        return provider_tools

    @classmethod
    def _compact_tool_catalog(cls, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep a small JSON fallback catalog without duplicating output schemas."""
        catalog: list[dict[str, Any]] = []
        for item in cls._provider_tools(tools):
            function = item["function"]
            parameters = function.get("parameters", {"type": "object", "properties": {}})
            if isinstance(parameters, dict):
                compact_parameters: dict[str, Any] = {
                    key: parameters[key] for key in ("type", "required") if key in parameters
                }
                properties = parameters.get("properties")
                if isinstance(properties, dict):
                    compact_properties: dict[str, Any] = {}
                    for name, schema in properties.items():
                        if not isinstance(schema, dict):
                            continue
                        compact_schema = {
                            key: schema[key] for key in ("type", "description", "enum", "items")
                            if key in schema
                        }
                        compact_properties[name] = compact_schema
                    compact_parameters["properties"] = compact_properties
                parameters = compact_parameters
            catalog.append({
                "name": function["name"],
                "description": function.get("description", "")[:cls.MAX_TOOL_DESCRIPTION],
                "input": parameters,
            })
        return catalog

    @staticmethod
    def _clip_context_text(value: str, limit: int) -> str:
        """Keep both the beginning and end of model context when clipping text."""
        text = str(value)
        if len(text) <= limit:
            return text
        if limit < 32:
            return text[:limit]
        marker = "...[已截断]..."
        side = (limit - len(marker)) // 2
        return f"{text[:side]}{marker}{text[-side:]}"

    @staticmethod
    def _clip_context_text_bytes(value: str, limit: int) -> str:
        """Clip UTF-8 text by bytes so the transport budget is real."""
        text = str(value)
        raw = text.encode("utf-8")
        if len(raw) <= limit:
            return text
        marker = "...[上下文已截断]..."
        marker_bytes = len(marker.encode("utf-8"))
        side_bytes = max(1, (limit - marker_bytes) // 2)
        head = raw[:side_bytes].decode("utf-8", "ignore")
        tail = raw[-side_bytes:].decode("utf-8", "ignore")
        return f"{head}{marker}{tail}"

    @classmethod
    def _compact_context_value(cls, value: Any, depth: int = 0) -> Any:
        """Bound nested provider context without changing the local tool result.

        Tool output is trusted local data, but it can contain a large order,
        evidence, or repeated option arrays. The model only needs a bounded
        view; the complete result remains in the Agent response and trace.
        """
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return cls._clip_context_text(value, cls.MAX_CONTEXT_MESSAGE_CHARS)
        if depth >= cls.MAX_CONTEXT_VALUE_DEPTH:
            return "[上下文层级已省略]"
        if isinstance(value, dict):
            compact: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= cls.MAX_CONTEXT_OBJECT_KEYS:
                    compact["_truncatedKeys"] = len(value) - index
                    break
                safe_key = cls._clip_context_text(str(key), 128)
                compact[safe_key] = cls._compact_context_value(item, depth + 1)
            return compact
        if isinstance(value, (list, tuple)):
            compact_list = [cls._compact_context_value(item, depth + 1)
                            for item in value[:cls.MAX_CONTEXT_LIST_ITEMS]]
            if len(value) > cls.MAX_CONTEXT_LIST_ITEMS:
                compact_list.append({"_truncatedItems": len(value) - cls.MAX_CONTEXT_LIST_ITEMS})
            return compact_list
        return cls._clip_context_text(str(value), cls.MAX_CONTEXT_MESSAGE_CHARS)

    @classmethod
    def _bounded_context_object(cls, value: Any, max_bytes: int) -> Any:
        """Return a JSON-safe context object within a byte budget."""
        compact = cls._compact_context_value(value)
        try:
            encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError, OverflowError, RecursionError):
            return {"_contextError": "工具结果无法编码"}
        if len(encoded.encode("utf-8")) <= max_bytes:
            return compact
        # The common oversized case is a list of options/evidence. Keep the
        # shape and a deterministic prefix so the provider can still summarize.
        if isinstance(compact, dict):
            reduced = dict(compact)
            for key in ("options", "evidence", "items", "toolTrace", "events", "history"):
                if isinstance(reduced.get(key), list) and len(reduced[key]) > 3:
                    reduced[key] = reduced[key][:3] + [{"_truncatedItems": "更多内容已保留在本地结果"}]
            try:
                encoded = json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode("utf-8")) <= max_bytes:
                    return reduced
            except (TypeError, ValueError, OverflowError, RecursionError):
                pass
        summary_limit = max(64, max_bytes - 96)
        for _ in range(4):
            result = {"_contextTruncated": True,
                      "summary": cls._clip_context_text_bytes(encoded, summary_limit)}
            try:
                if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= max_bytes:
                    return result
            except (TypeError, ValueError, OverflowError, RecursionError):
                return {"_contextError": "工具结果无法编码"}
            summary_limit = max(32, int(summary_limit * 0.75))
        return {"_contextTruncated": True, "summary": "[上下文超出传输预算]"}

    @staticmethod
    def _response_message(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return {}
        message = choices[0].get("message")
        return message if isinstance(message, dict) else {}

    @classmethod
    def _native_tool_plan(cls, result: Any) -> dict[str, Any] | None:
        """Convert a Chat Completions ``tool_calls`` response to our plan envelope."""
        message = cls._response_message(result)
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list) or not raw_calls:
            return None
        calls: list[dict[str, Any]] = []
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            raw_arguments = function.get("arguments", {})
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
                except json.JSONDecodeError:
                    continue
            else:
                arguments = raw_arguments
            if not isinstance(arguments, dict):
                continue
            call_id = raw_call.get("id")
            calls.append({
                "id": str(call_id)[:256] if isinstance(call_id, str) and call_id else "",
                "type": "function",
                "function": {"name": name.strip(), "arguments": arguments},
            })
        if not calls:
            return None
        content = message.get("content")
        reply = str(content).strip() if isinstance(content, str) else ""
        plan: dict[str, Any] = {
            "reply": reply,
            "patch": {},
            "tool": {
                "name": calls[0]["function"]["name"],
                "arguments": calls[0]["function"]["arguments"],
            },
            "_nativeToolCall": calls[0],
        }
        if len(calls) > 1:
            # The current Agent executes one bounded call per planning round.
            # Keep the extra count visible without silently executing a batch.
            plan["rejectedFields"] = [f"extraToolCalls:{len(calls) - 1}"]
        return plan

    def plan(self, text: str, order: dict[str, Any], tools: list[dict[str, Any]],
             history: list[dict[str, str]] | None = None,
             tool_result: dict[str, Any] | None = None,
             tool_call: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        prompt = {"role": "system", "content": "你是印刷订单助手，使用简洁自然的中文和非专业用户对话。每次只输出一个 JSON 对象，不要 Markdown 代码围栏：{reply:string,patch:object,confidence:object,evidence:array,questions:array,risks:array,knowledgeVersion:string,tool:{name:string,arguments:object}|null}。reply 是给用户看的自然语言，必须在需要时提出下一步问题；patch 只能使用订单字段 productType,purpose,quantity,quantityValue,quantityUnit,size,dimensions,pages,orientation,paper,printing,finishing,binding,deadline,budget,platform,productSpecs。quantity 是给用户看的数量文本，quantityValue 是数字，quantityUnit 是张、份、个等单位；三者不一致时只提交 quantity，让系统统一规范化。dimensions 只能包含 finishedSize、expandedSize、dieCutSize、packageSize。品类专属参数必须放在 productSpecs 对象中（例如 folding、paperParts、boxSize、boxStructure、labelMaterial、labelShape、bagSize、handle、cupVolume、displayMaterial、install、boardThickness）。confidence 是字段路径到 0~1 数字的对象；evidence 是 [{field,quote,source}]，quote 必须是支持该字段的原文短引文，source 填 user、rule 或 model。questions 和 risks 只能记录待确认事项，不得替代确定性校验；knowledgeVersion 必须来自当前 PrintOps 知识上下文。未知品类参数不要放进 patch，可列入 rejectedFields。工具使用规则：信息不完整时优先调用 validate_order；订单核心字段完整且用户需要方案时调用 recommend_processes；用户问费用时调用 estimate_price；术语问题调用 explain_print_term；只有订单满足系统前置条件时才调用询价或交接工具。工具调用必须使用当前可用工具名和 JSON 对象参数，不要把完整 order 作为参数，系统会提供当前会话订单。收到工具结果后，要么调用下一步不同的必要工具，要么给出总结，不要重复同一工具。不要编造价格、供应商能力或已提交订单。"}
        messages = [prompt]
        for item in (history or [])[-self.MAX_CONTEXT_HISTORY:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role") if item.get("role") in {"user", "assistant"} else "user"
            content = str(item.get("text", "")).strip()
            if content:
                if len(content) > self.MAX_CONTEXT_MESSAGE_CHARS:
                    content = content[-self.MAX_CONTEXT_MESSAGE_CHARS:]
                messages.append({"role": role, "content": content})
        provider_tools = self._provider_tools(tools)
        compact_catalog = self._compact_tool_catalog(tools)
        user_payload: dict[str, Any] = {
            "text": self._clip_context_text(str(text or ""), self.MAX_CONTEXT_MESSAGE_CHARS),
            "order": self._bounded_context_object(order, self.MAX_CONTEXT_ORDER_BYTES),
            # Native tool schemas travel in the protocol-level ``tools`` field.
            # Keep only names in the message so JSON fallback can add schemas
            # without paying for a duplicate catalog on every native round.
            "tools": (compact_catalog if provider_tools and tool_call is None else
                      [{"name": item["function"]["name"]} for item in provider_tools]
                      if provider_tools else compact_catalog),
        }
        if tool_result is not None and tool_call is None:
            user_payload["toolResult"] = self._bounded_context_object(
                tool_result, self.MAX_CONTEXT_TOOL_RESULT_BYTES)
        messages.append({"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)})
        if tool_call and tool_result is not None:
            call_id = str(tool_call.get("id") or "call_printops")[:256]
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            call_name = str(function.get("name") or "")[:128]
            call_arguments = function.get("arguments") if isinstance(function.get("arguments"), dict) else {}
            messages.append({"role": "assistant", "content": None, "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": call_name, "arguments": json.dumps(call_arguments, ensure_ascii=False)},
            }]})
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": json.dumps(
                                 self._bounded_context_object(
                                     tool_result.get("result", tool_result),
                                     self.MAX_CONTEXT_TOOL_RESULT_BYTES),
                                 ensure_ascii=False)})
        payload = {"model": self.model, "messages": messages}
        if provider_tools:
            payload["tools"] = provider_tools
            payload["tool_choice"] = "auto"
        payload_variants = [payload]
        if provider_tools:
            # Some older OpenAI-compatible gateways reject unknown tools fields.
            # Retry once without native tools while retaining the JSON contract.
            # The fallback message needs the compact schemas because the
            # protocol-level ``tools`` field is intentionally absent there.
            fallback_payload = dict(user_payload)
            fallback_payload["tools"] = compact_catalog
            if tool_result is not None and tool_call is not None:
                fallback_payload["toolResult"] = self._bounded_context_object(
                    tool_result, self.MAX_CONTEXT_TOOL_RESULT_BYTES)
                fallback_base_messages = messages[:-2]
            else:
                fallback_base_messages = messages[:-1]
            fallback_messages = [*fallback_base_messages, {
                "role": "user", "content": json.dumps(fallback_payload, ensure_ascii=False),
            }]
            payload_variants.append({"model": self.model, "messages": fallback_messages})
        self.last_error = ""
        for variant_index, variant in enumerate(payload_variants):
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=json.dumps(variant, ensure_ascii=False).encode(), method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            )
            for attempt in range(self.MAX_ATTEMPTS):
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        result = json.loads(response.read().decode("utf-8"))
                    plan = self._native_tool_plan(result)
                    if plan is None:
                        content = self._response_text(result)
                        plan = self._parse_plan(content)
                        self.last_protocol = "native_tools" if tool_call is not None else "json_fallback"
                    else:
                        self.last_protocol = "native_tools"
                    plan = self.validate_plan(plan, {item.get("name") for item in tools if isinstance(item, dict)})
                    if plan is None:
                        self.last_error = "模型返回内容无法识别"
                    else:
                        self.last_error = ""
                    return plan
                except HTTPError as error:
                    self.last_error = f"模型接口返回 HTTP {error.code}"
                    self.last_protocol = "error"
                    if variant_index == 0 and error.code in {400, 404, 405, 422}:
                        break
                    retryable = error.code in self.RETRYABLE_HTTP_CODES
                except (URLError, TimeoutError, OSError):
                    self.last_error = "模型接口连接失败"
                    self.last_protocol = "error"
                    retryable = True
                except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
                    self.last_error = "模型接口返回格式异常"
                    self.last_protocol = "error"
                    retryable = False
                if retryable and attempt + 1 < self.MAX_ATTEMPTS:
                    time.sleep(self.RETRY_BACKOFF_SECONDS * (attempt + 1))
        return None

    def test_connection(self, base_url: str | None = None, model: str | None = None,
                        api_key: str | None = None) -> dict[str, Any]:
        """Check a provider without sending order data or exposing the key."""
        target_url = self.base_url if base_url is None else normalize_base_url(
            base_url, allow_private_hosts=self.allow_private_hosts)
        target_model = self.model if model is None else (model or "").strip()
        target_key = self.api_key if api_key is None else (api_key or "").strip()
        if not (target_url and target_model):
            return {"ok": False, "message": "模型接口尚未配置", "latencyMs": None}
        payload = {
            "model": target_model,
            "messages": [{"role": "user", "content": "只回复 OK"}],
            "max_tokens": 4,
        }
        request = urllib.request.Request(
            f"{target_url}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     **({"Authorization": f"Bearer {target_key}"} if target_key else {})},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not self._response_text(result).strip():
                self.last_error = "模型接口返回空内容"
                return {"ok": False, "message": self.last_error, "latencyMs": self._latency(started)}
            self.last_error = ""
            return {"ok": True, "message": "模型接口连接正常", "latencyMs": self._latency(started)}
        except HTTPError as error:
            self.last_error = f"模型接口返回 HTTP {error.code}"
        except (URLError, TimeoutError, OSError):
            self.last_error = "模型接口连接失败"
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            self.last_error = "模型接口返回格式异常"
        return {"ok": False, "message": self.last_error, "latencyMs": self._latency(started)}

    @staticmethod
    def _latency(started: float) -> int:
        return round((time.monotonic() - started) * 1000)

    @staticmethod
    def _response_text(result: Any) -> str:
        """Read text from common Chat Completions and Responses-compatible shapes."""
        if not isinstance(result, dict):
            return ""
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            content = message.get("content", choice.get("text", ""))
            if isinstance(content, list):
                return "".join(
                    str(part.get("text", "")) for part in content
                    if isinstance(part, dict) and part.get("type", "text") in {"text", "output_text"}
                )
            return str(content or "")
        if result.get("output_text"):
            return str(result["output_text"])
        return ""

    @staticmethod
    def _parse_plan(content: str) -> dict[str, Any] | None:
        text = (content or "").strip()
        if not text:
            return None
        # Models occasionally ignore the no-fence instruction; remove only the
        # surrounding fence and then accept the first valid JSON object.
        if text.startswith("```"):
            text = text[3:]
            if text.startswith("json"):
                text = text[4:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        # A plain-language answer is still useful. It should not discard the
        # model entirely just because it did not follow the JSON instruction.
        return {"reply": text, "patch": {}, "tool": None}

    @classmethod
    def _normalize_plan_annotation(cls, value: Any) -> str | dict[str, str] | None:
        """Keep advisory skill questions/risks small and display-oriented.

        These fields are useful for dsh skill output, but they are not an
        authority channel.  Drop arbitrary nested model data before the plan
        reaches the Agent's persisted state.
        """
        if isinstance(value, str):
            text = value.strip()
            return text[:cls.MAX_PLAN_ANNOTATION_TEXT] if text else None
        if not isinstance(value, dict):
            return None
        result: dict[str, str] = {}
        for key in ("field", "question", "text", "message", "risk", "severity", "source", "code"):
            raw = value.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                continue
            if isinstance(raw, float) and not math.isfinite(raw):
                continue
            text = str(raw).strip()
            if text:
                result[key] = text[:cls.MAX_PLAN_ANNOTATION_TEXT]
        return result or None

    @classmethod
    def _normalize_plan_annotations(cls, raw: Any) -> list[str | dict[str, str]]:
        if not isinstance(raw, list):
            return []
        result: list[str | dict[str, str]] = []
        for item in raw[:cls.MAX_PLAN_ANNOTATIONS]:
            normalized = cls._normalize_plan_annotation(item)
            if normalized is not None:
                result.append(normalized)
        return result

    @classmethod
    def _normalize_plan_version(cls, raw: Any) -> str:
        if not isinstance(raw, str):
            return ""
        return raw.strip()[:cls.MAX_PLAN_META_VERSION]

    @classmethod
    def validate_plan(cls, value: Any, tool_names: set[str] | None = None) -> dict[str, Any] | None:
        """Normalize the model contract before it can mutate order state or call a tool."""
        if not isinstance(value, dict):
            return None
        reply = value.get("reply")
        reply = str(reply).strip() if reply is not None else ""
        if len(reply) > 4000:
            reply = reply[:4000].rstrip() + "..."
        raw_patch = value.get("patch")
        patch: dict[str, Any] = {}
        rejected_fields: list[str] = []
        product_hint = ""
        if isinstance(raw_patch, dict) and isinstance(raw_patch.get("productType"), str):
            product_hint = raw_patch.get("productType", "").strip()
        allowed_specs = known_product_spec_keys(product_hint or None)
        # Dimension aliases are accepted by the Agent's canonical dimensions
        # gateway even though they are not product-specific catalog questions.
        allowed_specs.update(DIMENSION_DEFAULTS)
        if isinstance(raw_patch, dict):
            for raw_key, item in raw_patch.items():
                # JSON object names are strings, but custom planner adapters
                # can call this validator with a Python mapping directly.
                # Reject non-string names before set membership or string
                # operations so malformed payloads fail closed instead of
                # escaping as a TypeError.
                if not isinstance(raw_key, str):
                    rejected_fields.append("<non-string-patch-key>")
                    continue
                key = raw_key
                if key not in cls.PATCH_FIELDS:
                    if isinstance(key, str) and key.strip():
                        rejected_fields.append(key.strip()[:256])
                    continue
                if item is None:
                    continue
                if key == "productSpecs":
                    if not isinstance(item, dict):
                        rejected_fields.append("productSpecs")
                        continue
                    specs: dict[str, str] = {}
                    for raw_name, raw_spec in item.items():
                        if not isinstance(raw_name, str):
                            rejected_fields.append("productSpecs.<non-string-key>")
                            continue
                        name = raw_name.strip()
                        if not name:
                            continue
                        field_name = f"productSpecs.{name}"[:256]
                        if name not in allowed_specs:
                            rejected_fields.append(field_name)
                            continue
                        if raw_spec is None:
                            continue
                        if not cls._is_patch_scalar(raw_spec):
                            rejected_fields.append(field_name)
                            continue
                        spec = str(raw_spec).strip()
                        if spec:
                            specs[name] = spec[:cls.MAX_EVIDENCE_QUOTE]
                    if specs:
                        patch[key] = specs
                elif key == "dimensions":
                    if not isinstance(item, dict):
                        rejected_fields.append("dimensions")
                        continue
                    dimensions: dict[str, str] = {}
                    for raw_name, raw_value in item.items():
                        if not isinstance(raw_name, str):
                            rejected_fields.append("dimensions.<non-string-key>")
                            continue
                        name = raw_name.strip()
                        field_name = f"dimensions.{name}"[:256]
                        if name not in DIMENSION_DEFAULTS:
                            rejected_fields.append(field_name)
                            continue
                        if raw_value is None:
                            continue
                        if not cls._is_patch_scalar(raw_value):
                            rejected_fields.append(field_name)
                            continue
                        normalized = str(raw_value).strip()
                        if normalized:
                            dimensions[name] = normalized[:cls.MAX_EVIDENCE_QUOTE]
                    if dimensions:
                        patch[key] = dimensions
                elif cls._is_patch_scalar(item):
                    try:
                        text = str(item).strip()
                    except (OverflowError, ValueError):
                        rejected_fields.append(str(key).strip()[:256])
                        continue
                    text = text[:cls.MAX_PATCH_VALUE]
                    if text:
                        patch[key] = text
                else:
                    rejected_fields.append(str(key).strip()[:256])
        raw_tool = value.get("tool")
        tool: dict[str, Any] | None = None
        if isinstance(raw_tool, dict):
            name = raw_tool.get("name")
            arguments = raw_tool.get("arguments", {})
            if isinstance(name, str) and name in (tool_names or set()) and isinstance(arguments, dict):
                tool = {"name": name, "arguments": arguments}
        confidence = cls._normalize_confidence(value.get("confidence"), patch)
        evidence = cls._normalize_evidence(value.get("evidence"), patch)
        supplied_rejected = value.get("rejectedFields")
        if isinstance(supplied_rejected, list):
            for item in supplied_rejected:
                if isinstance(item, str) and item.strip():
                    rejected_fields.append(item.strip()[:256])
        # Keep audit output deterministic and bounded.  A model can still
        # receive a useful reply when every proposed field was rejected.
        rejected_fields = list(dict.fromkeys(rejected_fields))[:cls.MAX_METADATA_FIELDS]
        metadata_present = any(key in value for key in
                              ("questions", "risks", "knowledgeVersion", "reportedKnowledgeVersion"))
        if not reply and not patch and tool is None and not rejected_fields and not metadata_present:
            return None
        result: dict[str, Any] = {"reply": reply, "patch": patch, "tool": tool}
        if confidence:
            result["confidence"] = confidence
        if evidence:
            result["evidence"] = evidence
        if rejected_fields:
            result["rejectedFields"] = rejected_fields
        # Preserve the common dsh skill envelope after bounding it.  Agent
        # treats these values as advisory ``planMeta`` only; they cannot write
        # order fields or authorize a tool call.
        if "questions" in value:
            result["questions"] = cls._normalize_plan_annotations(value.get("questions"))
        if "risks" in value:
            result["risks"] = cls._normalize_plan_annotations(value.get("risks"))
        if "knowledgeVersion" in value:
            result["knowledgeVersion"] = cls._normalize_plan_version(value.get("knowledgeVersion"))
        if "reportedKnowledgeVersion" in value:
            result["reportedKnowledgeVersion"] = cls._normalize_plan_version(value.get("reportedKnowledgeVersion"))
        native_call = value.get("_nativeToolCall")
        if isinstance(native_call, dict):
            function = native_call.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str) \
                    and isinstance(function.get("arguments"), dict):
                result["_nativeToolCall"] = {
                    "id": str(native_call.get("id") or "")[:256],
                    "type": "function",
                    "function": {
                        "name": function["name"][:128],
                        "arguments": deepcopy(function["arguments"]),
                    },
                }
        return result

    @classmethod
    def _normalize_confidence(cls, raw: Any, patch: dict[str, Any]) -> dict[str, float]:
        """Keep only finite confidence grades for fields in the accepted patch."""
        if not isinstance(raw, dict):
            return {}
        accepted = cls._patch_field_paths(patch)
        normalized: dict[str, float] = {}
        for raw_key, raw_value in raw.items():
            if not isinstance(raw_key, str):
                continue
            key = raw_key.strip()
            if not key or not cls._metadata_path_allowed(key, accepted):
                continue
            if isinstance(raw_value, bool):
                continue
            try:
                number = float(raw_value)
            except (OverflowError, TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            normalized[key[:256]] = round(max(0.0, min(1.0, number)), 3)
            if len(normalized) >= cls.MAX_METADATA_FIELDS:
                break
        return normalized

    @classmethod
    def _normalize_evidence(cls, raw: Any, patch: dict[str, Any]) -> dict[str, dict[str, str]]:
        """Normalize dsh evidence arrays or field-to-quote maps.

        The public dsh contract uses an array of ``{field, quote, source}``
        records, while adapters often find a compact ``{field: quote}`` map
        more convenient.  Both are converted to a bounded field map here.
        """
        if not isinstance(raw, (dict, list)):
            return {}
        accepted = cls._patch_field_paths(patch)
        candidates: list[tuple[Any, Any, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                candidates.append((item.get("field"), item.get("quote", item.get("evidence")), item.get("source")))
        else:
            for field, item in raw.items():
                if isinstance(item, dict):
                    candidates.append((field, item.get("quote", item.get("evidence")), item.get("source")))
                else:
                    candidates.append((field, item, None))
        normalized: dict[str, dict[str, str]] = {}
        for raw_field, raw_quote, raw_source in candidates:
            if not isinstance(raw_field, str):
                continue
            field = raw_field.strip()
            # Evidence is useful only for values this plan is allowed to
            # apply; this also prevents arbitrary metadata keys from leaking
            # into the Agent state.
            if not field or not cls._metadata_path_allowed(field, accepted):
                continue
            if not isinstance(raw_quote, str):
                continue
            quote = raw_quote.strip()
            if not quote:
                continue
            entry = {"quote": quote[:cls.MAX_EVIDENCE_QUOTE]}
            if isinstance(raw_source, str) and raw_source.strip():
                entry["source"] = raw_source.strip()[:cls.MAX_EVIDENCE_SOURCE]
            normalized[field[:256]] = entry
            if len(normalized) >= cls.MAX_METADATA_FIELDS:
                break
        return normalized

    @staticmethod
    def _patch_field_paths(patch: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        for key in patch:
            if key == "productSpecs" and isinstance(patch.get(key), dict):
                paths.add("productSpecs")
                paths.update(f"productSpecs.{name}" for name in patch[key])
            elif key == "dimensions" and isinstance(patch.get(key), dict):
                paths.add("dimensions")
                paths.update(f"dimensions.{name}" for name in patch[key])
            else:
                paths.add(str(key))
        return paths

    @staticmethod
    def _metadata_path_allowed(field: str, accepted: set[str]) -> bool:
        """Allow an optional stable multi-product item prefix."""
        if field in accepted or field in {"productSpecs", "dimensions"}:
            return True
        parts = field.split(".")
        if len(parts) < 3 or parts[0] != "items":
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[1]):
            return False
        remainder = ".".join(parts[2:])
        return remainder in accepted or remainder in {"productSpecs", "dimensions"}
