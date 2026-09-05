"""Feature tests for the v0.9.0 knowledge-and-reliability release.

Covers: per-category supplier field maps, graded rule confidence, the
versioned price model, gang-run/dedicated-press guidance, minimum quantity
warnings, imposition hints, and the compact planner digest.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch as mock_patch

from agent import Agent, Memory
from llm_adapter import OpenAICompatiblePlanner
from nlu import perceive
from order_model import ORDER_DEFAULTS, STATE_SCHEMA_VERSION, imposition_hint
from product_knowledge import PRICE_MODEL_VERSION
from supplier_adapters import get_adapter
from tools import estimate_price, prepare_handoff, recommend_processes, validate_order


class FieldMapTest(unittest.TestCase):
    def test_wildcard_map_applies_to_any_product(self) -> None:
        adapter = get_adapter("generic")
        mapped = adapter.map_order({"productType": "画册", "quantity": "500 本", "paper": ""})
        self.assertEqual(mapped["productType"], "画册")
        self.assertEqual(mapped["quantity"], "500 本")
        self.assertNotIn("paper", mapped)

    def test_category_overlay_renames_provider_fields(self) -> None:
        adapter = get_adapter("shengda")
        card = adapter.map_order({"productType": "名片", "quantity": "500 张",
                                  "productSpecs": {"cardStock": "300g 白卡"}})
        self.assertEqual(card["material"], "300g 白卡")
        box = adapter.map_order({"productType": "包装盒",
                                 "productSpecs": {"boxSize": "60×40×20CM", "boxStructure": "天地盖"}})
        self.assertEqual(box["boxDimension"], "60×40×20CM")
        self.assertEqual(box["boxStyle"], "天地盖")
        self.assertEqual(box["productType"], "包装盒")

    def test_base_fields_survive_category_overlay(self) -> None:
        adapter = get_adapter("shengda")
        mapped = adapter.map_order({"productType": "名片", "deadline": "一周内"})
        self.assertEqual(mapped["deadline"], "一周内")


class SchemaVersionTest(unittest.TestCase):
    def test_fresh_state_carries_schema_version(self) -> None:
        state = Memory.fresh_state()
        self.assertEqual(state["schemaVersion"], STATE_SCHEMA_VERSION)

    def test_legacy_state_is_migrated_and_stamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp) / "agent.sqlite3")
            legacy = {"order": {"productType": "名片", "quantity": "500 张"}}
            memory.save("legacy", legacy)
            state = Agent(memory, "legacy").state
            self.assertEqual(state["schemaVersion"], STATE_SCHEMA_VERSION)
            self.assertEqual(state["order"]["quantityValue"], 500)


class ConfidenceGradingTest(unittest.TestCase):
    def test_explicit_fields_score_above_threshold(self) -> None:
        _, confidence = perceive("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        self.assertGreaterEqual(confidence["quantity"], 0.9)
        self.assertGreaterEqual(confidence["productType"], 0.9)
        self.assertGreaterEqual(confidence["paper"], 0.9)
        self.assertGreaterEqual(confidence["size"], 0.9)
        self.assertGreaterEqual(confidence["printing"], 0.85)
        self.assertGreaterEqual(confidence["deadline"], 0.85)

    def test_weak_inference_scores_below_confirmation_threshold(self) -> None:
        _, confidence = perceive("做个有质感的宣传册")
        self.assertLess(confidence["budget"], 0.75)
        self.assertLess(confidence["purpose"], 0.75)

    def test_bare_number_quantity_is_flagged(self) -> None:
        _, confidence = perceive("做 500 名片，A4")
        self.assertLess(confidence["quantity"], 0.75)
        self.assertGreaterEqual(confidence["quantity"], 0.7)

    def test_labeled_dimensions_are_explicit_evidence(self) -> None:
        _, confidence = perceive("做 500 张折页，成品尺寸 210*267mm，展开尺寸 426*267mm")
        self.assertGreaterEqual(confidence["dimensions.finishedSize"], 0.95)
        self.assertGreaterEqual(confidence["dimensions.expandedSize"], 0.95)

    def test_inferred_spec_placeholders_need_confirmation(self) -> None:
        _, confidence = perceive("做 500 个天地盖包装盒，60*40*20cm，没有刀模")
        self.assertLess(confidence["productSpecs.dieCut"], 0.75)
        self.assertGreaterEqual(confidence["productSpecs.boxStructure"], 0.85)

    def test_preference_fields_do_not_block_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(Memory(Path(tmp) / "agent.sqlite3"))
            agent.chat("我要做 500 份 A4 宣传册，32页骑马钉，下周一要用，想要有质感",
                       {"paper": "待推荐", "printing": "双面四色"})
            agent.choose("balanced")
            result = agent.generate()
            self.assertTrue(result["orderGenerated"],
                            f"preference fields must not block generation: {result['messages']}")

    def test_bare_quantity_blocks_generation_until_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(Memory(Path(tmp) / "agent.sqlite3"))
            agent.chat("做 500 名片，A4，250g铜版纸，双面四色，下周内")
            blocked = agent.generate()
            self.assertFalse(blocked["orderGenerated"])
            self.assertIn("低置信度字段", blocked["messages"][0])
            agent.chat("数量确认是 500 张", {"quantity": "500 张"})
            agent.choose("balanced")
            result = agent.generate()
            self.assertTrue(result["orderGenerated"])


class PriceModelTest(unittest.TestCase):
    ORDER = {"productType": "名片", "quantity": "800 张", "quantityValue": 800,
             "quantityUnit": "张", "size": "90×54MM", "printing": "四色印刷"}

    def test_estimate_carries_pricing_model_version(self) -> None:
        result = estimate_price(self.ORDER)
        self.assertIsNotNone(result["range"])
        self.assertEqual(result["pricingModelVersion"], PRICE_MODEL_VERSION)
        self.assertIn("示例价格参数表", result["assumptions"])

    def test_quantity_tier_is_stepped_not_linear(self) -> None:
        small = estimate_price({**self.ORDER, "quantity": "800 张", "quantityValue": 800})
        large = estimate_price({**self.ORDER, "quantity": "1500 张", "quantityValue": 1500})
        small_low = int(small["range"].split(" - ")[0].lstrip("¥"))
        large_low = int(large["range"].split(" - ")[0].lstrip("¥"))
        self.assertLess(large_low, small_low * 1500 / 800 * 0.95,
                        "larger quantity should drop into a cheaper tier")

    def test_finishing_factor_raises_the_band(self) -> None:
        order = {**self.ORDER, "productType": "单页"}
        plain = estimate_price(order)
        gilded = estimate_price({**order, "finishing": "烫金"})
        self.assertNotEqual(plain["range"], gilded["range"])

    def test_tiny_order_respects_make_ready_floor(self) -> None:
        result = estimate_price({**self.ORDER, "quantity": "10 张", "quantityValue": 10})
        low = int(result["range"].split(" - ")[0].lstrip("¥"))
        self.assertGreaterEqual(low, 80)


class IndustryDepthTest(unittest.TestCase):
    CARD_ORDER = {"productType": "名片", "quantity": "800 张", "quantityValue": 800,
                  "quantityUnit": "张", "size": "90×54MM", "paper": "250g 铜版纸",
                  "printing": "四色印刷", "deadline": "一周内"}

    def test_imposition_hint_counts_pieces_on_sheet(self) -> None:
        hint = imposition_hint("210×285MM")
        self.assertIsNotNone(hint)
        self.assertIn("大度全张", hint)
        self.assertIn("16 裁", hint)
        self.assertIn("参考", hint)

    def test_imposition_hint_rejects_three_dimensional_sizes(self) -> None:
        self.assertIsNone(imposition_hint("60×40×20CM"))
        self.assertIsNone(imposition_hint("A4 / 210×285MM".split(" / ")[1] + "×20CM"))

    def test_options_carry_print_mode_and_reasons(self) -> None:
        options = recommend_processes(self.CARD_ORDER)
        self.assertEqual(len(options), 3)
        self.assertEqual(options[0]["printMode"], "合版")
        self.assertEqual(options[2]["printMode"], "专版")
        self.assertIn("批次色差", options[0]["risk"])
        self.assertIn("颜色可控", options[2]["reason"])

    def test_min_quantity_warning_fires_and_clears(self) -> None:
        low = validate_order({**self.CARD_ORDER, "quantity": "20 张", "quantityValue": 20})
        self.assertTrue(any("起印量" in warning for warning in low["warnings"]))
        fine = validate_order(self.CARD_ORDER)
        self.assertFalse(any("起印量" in warning for warning in fine["warnings"]))

    def test_handoff_includes_imposition_reference(self) -> None:
        result = prepare_handoff({**ORDER_DEFAULTS, **self.CARD_ORDER})
        self.assertIn("开数参考", result["text"])


class PlannerDigestTest(unittest.TestCase):
    class _FakeResponse:
        def __init__(self, payload):
            self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    def test_plan_payload_sends_compact_order_and_described_tools(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return PlannerDigestTest._FakeResponse({"choices": [{"message": {"content": '{"reply": "好的，我来整理", "patch": {}, "tool": null}'}}]})

        with tempfile.TemporaryDirectory() as tmp:
            planner = OpenAICompatiblePlanner("https://example.test/v1", "key", "sample-model")
            agent = Agent(Memory(Path(tmp) / "agent.sqlite3"), planner=planner)
            with mock_patch("llm_adapter.urllib.request.urlopen", fake_urlopen):
                agent.chat("做 500 份 A4 宣传册，32页骑马钉", {"paper": "待推荐"})
        user_content = json.loads(captured["body"]["messages"][-1]["content"])
        order = user_content["order"]
        self.assertTrue(order, "digest should carry the filled fields")
        self.assertNotIn("items", order)
        self.assertNotIn("", [value for value in order.values() if isinstance(value, str)])
        self.assertIn("missingFields", order)
        self.assertTrue(user_content["tools"])
        for tool in user_content["tools"]:
            for prop in tool["input"]["properties"].values():
                self.assertIn("description", prop)


if __name__ == "__main__":
    unittest.main()
