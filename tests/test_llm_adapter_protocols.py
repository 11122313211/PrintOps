"""Offline protocol contract tests for the OpenAI-compatible planner.

These tests use no network, credentials, or package dependencies.  They keep
provider-shape compatibility executable independently from the Agent loop.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from llm_adapter import OpenAICompatiblePlanner


TOOLS = [
    {"name": "validate_order", "description": "校验", "input": {"type": "object", "properties": {}}},
    {"name": "estimate_price", "description": "估算", "input": {"type": "object", "properties": {}}},
]


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


def _planner() -> OpenAICompatiblePlanner:
    return OpenAICompatiblePlanner("https://example.test/v1", "", "test-model")


def _capability_hop(body: dict) -> str:
    """Name a probe hop from its own request.

    The independent probes now overlap, so the order the fake transport sees
    them in is the scheduler's choice; dispatching on the request body keeps
    these assertions about the probe *set* rather than its sequence.
    """
    messages = body.get("messages") or []
    if body.get("tools") or body.get("functions"):
        roles = {item.get("role") for item in messages if isinstance(item, dict)}
        return "continuation" if roles & {"tool", "function"} else "native"
    joined = json.dumps(messages, ensure_ascii=False)
    return "json" if "PRINTOPS_CAPABILITY_JSON_OK" in joined else "chat"


_JSON_PLAN = {"choices": [{"message": {"content":
    '{"reply":"PRINTOPS_CAPABILITY_JSON_OK","patch":{},"tool":null}'}}]}
_NATIVE_CALL = {"choices": [{"message": {"content": None, "tool_calls": [{
    "id": "probe-call", "type": "function",
    "function": {"name": "printops_capability_probe",
                 "arguments": '{"token":"printops-probe-v1"}'},
}]}}]}
_CHAT_OK = {"choices": [{"message": {"content": "OK"}}]}


class LlmAdapterProtocolTest(unittest.TestCase):
    def test_native_batch_and_content_patch_are_preserved(self) -> None:
        response = {
            "choices": [{"message": {
                "content": '{"reply":"已识别","patch":{"quantity":"12张"},"confidence":{"quantity":0.91}}',
                "tool_calls": [
                    {"id": "call-v", "type": "function",
                     "function": {"name": "validate_order", "arguments": "{}"}},
                    {"id": "call-p", "type": "function",
                     "function": {"name": "estimate_price", "arguments": {}}},
                ],
            }}],
        }
        with patch("llm_adapter.urllib.request.urlopen", return_value=_Response(response)):
            plan = _planner().plan("12张", {}, TOOLS)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["patch"], {"quantity": "12张"})
        self.assertEqual(plan["tool"]["name"], "validate_order")
        self.assertEqual([item["id"] for item in plan["_nativeToolCalls"]], ["call-v", "call-p"])
        self.assertEqual(plan["_nativeToolCall"]["id"], "call-v")

    def test_legacy_function_call_and_responses_output_are_normalized(self) -> None:
        legacy = {"choices": [{"message": {
            "content": None,
            "function_call": {"name": "validate_order", "arguments": "{}"},
        }}]}
        responses = {"output": [
            {"type": "function_call", "call_id": "resp-1", "name": "estimate_price", "arguments": "{}"},
            {"type": "message", "content": [{"type": "output_text", "text": "说明"}]},
        ]}
        legacy_plan = OpenAICompatiblePlanner._native_tool_plan(legacy)
        response_plan = OpenAICompatiblePlanner._native_tool_plan(responses)
        self.assertEqual(legacy_plan["tool"]["name"], "validate_order")
        self.assertEqual(response_plan["tool"]["name"], "estimate_price")
        self.assertEqual(response_plan["_nativeToolCall"]["id"], "resp-1")
        self.assertEqual(response_plan["reply"], "说明")

    def test_malformed_native_arguments_try_json_fallback(self) -> None:
        malformed = {"choices": [{"message": {"tool_calls": [{
            "id": "bad", "type": "function",
            "function": {"name": "validate_order", "arguments": "{not-json"},
        }]}}]}
        fallback = {"choices": [{"message": {
            "content": '{"reply":"降级成功","patch":{},"tool":null}',
        }}]}
        requests: list[dict] = []

        def fake_urlopen(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            return _Response(malformed if len(requests) == 1 else fallback)

        planner = _planner()
        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            plan = planner.plan("继续", {}, TOOLS)
        self.assertEqual(plan["reply"], "降级成功")
        self.assertEqual(planner.last_protocol, "json_fallback")
        self.assertEqual(len(requests), 2)
        self.assertIn("tools", json.loads(requests[1]["messages"][-1]["content"]))

    def test_native_http_rejection_rebuilds_one_user_message(self) -> None:
        native = {"choices": [{"message": {"tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "validate_order", "arguments": "{}"},
        }]}}]}
        fallback = {"choices": [{"message": {"content": '{"reply":"完成","patch":{},"tool":null}'}}]}
        requests: list[dict] = []

        def fake_urlopen(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            if len(requests) == 1:
                return _Response(native)
            if len(requests) == 2:
                raise HTTPError(request.full_url, 400, "tools unsupported", {}, None)
            return _Response(fallback)

        planner = _planner()
        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            first = planner.plan("继续", {}, TOOLS)
            second = planner.plan(
                "继续", {}, TOOLS, tool_result={"name": "validate_order", "result": {"ok": True}},
                tool_call=first["_nativeToolCall"], tool_ledger=[
                    {"name": "validate_order", "id": "call-1", "result": {"ok": True}},
                ],
            )
        self.assertEqual(second["reply"], "完成")
        fallback_request = requests[-1]
        self.assertEqual(sum(item.get("role") == "user" for item in fallback_request["messages"]), 1)
        self.assertFalse(any(item.get("role") in {"assistant", "tool"} for item in fallback_request["messages"]))

    def test_synthesis_can_disable_tools_and_context_has_total_budget(self) -> None:
        response = {"choices": [{"message": {"content": '{"reply":"总结","patch":{},"tool":null}'}}]}
        requests: list[dict] = []

        def fake_urlopen(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            return _Response(response)

        planner = _planner()
        ledger = [{"name": "validate_order", "id": str(i), "round": i,
                   "arguments": {}, "result": {"detail": "大量" * 5000}} for i in range(8)]
        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            plan = planner.plan("总结", {"productType": "名片", "note": "大" * 10000}, TOOLS,
                                history=[{"role": "user", "text": "历史" * 3000}] * 8,
                                tool_ledger=ledger, allow_tools=False, synthesis=True,
                                context_budget=5000)
        self.assertEqual(plan["reply"], "总结")
        body = requests[0]
        self.assertLessEqual(len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")), 5000)
        self.assertNotIn("tools", body)
        user = next(item for item in body["messages"] if item.get("role") == "user")
        self.assertTrue(json.loads(user["content"]).get("_contextTruncated"))

    def test_retry_backoff_is_class_based(self) -> None:
        # TD-12: 429 backs off exponentially, other retryable HTTP codes keep
        # short linear spacing, connection failures back off exponentially.
        backoff = OpenAICompatiblePlanner._retry_backoff_seconds
        rate_limited = HTTPError("https://example.test/v1", 429, "rate limited", {}, None)
        server_error = HTTPError("https://example.test/v1", 503, "unavailable", {}, None)
        network_error = TimeoutError("connect timed out")
        self.assertEqual(backoff(rate_limited, 0), 0.5)
        self.assertEqual(backoff(rate_limited, 1), 1.0)
        self.assertEqual(backoff(server_error, 0), 0.15)
        self.assertEqual(backoff(server_error, 1), 0.3)
        self.assertEqual(backoff(network_error, 0), 0.3)
        self.assertEqual(backoff(network_error, 1), 0.6)

    def test_rate_limited_retry_sleeps_through_deadline_budget(self) -> None:
        delays: list[float] = []

        def fake_urlopen(request, timeout):
            raise HTTPError(request.full_url, 429, "rate limited", {}, None)

        planner = _planner()
        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen), \
                patch("llm_adapter.deadline_sleep", side_effect=lambda seconds, deadline: delays.append(seconds)):
            plan = planner.plan("继续", {}, TOOLS)
        self.assertIsNone(plan)
        # Both payload variants get one rate-limit backoff at attempt 0; the
        # deadline-aware sleep receives the value instead of a bare time.sleep.
        self.assertEqual(delays, [0.5, 0.5])
        self.assertIn("429", planner.last_error)


class LlmAdapterCapabilityTest(unittest.TestCase):
    def _assert_no_order_payload(self, requests: list[dict]) -> None:
        for request in requests:
            for message in request.get("messages", []):
                content = message.get("content")
                if not isinstance(content, str) or not content.startswith("{"):
                    continue
                try:
                    value = json.loads(content)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    self_keys = set(value)
                    if "order" in self_keys:
                        raise AssertionError("capability probe sent an order payload")

    def test_capabilities_reports_chat_only(self) -> None:
        requests: list[dict] = []

        def fake_urlopen(request, timeout):
            if getattr(request, "method", "POST") == "GET":
                return _Response({"data": []})  # reachability pre-flight, no model call
            body = json.loads(request.data.decode("utf-8"))
            requests.append(body)
            return _Response(_CHAT_OK)

        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            result = _planner().test_capabilities()
        self.assertTrue(result["chat"])
        self.assertFalse(result["jsonPlan"])
        self.assertFalse(result["nativeTools"])
        self.assertEqual(result["chosenProtocol"], "chat_only")
        self.assertFalse(result["agentReady"])
        self.assertEqual(sorted(_capability_hop(body) for body in requests),
                         ["chat", "json", "native"])
        self.assertEqual(result["probeTimings"]["hops"], 3)
        self._assert_no_order_payload(requests)

    def test_capabilities_selects_json_fallback(self) -> None:
        requests: list[dict] = []

        def fake_urlopen(request, timeout):
            if getattr(request, "method", "POST") == "GET":
                return _Response({"data": []})  # reachability pre-flight, no model call
            body = json.loads(request.data.decode("utf-8"))
            requests.append(body)
            hop = _capability_hop(body)
            if hop == "json":
                return _Response(_JSON_PLAN)
            if hop == "native":
                raise HTTPError(request.full_url, 400, "tools unsupported", {}, None)
            return _Response(_CHAT_OK)

        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            result = _planner().test_capabilities()
        self.assertTrue(result["chat"])
        self.assertTrue(result["jsonPlan"])
        self.assertFalse(result["nativeTools"])
        self.assertEqual(result["chosenProtocol"], "json_fallback")
        self.assertTrue(result["agentReady"])
        self.assertEqual(result["errors"]["nativeTools"], "HTTP 400")
        self._assert_no_order_payload(requests)

    def test_capabilities_selects_native_tools_from_the_tool_call_alone(self) -> None:
        requests: list[dict] = []

        def fake_urlopen(request, timeout):
            if getattr(request, "method", "POST") == "GET":
                return _Response({"data": []})  # reachability pre-flight, no model call
            body = json.loads(request.data.decode("utf-8"))
            requests.append(body)
            hop = _capability_hop(body)
            if hop == "json":
                return _Response(_JSON_PLAN)
            if hop == "native":
                return _Response(_NATIVE_CALL)
            return _Response(_CHAT_OK)

        planner = _planner()
        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            result = planner.test_capabilities()
        self.assertTrue(result["chat"])
        self.assertTrue(result["jsonPlan"])
        self.assertTrue(result["nativeTools"])
        self.assertEqual(result["chosenProtocol"], "native_tools")
        self.assertTrue(result["agentReady"])
        self.assertEqual(planner.last_protocol, "native_tools")
        self.assertEqual(result["probeTimings"]["hops"], 3)
        # The probe no longer spends a generation round trip replaying a tool
        # result: that contract is exercised by the Agent, which falls back to
        # JSON on its own.
        self.assertEqual(sorted(_capability_hop(body) for body in requests),
                         ["chat", "json", "native"])
        self._assert_no_order_payload(requests)


class PlannerPromptContractTest(unittest.TestCase):
    """V5-01 TD-08 提示词契约绊线。

    system prompt 是 llm_adapter.plan 内联字符串，没有独立版本字段；改一行即
    静默漂移。本绊线用 FakeResponse 捕获真实请求 payload，断言提示词包含四类
    契约要素：JSON 输出形态、PATCH_FIELDS 白名单字段名、工具使用规则、置信度
    语义。改提示词丢掉任一要素 → 测试红，"换提示词的影响"从静默变可见。
    提示词实质变更时必须 bump ``PLANNER_PROMPT_VERSION`` 并重跑
    tests/test_dual_track_equivalence.py（差分用例）。
    """

    def _captured_system_prompt(self) -> str:
        requests: list[dict] = []
        response = {"choices": [{"message": {
            "content": '{"reply":"绊线","patch":{},"tool":null}'}}]}

        def fake_urlopen(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            return _Response(response)

        planner = _planner()
        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            planner.plan("做500张名片", {"productType": "名片"}, TOOLS)
        self.assertTrue(requests, "绊线必须捕获到至少一次真实协议请求")
        system_messages = [item for item in requests[0]["messages"]
                           if isinstance(item, dict) and item.get("role") == "system"]
        self.assertEqual(len(system_messages), 1, "请求必须恰好携带一条 system prompt")
        return system_messages[0]["content"]

    def test_prompt_version_constant_is_pinned(self) -> None:
        """变更锚：提示词实质变更必须 bump PLANNER_PROMPT_VERSION。"""
        import llm_adapter
        self.assertEqual(llm_adapter.PLANNER_PROMPT_VERSION, "1")

    def test_prompt_declares_json_output_shape(self) -> None:
        """契约要素 1：JSON 输出形态（reply/patch/tool 信封与 null 哨兵）。"""
        prompt = self._captured_system_prompt()
        for marker in ("reply", "patch:object", "tool:{name:string,arguments:object}|null",
                       "knowledgeVersion", "JSON"):
            self.assertIn(marker, prompt, f"system prompt 丢失 JSON 形态要素：{marker}")

    def test_prompt_enumerates_every_patch_whitelist_field(self) -> None:
        """契约要素 2：patch 白名单——提示词必须逐一列出 PATCH_FIELDS 的全部
        字段名（从类常量推导，白名单扩字段而提示词漏写 → 红）。"""
        prompt = self._captured_system_prompt()
        for field in sorted(OpenAICompatiblePlanner.PATCH_FIELDS):
            self.assertIn(field, prompt,
                          f"system prompt 未列出 patch 白名单字段：{field}")

    def test_prompt_states_tool_usage_rules(self) -> None:
        """契约要素 3：工具使用规则（本地权威工具名 + 防重复/防编造约束）。"""
        prompt = self._captured_system_prompt()
        for marker in ("validate_order", "recommend_processes", "estimate_price",
                       "explain_print_term", "工具使用规则", "不要重复同一工具",
                       "不要把完整 order 作为参数", "不要编造价格"):
            self.assertIn(marker, prompt, f"system prompt 丢失工具规则要素：{marker}")

    def test_prompt_defines_confidence_semantics(self) -> None:
        """契约要素 4：置信度语义（字段路径到 0~1 数字的映射约定）。"""
        prompt = self._captured_system_prompt()
        self.assertIn("confidence", prompt)
        self.assertIn("0~1", prompt, "system prompt 丢失置信度 0~1 语义")
        self.assertIn("字段路径", prompt)


class DeepResponseClassificationTest(unittest.TestCase):
    """TD-28（安全体检 L10b）：模型返回深嵌套 JSON（字节量在响应上限内）时
    json.loads 抛 RecursionError（RuntimeError 而非 ValueError）——捕获元组
    补齐后按"返回格式异常"分类，plan 硬失败降级而非异常外溢/500。"""

    def test_deeply_nested_model_response_is_a_format_error(self) -> None:
        # 实际触发 RecursionError 的深度取决于平台的 C 递归上限（3.13+ 按线程
        # 栈大小推导，Linux 8MB 栈上 90k 层可被正常解析、走"内容无法识别"
        # 分支），因此改用标记响应体让 json.loads 只在该调用点抛出受控
        # RecursionError——钉住分类契约本身，不赌平台深度阈值。
        real_loads = json.loads

        def _deep_only_loads(text, *args, **kwargs):
            if text == "PRINTOPS_DEEP_NESTED_BODY":
                raise RecursionError("simulated deep nesting")
            return real_loads(text, *args, **kwargs)

        class _DeepResponse(_Response):
            def __init__(self) -> None:
                self._body = b"PRINTOPS_DEEP_NESTED_BODY"

        planner = _planner()
        with patch("llm_adapter.urllib.request.urlopen", return_value=_DeepResponse()), \
                patch("json.loads", side_effect=_deep_only_loads):
            plan = planner.plan("做 500 张名片", {}, TOOLS, [])
        self.assertIsNone(plan)
        # 修复前 RecursionError（RuntimeError，非 ValueError 族）逃出捕获
        # 元组直达调用方/500 兜底；现在按"返回格式异常"硬失败降级。
        self.assertEqual(planner.last_error, "模型接口返回格式异常")
        self.assertEqual(planner.last_protocol, "error")


class RedirectRefusalTest(unittest.TestCase):
    """TD-27（安全体检 L1）：网关返回 3xx 时绝不跟随——Authorization 不得
    外发到 Location 指向的主机，私网校验也不得被重定向绕过。真实 socket
    端到端：网关 302 → 第二台服务器必须收不到任何请求。"""

    def test_plan_follows_no_redirect_and_never_reaches_location_target(self) -> None:
        import http.server
        import threading

        hits: list[tuple[str, str]] = []

        class _Redirector(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.target_port}/chat/completions")
                self.end_headers()

            def log_message(self, *_args) -> None:
                pass

        class _Target(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                hits.append((self.command, self.headers.get("Authorization", "")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                body = json.dumps({"choices": [{"message": {"content": "leaked"}}]}).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass

        def _serve(handler):
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.listener_port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return server

        redirector = _serve(_Redirector)
        target = _serve(_Target)
        redirector.target_port = target.listener_port
        try:
            planner = OpenAICompatiblePlanner(
                f"http://127.0.0.1:{redirector.listener_port}", "test-key", "test-model",
                allow_private_hosts=True)
            plan = planner.plan("做 500 张名片", {}, TOOLS, [])
        finally:
            redirector.shutdown()
            redirector.server_close()
            target.shutdown()
            target.server_close()

        self.assertIsNone(plan, "重定向响应必须按硬失败处理而非跟随")
        self.assertEqual(hits, [], "Authorization 头绝不能随 302 到达 Location 主机")
        self.assertIn("302", planner.last_error)
        self.assertNotIn("test-key", planner.last_error or "")

    def test_installed_global_opener_refuses_redirects(self) -> None:
        """import llm_adapter 即安装禁重定向 opener（进程级 TD-27 保证）。"""
        import urllib.request

        import llm_adapter

        installed = getattr(urllib.request, "_opener", None)
        self.assertIsNotNone(installed, "llm_adapter 导入后必须已安装全局 opener")
        self.assertTrue(
            any(isinstance(handler, llm_adapter._NoRedirect) for handler in installed.handlers),
            "全局 opener 必须包含 _NoRedirect 处理器")


if __name__ == "__main__":
    unittest.main()
