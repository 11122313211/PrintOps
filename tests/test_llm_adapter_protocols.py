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
            requests.append(json.loads(request.data.decode("utf-8")))
            return _Response({"choices": [{"message": {"content": "OK"}}]})

        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            result = _planner().test_capabilities()
        self.assertTrue(result["chat"])
        self.assertFalse(result["jsonPlan"])
        self.assertFalse(result["nativeTools"])
        self.assertFalse(result["toolContinuation"])
        self.assertEqual(result["chosenProtocol"], "chat_only")
        self.assertFalse(result["agentReady"])
        self.assertEqual(len(requests), 3)
        self._assert_no_order_payload(requests)

    def test_capabilities_selects_json_fallback(self) -> None:
        requests: list[dict] = []

        def fake_urlopen(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            requests.append(body)
            if len(requests) == 1:
                return _Response({"choices": [{"message": {"content": "OK"}}]})
            if len(requests) == 2:
                return _Response({"choices": [{"message": {"content":
                    '{"reply":"PRINTOPS_CAPABILITY_JSON_OK","patch":{},"tool":null}'}}]})
            raise HTTPError(request.full_url, 400, "tools unsupported", {}, None)

        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            result = _planner().test_capabilities()
        self.assertTrue(result["chat"])
        self.assertTrue(result["jsonPlan"])
        self.assertFalse(result["nativeTools"])
        self.assertEqual(result["chosenProtocol"], "json_fallback")
        self.assertTrue(result["agentReady"])
        self.assertEqual(result["errors"]["nativeTools"], "HTTP 400")
        self._assert_no_order_payload(requests)

    def test_capabilities_selects_native_only_after_continuation(self) -> None:
        requests: list[dict] = []

        def fake_urlopen(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            requests.append(body)
            if len(requests) == 1:
                return _Response({"choices": [{"message": {"content": "OK"}}]})
            if len(requests) == 2:
                return _Response({"choices": [{"message": {"content":
                    '{"reply":"PRINTOPS_CAPABILITY_JSON_OK","patch":{},"tool":null}'}}]})
            if len(requests) == 3:
                return _Response({"choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "probe-call", "type": "function",
                    "function": {"name": "printops_capability_probe",
                                 "arguments": '{"token":"printops-probe-v1"}'},
                }]}}]})
            return _Response({"choices": [{"message": {
                "content": "PRINTOPS_CAPABILITY_CONTINUATION_OK",
            }}]})

        planner = _planner()
        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            result = planner.test_capabilities()
        self.assertTrue(result["chat"])
        self.assertTrue(result["jsonPlan"])
        self.assertTrue(result["nativeTools"])
        self.assertTrue(result["toolContinuation"])
        self.assertEqual(result["chosenProtocol"], "native_tools")
        self.assertTrue(result["agentReady"])
        self.assertEqual(planner.last_protocol, "native_tools")
        continuation = requests[-1]
        self.assertEqual([item["role"] for item in continuation["messages"][-2:]],
                         ["assistant", "tool"])
        self.assertEqual(continuation["messages"][-1]["tool_call_id"], "probe-call")
        self._assert_no_order_payload(requests)


if __name__ == "__main__":
    unittest.main()
