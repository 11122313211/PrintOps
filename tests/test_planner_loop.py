"""Replay tests for the bounded model -> tool -> observation loop.

These tests deliberately use scripted planners and a fake HTTP endpoint.  They
do not require credentials or a live model; their purpose is to make the
provider contract and the Agent's safety invariants executable before a real
OpenAI-compatible endpoint is used.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from unittest.mock import patch

from agent import Agent, Memory
from llm_adapter import OpenAICompatiblePlanner


READY_TEXT = "做 500 张 A4 名片，250g铜版纸，双面四色，下周内"


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self.payload


class ScriptedPlanner:
    """Small deterministic planner used to replay Agent-level situations."""

    enabled = True
    model = "scripted"
    last_error = ""

    def __init__(self, *plans: dict[str, Any] | None) -> None:
        self.plans = list(plans)
        self.calls: list[dict[str, Any]] = []

    def plan(self, text: str, order: dict[str, Any], tools: list[dict[str, Any]],
             history: list[dict[str, str]], tool_result: dict[str, Any] | None = None,
             tool_call: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append({
            "text": text,
            "order": order,
            "tools": tools,
            "history": history,
            "tool_result": tool_result,
            "tool_call": tool_call,
            **kwargs,
        })
        if self.plans:
            return self.plans.pop(0)
        return None

    def public_config(self) -> dict[str, Any]:
        return {"enabled": True, "url": "https://example.test/v1", "model": self.model,
                "keyConfigured": False, "lastError": self.last_error}


def tool_plan(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"reply": "", "patch": {},
            "tool": {"name": name, "arguments": arguments or {}}}


def final_plan(reply: str = "综合完成") -> dict[str, Any]:
    return {"reply": reply, "patch": {}, "tool": None}


class PlannerLoopTest(unittest.TestCase):
    def make_agent(self, planner: Any) -> tuple[tempfile.TemporaryDirectory[str], Agent]:
        temporary = tempfile.TemporaryDirectory()
        agent = Agent(Memory(Path(temporary.name) / "agent.sqlite3"), planner=planner)
        return temporary, agent

    def test_native_multiround_carries_every_prior_tool_receipt(self) -> None:
        """A later native request must retain the complete bounded tool ledger."""
        planner = OpenAICompatiblePlanner("https://example.test/v1", "", "demo")
        responses = [
            {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "call-validate", "type": "function",
                "function": {"name": "validate_order", "arguments": "{}"},
            }]}}]},
            {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "call-match", "type": "function",
                "function": {"name": "match_supplier_capability",
                              "arguments": '{"platformId":"generic"}'},
            }]}}]},
            {"choices": [{"message": {"content": '{"reply":"综合完成","patch":{},"tool":null}'}}]},
        ]
        requests: list[dict[str, Any]] = []

        def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
            requests.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(responses[len(requests) - 1])

        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            temporary, agent = self.make_agent(planner)
            try:
                result = agent.chat(READY_TEXT)
            finally:
                temporary.cleanup()

        self.assertIn("综合完成", result["messages"][0])
        self.assertIn("调用工具：validate_order", result["toolTrace"])
        self.assertIn("调用工具：match_supplier_capability", result["toolTrace"])
        self.assertGreaterEqual(len(requests), 3)
        # The third request may encode the ledger as protocol messages or as a
        # bounded explicit ledger field.  In either case both call IDs and both
        # tool names must be observable to the provider.
        final_request = json.dumps(requests[-1], ensure_ascii=False)
        for marker in ("call-validate", "call-match", "validate_order",
                       "match_supplier_capability"):
            self.assertIn(marker, final_request)

    def test_native_loop_applies_model_parameters_before_followup_tools(self) -> None:
        """Model-extracted fields land before the first local tool observes the order."""
        planner = OpenAICompatiblePlanner("https://example.test/v1", "", "demo")
        model_patch = {
            "productType": "名片", "quantity": "500张", "size": "A4",
            "paper": "250g 铜版纸", "printing": "双面四色", "deadline": "下周内",
        }
        responses = [
            {"choices": [{"message": {
                "content": json.dumps({"reply": "已识别订单，开始校验。",
                                         "patch": model_patch, "tool": None}, ensure_ascii=False),
                "tool_calls": [{"id": "patch-validate", "type": "function",
                                "function": {"name": "validate_order", "arguments": "{}"}}],
            }}]},
            {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "patch-recommend", "type": "function",
                 "function": {"name": "recommend_processes", "arguments": "{}"}},
            ]}}]},
            {"choices": [{"message": {"content": json.dumps({
                "reply": "已完成校验并生成方案。", "patch": {}, "tool": None,
            }, ensure_ascii=False)}}]},
        ]
        requests: list[dict[str, Any]] = []

        def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
            requests.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(responses[min(len(requests) - 1, len(responses) - 1)])

        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            temporary, agent = self.make_agent(planner)
            try:
                result = agent.chat("请做一批名片")
            finally:
                temporary.cleanup()

        self.assertEqual(result["order"]["productType"], "名片")
        self.assertEqual(result["order"]["quantity"], "500 张")
        self.assertEqual(result["order"]["size"], '210×297 mm')
        self.assertEqual(result["order"]["paper"], '250 g/m² 铜版纸')
        self.assertEqual([item["name"] for item in result["toolReceipts"]],
                         ["validate_order", "recommend_processes"])
        self.assertIn("已完成校验并生成方案", result["messages"][0])
        self.assertGreaterEqual(len(requests), 3)
        followup = json.dumps(requests[1], ensure_ascii=False)
        self.assertIn("500 张", followup)
        self.assertIn("patch-validate", followup)

    def test_tool_limit_is_followed_by_a_model_synthesis_turn(self) -> None:
        planner = ScriptedPlanner(
            tool_plan("validate_order"),
            tool_plan("match_supplier_capability", {"platformId": "generic"}),
            tool_plan("estimate_price"),
            final_plan("三步结果已经综合完成"),
        )
        temporary, agent = self.make_agent(planner)
        try:
            result = agent.chat(READY_TEXT)
        finally:
            temporary.cleanup()

        self.assertGreaterEqual(len(planner.calls), 4)
        self.assertIn("三步结果已经综合完成", result["messages"][0])
        self.assertIn("调用工具：estimate_price", result["toolTrace"])
        self.assertNotIn("费用区间为", result["messages"][0])

    def test_explicit_price_intent_is_checked_after_a_wrong_tool_call(self) -> None:
        """A model choosing validation first must not suppress a price request."""
        planner = ScriptedPlanner(tool_plan("validate_order"), final_plan("已检查"))
        temporary, agent = self.make_agent(planner)
        try:
            result = agent.chat(READY_TEXT + "，多少钱？")
        finally:
            temporary.cleanup()

        self.assertIn("调用工具：validate_order", result["toolTrace"])
        self.assertIn("调用工具：estimate_price", result["toolTrace"])
        self.assertIsInstance(result["toolResult"], dict)
        self.assertEqual(result["toolResult"].get("type"), "estimate")
        self.assertTrue(result["toolResult"].get("range"))

    def test_blocked_tool_never_gets_a_success_message_when_synthesis_fails(self) -> None:
        planner = ScriptedPlanner(tool_plan("recommend_processes"), None)
        temporary, agent = self.make_agent(planner)
        try:
            result = agent.chat("我想做宣传册")
        finally:
            temporary.cleanup()

        self.assertIsInstance(result["toolResult"], dict)
        self.assertEqual(result["toolResult"].get("status"), "blocked")
        message = result["messages"][0]
        self.assertNotIn("已生成工艺方案", message)
        self.assertTrue(any(word in message for word in ("补充", "不完整", "不能")))

    def test_non_adjacent_repeat_a_b_a_is_blocked(self) -> None:
        planner = ScriptedPlanner(
            tool_plan("validate_order"),
            tool_plan("match_supplier_capability", {"platformId": "generic"}),
            tool_plan("validate_order"),
            final_plan(),
        )
        temporary, agent = self.make_agent(planner)
        try:
            result = agent.chat(READY_TEXT)
        finally:
            temporary.cleanup()

        self.assertEqual(result["toolTrace"].count("调用工具：validate_order"), 1)
        self.assertEqual(result["toolTrace"].count("调用工具：match_supplier_capability"), 1)
        self.assertTrue(any("重复" in item or "循环" in item for item in result["toolTrace"]))

    def test_order_patch_invalidates_old_tool_result_and_reruns_dependent_tool(self) -> None:
        planner = ScriptedPlanner(
            tool_plan("recommend_processes"),
            {"reply": "尺寸已更新", "patch": {"size": "A3"}, "tool": None},
        )
        temporary, agent = self.make_agent(planner)
        try:
            result = agent.chat(READY_TEXT + "，先给我推荐方案")
        finally:
            temporary.cleanup()

        self.assertEqual(result["order"]["size"], '297×420 mm')
        self.assertTrue(result["options"], "更新订单后仍应返回与新状态对应的方案")
        self.assertGreaterEqual(result["toolTrace"].count("调用工具：recommend_processes"), 2)
        descriptions = " ".join(str(item.get("description", "")) for item in result["options"])
        self.assertIn("297×420 mm", descriptions)
        self.assertNotIn("A4", descriptions)

    def test_native_batch_calls_execute_once_each_and_return_each_call_id(self) -> None:
        planner = OpenAICompatiblePlanner("https://example.test/v1", "", "demo")
        batch = {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "batch-validate", "type": "function",
             "function": {"name": "validate_order", "arguments": "{}"}},
            {"id": "batch-estimate", "type": "function",
             "function": {"name": "estimate_price", "arguments": "{}"}},
        ]}}]}
        final = {"choices": [{"message": {"content": '{"reply":"批量工具已完成","patch":{},"tool":null}'}}]}
        requests: list[dict[str, Any]] = []

        def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
            requests.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(batch if len(requests) == 1 else final)

        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            temporary, agent = self.make_agent(planner)
            try:
                result = agent.chat(READY_TEXT)
            finally:
                temporary.cleanup()

        self.assertIn("调用工具：validate_order", result["toolTrace"])
        self.assertIn("调用工具：estimate_price", result["toolTrace"])
        self.assertNotIn("extraToolCalls", " ".join(result["toolTrace"]))
        self.assertGreaterEqual(len(requests), 2)
        followup = json.dumps(requests[-1], ensure_ascii=False)
        self.assertIn("batch-validate", followup)
        self.assertIn("batch-estimate", followup)

    def test_native_followup_http_rejection_uses_one_json_fallback_message(self) -> None:
        planner = OpenAICompatiblePlanner("https://example.test/v1", "", "demo")
        native = {"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "fallback-call", "type": "function",
            "function": {"name": "validate_order", "arguments": "{}"},
        }]}}]}
        final = {"choices": [{"message": {"content": '{"reply":"降级后完成","patch":{},"tool":null}'}}]}
        requests: list[dict[str, Any]] = []

        def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
            requests.append(json.loads(request.data.decode("utf-8")))
            if len(requests) == 1:
                return FakeResponse(native)
            if len(requests) == 2:
                raise HTTPError(request.full_url, 400, "native tools unsupported", {}, None)
            return FakeResponse(final)

        with patch("llm_adapter.urllib.request.urlopen", side_effect=fake_urlopen):
            temporary, agent = self.make_agent(planner)
            try:
                result = agent.chat(READY_TEXT)
            finally:
                temporary.cleanup()

        self.assertIn("降级后完成", result["messages"][0])
        self.assertGreaterEqual(len(requests), 3)
        fallback_request = requests[2]
        user_messages = [item for item in fallback_request["messages"] if item.get("role") == "user"]
        self.assertEqual(len(user_messages), 1)
        fallback_payload = json.loads(user_messages[0]["content"])
        self.assertIn("toolResult", fallback_payload)
        self.assertFalse(any(item.get("role") in {"assistant", "tool"}
                             for item in fallback_request["messages"]))

    def test_compound_recommendation_and_price_intent_forces_both_tools(self) -> None:
        planner = ScriptedPlanner(
            final_plan("我先整理一下。"),
            final_plan("方案和费用已经结合当前订单整理完成。"),
        )
        temporary, agent = self.make_agent(planner)
        try:
            result = agent.chat(READY_TEXT + "，请推荐方案并估个价")
        finally:
            temporary.cleanup()

        self.assertIn("调用工具：recommend_processes", result["toolTrace"])
        self.assertIn("调用工具：estimate_price", result["toolTrace"])
        self.assertEqual(len(result["options"]), 3)
        self.assertIn("结合当前订单", result["messages"][0])
        names = [item.get("name") for item in result["toolReceipts"]]
        self.assertIn("recommend_processes", names)
        self.assertIn("estimate_price", names)

    def test_invalid_required_tool_attempt_is_retried_with_canonical_arguments(self) -> None:
        planner = ScriptedPlanner(
            tool_plan("estimate_price", {"unexpected": True}),
            final_plan("估价已经完成。"),
            final_plan("已根据有效的费用结果完成总结。"),
        )
        temporary, agent = self.make_agent(planner)
        try:
            result = agent.chat(READY_TEXT + "，多少钱？")
        finally:
            temporary.cleanup()

        receipts = [item for item in result["toolReceipts"]
                    if item.get("name") == "estimate_price"]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0].get("status"), "invalid_arguments")
        self.assertTrue(receipts[1].get("result", {}).get("range"))
        self.assertEqual(receipts[1].get("arguments"), {})

    def test_explicit_handoff_cannot_be_suppressed_by_reply_only_model(self) -> None:
        temporary, agent = self.make_agent(None)
        try:
            agent.chat(READY_TEXT)
            agent.choose("balanced")
            agent.planner = ScriptedPlanner(
                final_plan("我会直接处理。"),
                final_plan("交接草稿已生成，仍需人工确认。"),
            )
            result = agent.chat("请生成交接单")
        finally:
            temporary.cleanup()

        self.assertIn("调用工具：prepare_handoff", result["toolTrace"])
        self.assertEqual(result["toolResult"].get("status"), "ready")
        self.assertIsInstance(result.get("handoff"), dict)
        self.assertTrue(result["handoff"].get("requiresHumanConfirmation"))

    def test_negated_quote_is_not_forced(self) -> None:
        planner = ScriptedPlanner(
            final_plan("只检查订单。"),
            final_plan("订单检查完成。"),
        )
        temporary, agent = self.make_agent(planner)
        try:
            result = agent.chat(READY_TEXT + "，先不要询价，只检查订单")
        finally:
            temporary.cleanup()

        self.assertIn("调用工具：validate_order", result["toolTrace"])
        self.assertNotIn("调用工具：request_supplier_quote", result["toolTrace"])

    def test_reply_only_model_cannot_suppress_common_action_intents(self) -> None:
        """Common wording variants still dispatch the authoritative local tool."""
        cases = (
            ("请报价", "request_supplier_quote", True),
            ("报个价", "request_supplier_quote", True),
            ("费用是多少", "estimate_price", False),
            ("能印吗", "match_supplier_capability", False),
            ("订单完整吗", "validate_order", False),
        )
        for phrase, expected_tool, needs_selection in cases:
            with self.subTest(phrase=phrase):
                temporary, agent = self.make_agent(None)
                try:
                    agent.chat(READY_TEXT)
                    if needs_selection:
                        agent.choose("balanced")
                    agent.planner = ScriptedPlanner(
                        final_plan("模型只返回了文字。"),
                        final_plan("已根据本地工具结果完成处理。"),
                    )
                    result = agent.chat(phrase)
                finally:
                    temporary.cleanup()
                self.assertIn(f"调用工具：{expected_tool}", result["toolTrace"])


if __name__ == "__main__":
    unittest.main()
