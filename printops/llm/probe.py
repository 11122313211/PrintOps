"""Connectivity pre-probe and concurrent capability probing for LLM endpoints."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as futures_wait
import json
import math
import socket
import ssl
import time
from typing import Any
import urllib.error
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
import urllib.request

from printops.runtime.deadline import (
    DeadlineExceeded,
    bounded_io,
    check_deadline,
    current_deadline,
    deadline_scope,
    operation_deadline,
    remaining,
)
from printops.llm.transport import _close_error_response, _system_proxy_hint, normalize_base_url


class ProbeMixin:
    """Endpoint reachability and native/JSON capability probing."""

    @staticmethod
    def _classify_probe_error(error: Exception) -> tuple[str, str]:
        """Map a probe failure to (user-facing cause, kind).

        dsh's timeout library keeps capability-owned reason codes instead
        of one generic error; kind drives fail-fast: network/auth stop
        the probe (the endpoint or key will not heal mid-probe), protocol
        errors continue (payload-specific 4xx/5xx are still informative).
        """
        reason = getattr(error, "reason", None)
        if isinstance(error, HTTPError):
            if error.code in (401, 403):
                return f"HTTP {error.code}：API Key 被拒绝或无权访问该模型", "auth"
            if error.code == 404:
                return "HTTP 404：接口路径或模型名不存在（URL 通常应以 /v1 结尾）", "protocol"
            if error.code == 429:
                return "HTTP 429：供应商限流，请稍后重试", "protocol"
            return f"HTTP {error.code}", "protocol"
        if isinstance(error, DeadlineExceeded):
            # The probe's own bounded wait expired (urlopen reads can outlast
            # the caller budget); from the user's side this is indistinguish-
            # able from a socket read timeout, so report the same actionable
            # cause instead of an internal deadline name.
            return "连接或响应超时，端点未在时限内返回数据", "network"
        if isinstance(error, (TimeoutError, socket.timeout)) \
                or isinstance(reason, (TimeoutError, socket.timeout)):
            return "连接或响应超时，端点未在时限内返回数据" + _system_proxy_hint(), "network"
        if isinstance(error, socket.gaierror) or isinstance(reason, socket.gaierror):
            return "无法解析主机名，请检查域名拼写或本机 DNS/代理", "network"
        if isinstance(error, ConnectionRefusedError) or isinstance(reason, ConnectionRefusedError):
            return "连接被拒绝，目标端口未监听或地址错误" + _system_proxy_hint(), "network"
        if isinstance(error, ssl.SSLError) or isinstance(reason, ssl.SSLError):
            return "TLS 握手失败，请检查 HTTPS 证书或代理设置" + _system_proxy_hint(), "network"
        if isinstance(error, URLError):
            return "网络错误：" + str(reason or error) + _system_proxy_hint(), "network"
        return "模型接口连接失败", "network"

    def _probe_reachability(self, target_url: str, deadline: float) -> tuple[bool, str, str, int]:
        """Ask the endpoint once, without a model request, whether it answers.

        Only a network-level failure decides anything: a 401/404 on the base
        path says nothing about the key or the completions route, which the
        capability hops judge.  Any unexpected shape fails open, because a
        diagnostic must never veto the probe it is trying to accelerate.
        """
        began = time.monotonic()
        budget = min(self.CAPABILITY_PROBE_CONNECT_SECONDS,
                     max(0.5, deadline - time.monotonic()))
        request = urllib.request.Request(target_url, method="GET",
                                         headers={"Accept": "*/*"})

        def fetch() -> Any:
            with urllib.request.urlopen(request, timeout=remaining(
                    operation_deadline(budget, deadline))) as response:
                return getattr(response, "status", 200)

        try:
            bounded_io(fetch, operation_deadline(budget, deadline))
            return True, "", "", self._latency(began)
        except HTTPError as error:
            _close_error_response(error)
            return True, "", "", self._latency(began)
        except (URLError, TimeoutError, OSError) as error:
            message, kind = self._classify_probe_error(error)
            if kind == "network" and "超时" in message:
                message = f"{message}（{budget:.1f} 秒内未能建立连接）"
            return False, message, kind, self._latency(began)
        except Exception:
            return True, "", "", self._latency(began)

    def _capability_post(self, target_url: str, target_model: str, target_key: str,
                         payload: dict[str, Any], deadline: float) -> tuple[dict[str, Any] | None, str, str]:
        """Send one bounded, non-retrying capability request."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "能力探测已超时", "network"
        try:
            configured_timeout = float(self.timeout)
        except (TypeError, ValueError, OverflowError):
            configured_timeout = self.CAPABILITY_PROBE_REQUEST_SECONDS
        if not math.isfinite(configured_timeout) or configured_timeout <= 0:
            configured_timeout = self.CAPABILITY_PROBE_REQUEST_SECONDS
        probe_budget = max(self.CAPABILITY_PROBE_REQUEST_SECONDS,
                           min(configured_timeout, self.CAPABILITY_PROBE_REQUEST_MAX_SECONDS))
        request_timeout = min(remaining, probe_budget)
        request = urllib.request.Request(
            f"{target_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     **({"Authorization": f"Bearer {target_key}"} if target_key else {})},
        )
        try:
            request_deadline = operation_deadline(request_timeout, deadline)
            decoded = self._request_json(request, request_deadline, self.MAX_CAPABILITY_RESPONSE_BYTES)
            if not isinstance(decoded, dict):
                return None, "模型接口响应不是 JSON 对象", "protocol"
            return decoded, "", ""
        except HTTPError as error:
            message, kind = self._classify_probe_error(error)
            _close_error_response(error)
            return None, message, kind
        except (URLError, TimeoutError, OSError) as error:
            message, kind = self._classify_probe_error(error)
            return None, message, kind
        except (UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            return None, "模型接口返回格式异常", "protocol"

    def _probe_root_cause(self, result: dict[str, Any], started: float,
                          error: str, kind: str, skipped: str) -> dict[str, Any]:
        """Stop the probe and report the one cause the user can act on."""
        result["errors"]["chat"] = error
        prefix = "鉴权失败：" if kind == "auth" else "模型接口不可达："
        result["message"] = f"{prefix}{error}，已跳过{skipped}"
        result["chosenProtocol"] = "unavailable" if kind == "auth" else "unreachable"
        result["latencyMs"] = self._latency(started)
        self.last_error, self.last_protocol = result["message"], ""
        return result

    def test_capabilities(self, base_url: str | None = None, model: str | None = None,
                          api_key: str | None = None) -> dict[str, Any]:
        """Probe whether an endpoint can drive the Agent without order data.

        The probe never executes a real tool.  It advertises one synthetic,
        side-effect-free function and checks whether the model returns a
        matching call.  Tool-result continuation is left to the Agent loop,
        which already retries with the JSON protocol when a provider rejects
        a ``tool`` turn; probing it here cost a second generation round trip
        (2-9s on reasoning models) and misjudged providers by the model's
        wording rather than by the protocol it accepted.
        """
        started = time.monotonic()
        result: dict[str, Any] = {
            "ok": False,
            "chat": False,
            "jsonPlan": False,
            "nativeTools": False,
            "chosenProtocol": "unavailable",
            "agentReady": False,
            "message": "模型接口尚未配置",
            "latencyMs": None,
            "errors": {},
        }
        try:
            target_url = self.base_url if base_url is None else normalize_base_url(
                base_url, allow_private_hosts=self.allow_private_hosts)
        except ValueError:
            result["message"] = "接口 URL 格式不正确"
            result["latencyMs"] = self._latency(started)
            return result
        target_model = self.model if model is None else (model or "").strip()
        target_key = self.api_key if api_key is None else (api_key or "").strip()
        if not target_url or not target_model:
            result["latencyMs"] = self._latency(started)
            return result

        deadline = started + self.CAPABILITY_PROBE_TOTAL_SECONDS
        errors: dict[str, str] = result["errors"]
        timings: dict[str, int] = {}
        result["probeTimings"] = timings

        chat_marker = "PRINTOPS_CAPABILITY_CHAT_OK"
        chat_payload = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": "这是只读协议能力检查，不处理订单或业务数据。"},
                {"role": "user", "content": f"请回复 {chat_marker}。"},
            ],
            "max_tokens": 32,
        }
        json_marker = "PRINTOPS_CAPABILITY_JSON_OK"
        json_payload = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": (
                    "这是只读协议能力检查，不处理订单或业务数据。"
                    "只输出请求的 JSON 对象，不要 Markdown 或额外文字。")},
                {"role": "user", "content": (
                    '{"reply":"' + json_marker + '","patch":{},"tool":null}')},
            ],
            "max_tokens": 96,
        }
        tool_schema = {
            "type": "function",
            "function": {
                "name": self.CAPABILITY_TOOL_NAME,
                "description": "只读协议探测函数；无外部副作用，不访问任何订单。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "token": {"type": "string", "enum": [self.CAPABILITY_TOKEN]},
                    },
                    "required": ["token"],
                    "additionalProperties": False,
                },
            },
        }
        native_messages = [
            {"role": "system", "content": (
                "这是只读协议能力检查。必须调用给定的无副作用工具一次，"
                "并原样传入指定 token；不要处理订单或业务数据。")},
            {"role": "user", "content": f"调用 {self.CAPABILITY_TOOL_NAME}，token={self.CAPABILITY_TOKEN}。"},
        ]
        native_payload = {
            "model": target_model,
            "messages": native_messages,
            "tools": [tool_schema],
            "tool_choice": "auto",
            "max_tokens": 96,
        }

        def hop(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str, str, int]:
            began = time.monotonic()
            response, message, kind = self._capability_post(
                target_url, target_model, target_key, payload, deadline)
            return response, message, kind, self._latency(began)

        # Three serial generation round trips measured 8.7s on a live gateway.
        # Chat, JSON and native tools are independent once the endpoint is known
        # to be alive, so they overlap in one round trip.  A header-only GET
        # runs beside them to settle liveness first: on a live endpoint it
        # answers in a few hundred milliseconds, while a refused or firewalled
        # endpoint yields one named root cause instead of three identical
        # connect failures.
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="printops-capability")
        try:
            gate_began = time.monotonic()
            chat_future = pool.submit(hop, chat_payload)
            reach_future = pool.submit(self._probe_reachability, target_url, deadline)
            futures_wait((chat_future, reach_future), self.CAPABILITY_PROBE_GATE_SECONDS,
                         return_when=FIRST_COMPLETED)
            timings["gateMs"] = self._latency(gate_began)
            if reach_future.done():
                reachable, reach_error, reach_kind, timings["reachMs"] = reach_future.result()
                if not reachable:
                    timings["hops"] = 1
                    return self._probe_root_cause(result, started, reach_error, reach_kind,
                                                  "后续检查")
            chat_resolved = chat_future.done()
            if chat_resolved:
                chat_response, chat_error, chat_kind, timings["chatMs"] = chat_future.result()
                if chat_response is None and chat_kind in {"network", "auth"}:
                    timings["hops"] = 1
                    return self._probe_root_cause(result, started, chat_error, chat_kind,
                                                  "后续检查")
            json_future = pool.submit(hop, json_payload)
            native_future = pool.submit(hop, native_payload)
            if not chat_resolved:
                chat_response, chat_error, chat_kind, timings["chatMs"] = chat_future.result()
            json_response, json_error, _json_kind, timings["jsonMs"] = json_future.result()
            native_response, native_error, _native_kind, timings["nativeMs"] = native_future.result()
        finally:
            # Abandon a hop that outlived the verdict instead of holding the
            # handler open: request_runtime discards late results, and a hung
            # connection does not heal while the user waits for an answer.
            pool.shutdown(wait=False)
        timings["hops"] = 3
        if chat_response is None and chat_kind in {"network", "auth"}:
            # Nothing proved the endpoint answers a completion request, so the
            # remaining verdict would only repeat the same failure.
            return self._probe_root_cause(result, started, chat_error, chat_kind,
                                          "后续检查")

        if chat_response is not None and self._response_text(chat_response).strip():
            result["chat"] = True
        else:
            errors["chat"] = chat_error or "模型接口返回空内容"

        if json_response is not None:
            value = self._strict_json_object(self._response_text(json_response))
            normalized = self.validate_plan(value, set())
            result["jsonPlan"] = bool(
                normalized and normalized.get("reply") == json_marker
                and normalized.get("patch") == {} and normalized.get("tool") is None)
        if not result["jsonPlan"]:
            errors["jsonPlan"] = json_error or "未返回指定 JSON 规划对象"

        matching_call: dict[str, Any] | None = None
        if native_response is not None:
            native_calls, _native_protocol = self._native_calls(native_response)
            matching_call = next((call for call in native_calls
                                  if call.get("function", {}).get("name") == self.CAPABILITY_TOOL_NAME
                                  and call.get("function", {}).get("arguments", {}).get("token")
                                  == self.CAPABILITY_TOKEN), None)
        result["nativeTools"] = matching_call is not None
        if matching_call is None:
            errors["nativeTools"] = native_error or "未返回带正确参数的 native tool call"

        if result["nativeTools"]:
            chosen = "native_tools"
            message = "接口支持原生工具调用，可用于 Agent。"
        elif result["jsonPlan"]:
            chosen = "json_fallback"
            message = "接口支持 JSON 规划，可使用兼容模式运行 Agent。"
        elif result["chat"]:
            chosen = "chat_only"
            message = "接口仅通过基础聊天检查，暂不满足 Agent 规划协议。"
        else:
            chosen = "unavailable"
            message = "模型接口不可用或返回格式不兼容。"
        result["chosenProtocol"] = chosen
        result["agentReady"] = chosen in {"native_tools", "json_fallback"}
        result["ok"] = bool(result["chat"] or result["jsonPlan"] or result["nativeTools"])
        result["message"] = message
        result["latencyMs"] = self._latency(started)
        self.last_protocol = chosen
        self.last_error = "" if result["agentReady"] else message
        return result

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
            request_deadline = operation_deadline(self.timeout)
            result = self._request_json(request, request_deadline, self.MAX_PLAN_RESPONSE_BYTES)
            if not self._response_text(result).strip():
                self.last_error = "模型接口返回空内容"
                return {"ok": False, "message": self.last_error, "latencyMs": self._latency(started)}
            self.last_error = ""
            return {"ok": True, "message": "模型接口连接正常", "latencyMs": self._latency(started)}
        except HTTPError as error:
            self.last_error = f"模型接口返回 HTTP {error.code}"
            _close_error_response(error)
        except (URLError, TimeoutError, OSError):
            self.last_error = "模型接口连接失败"
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            self.last_error = "模型接口返回格式异常"
        return {"ok": False, "message": self.last_error, "latencyMs": self._latency(started)}
