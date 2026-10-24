"""Regression cases for the ordered intent dispatch table in Agent.chat.

The table must keep the historical if-chain semantics: negation-aware intent
matching, stable dispatch order when one message matches several intents, and
item-scoped routing after a focus switch.
"""

import tempfile
import unittest
from pathlib import Path

from agent import Agent, Memory


class IntentDispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.tmp.name) / "agent.sqlite3")
        self.agent = Agent(self.memory)

    def tearDown(self):
        self.tmp.cleanup()

    def test_negated_quote_request_does_not_dispatch_quote_intent(self):
        # Matcher level: negation-aware matching suppresses the quote intent.
        self.assertFalse(self.agent._intent_fired("不用报价了，直接生成订单", "quote"))
        self.assertTrue(self.agent._intent_fired("帮我报个价", "quote"))
        # End to end: a held quote request goes stale on the field change, and
        # the negated wording must not open a second awaiting request.
        self.agent.chat("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        quoted = self.agent.chat("帮我报个价")
        self.assertEqual(quoted["toolResult"]["status"], "awaiting_human_confirmation")
        held = self.agent.chat("先不用报价，数量改为 800 张")
        self.assertEqual(held["order"]["quantity"], "800 张")
        self.assertEqual([request["status"] for request in held["quoteRequests"]], ["stale"])
        self.assertIsNone(held["activeQuoteRequestId"])

    def test_focus_switch_routes_follow_up_intent_to_selected_item(self):
        self.agent.chat("做 500 张名片和 1000 张折页，都使用A4，157g哑粉纸，双面四色，下周内")
        focused = self.agent.chat("处理第 2 项：折页")
        self.assertEqual(focused["activeItemIndex"], 1)
        # A focus-only turn routes no tool; the next bare intent belongs to item 2.
        priced = self.agent.chat("多少钱")
        self.assertEqual(priced["activeItemIndex"], 1)
        self.assertEqual(priced["toolResult"]["itemIndex"], 1)
        self.assertIn("调用工具：estimate_price", priced["toolTrace"])
        self.assertEqual(priced["order"]["items"][1]["productType"], "折页")

    def test_multi_product_generate_request_stays_aggregate(self):
        result = self.agent.chat("做 500 张名片和 1000 张折页，都使用A4，157g哑粉纸，双面四色，下周内，生成订单")
        # The aggregate gate owns the turn: generation is attempted for the
        # whole order and blocked per pending item, never partial per item.
        self.assertFalse(result["orderGenerated"])
        self.assertIn("订单还不能生成", result["messages"][0])
        self.assertIn("第 2 项", result["messages"][0])
        negated = self.agent.chat("先不生成订单，第 1 项数量改为 600 张")
        self.assertFalse(negated["orderGenerated"])
        self.assertNotIn("订单还不能生成", negated["messages"][0])
        self.assertEqual(negated["order"]["items"][0]["quantity"], "600 张")

    def test_quote_intent_wins_over_handoff_when_both_match(self):
        # Dispatch order is load-bearing: with both intents in one message the
        # supplier-quote handler owns the turn and handoff never runs.
        self.agent.chat("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        result = self.agent.chat("出报价单和交接单")
        self.assertEqual(len(self.agent.state["quoteRequests"]), 1)
        self.assertEqual(self.agent.state["quoteRequests"][0]["status"], "awaiting_human_confirmation")
        self.assertNotEqual(result["workflowStage"], "export")


if __name__ == "__main__":
    unittest.main()
