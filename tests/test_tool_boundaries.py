import tempfile
import unittest
from pathlib import Path

from agent import Agent, Memory
from order_model import normalize_order_dimensions


class ToolBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agent = Agent(Memory(Path(self.tmp.name) / "agent.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_product_spec_dimension_aliases_are_migrated_before_cleanup(self):
        order = {
            "productType": "折页",
            "size": "210×297MM",
            "dimensions": {"finishedSize": "210×300MM"},
            "productSpecs": {
                "finishedSize": "210×297MM",
                "expandedSize": "420×594MM",
                "dieCutSize": "426×600MM",
                "packageSize": "60×40×20CM",
                "boxSize": "70×50×30CM",
                "unsafe": {"nested": True},
            },
        }

        normalize_order_dimensions(order)

        # An explicit canonical field wins; legacy aliases fill only blanks.
        self.assertEqual(order["dimensions"], {
            "finishedSize": "210×300MM",
            "expandedSize": "420×594MM",
            "dieCutSize": "426×600MM",
            "packageSize": "60×40×20CM",
        })
        self.assertEqual(order["productSpecs"], {"boxSize": "70×50×30CM"})

        oversized = {"productSpecs": {"finishedSize": "x" * 5000}}
        normalize_order_dimensions(oversized)
        self.assertEqual(len(oversized["dimensions"]["finishedSize"]), 4096)

    def test_preflight_tool_rejects_stringly_typed_or_unsafe_arguments(self):
        invalid_payloads = (
            {"fileName": "encrypted.pdf", "sizeBytes": 1024,
             "encrypted": "true", "readable": True},
            {"fileName": "broken.pdf", "sizeBytes": 1024,
             "encrypted": False, "readable": "false"},
            {"fileName": "/tmp/escape.pdf", "sizeBytes": 1024,
             "encrypted": False, "readable": True},
            {"fileName": "fraction.pdf", "sizeBytes": 1.5,
             "encrypted": False, "readable": True},
            {"fileName": "negative.pdf", "sizeBytes": -1,
             "encrypted": False, "readable": True},
            {"fileName": "huge.pdf", "sizeBytes": 2**53,
             "encrypted": False, "readable": True},
            {"fileName": "fraction-page.pdf", "sizeBytes": 1024,
             "pageCount": "1", "encrypted": False, "readable": True},
            {"fileName": "large-inspection.pdf", "sizeBytes": 1024,
             "encrypted": False, "readable": True,
             "inspection": {"note": "x" * 17000}},
            {"fileName": "large-expected.pdf", "sizeBytes": 1024,
             "encrypted": False, "readable": True,
             "expectedSize": "x" * 129},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                result = self.agent.call_tool("preflight_file", payload)
                self.assertEqual(result["toolResult"]["status"], "invalid_arguments")
                self.assertIsNone(self.agent.state["uploadedFile"])

        valid = self.agent.call_tool("preflight_file", {
            "fileName": "  artwork.pdf  ", "sizeBytes": 1024,
            "pageCount": 1, "encrypted": False, "readable": True,
        })
        self.assertTrue(valid["toolResult"]["ok"])
        self.assertEqual(self.agent.state["uploadedFile"], "artwork.pdf")

    def test_tool_name_platform_and_item_index_are_bounded(self):
        long_name = self.agent.call_tool("x" * 129, {})
        self.assertEqual(long_name["toolResult"]["reason"], "tool_name")

        for payload in (
            {"platformId": "unknown"},
            {"platformId": "x" * 129},
            {"platformId": 7},
        ):
            with self.subTest(payload=payload):
                result = self.agent.call_tool("match_supplier_capability", payload)
                self.assertEqual(result["toolResult"]["status"], "invalid_arguments")

        padded = self.agent.call_tool("match_supplier_capability", {"platformId": " shengda "})
        self.assertNotEqual(padded["toolResult"].get("status"), "invalid_arguments")

        for value in (True, "0", -1, 2**53):
            with self.subTest(itemIndex=value):
                result = self.agent.call_tool("estimate_price", {"itemIndex": value})
                self.assertEqual(result["toolResult"]["status"], "invalid_arguments")

    def test_legacy_order_argument_is_bounded_and_never_overrides_session(self):
        before = self.agent.state["order"].copy()
        accepted = self.agent.call_tool("validate_order", {
            "order": {"productType": "名片", "quantity": "999 张"},
        })
        self.assertNotEqual(accepted["toolResult"].get("status"), "invalid_arguments")
        self.assertEqual(self.agent.state["order"], before)

        oversized = self.agent.call_tool("validate_order", {
            "order": {"note": "x" * (64 * 1024)},
        })
        self.assertEqual(oversized["toolResult"]["status"], "invalid_arguments")

    def test_public_handoff_and_quote_gate_empty_or_unselected_order(self):
        for name in ("prepare_handoff", "request_supplier_quote"):
            with self.subTest(tool=name):
                result = self.agent.call_tool(name, {}, remember=False)
                self.assertEqual(result["toolResult"]["status"], "blocked")
                self.assertEqual(result["toolResult"]["reason"], "order_not_ready")
                self.assertEqual(result["workflowStage"], "collect")
                self.assertIsNone(result["handoff"])
                self.assertEqual(result["quoteRequests"], [])

        self.agent.chat("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        unselected = self.agent.call_tool("prepare_handoff", {}, remember=False)
        self.assertEqual(unselected["toolResult"]["reason"], "selection_required")
        self.assertEqual(unselected["workflowStage"], "recommend")

    def test_public_handoff_and_quote_gate_low_confidence_production_field(self):
        self.agent.chat("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        self.agent.state["fieldMeta"]["paper"]["confidence"] = 0.5

        handoff = self.agent.call_tool("prepare_handoff", {}, remember=False)
        self.assertEqual(handoff["toolResult"]["reason"], "low_confidence")
        self.assertIn("paper", handoff["toolResult"]["uncertain"])
        self.assertIsNone(handoff["handoff"])

        quote = self.agent.call_tool("request_supplier_quote", {"platformId": "shengda"}, remember=False)
        self.assertEqual(quote["toolResult"]["reason"], "low_confidence")
        self.assertIn("paper", quote["toolResult"]["uncertain"])
        self.assertEqual(self.agent.state["quoteRequests"], [])

    def test_product_switch_restoration_rejects_alias_keys_and_booleans(self):
        class AliasKey:
            def __str__(self):
                return "boxStructure"

        self.agent._update_order({"productType": "名片"}, source="user", confidence=1.0)
        self.agent._update_order({"productType": "包装盒",
                                  "productSpecs": {AliasKey(): True}},
                                 source="user", confidence=1.0)
        self.assertEqual(self.agent.state["order"]["productSpecs"], {})

    def test_evidence_does_not_stringify_complex_quotes(self):
        evidence = self.agent._normalize_field_evidence([
            {"field": "size", "quote": {"nested": "value"}, "source": ["model"]},
            {"field": "paper", "quote": "250g 铜版纸", "source": "user"},
        ])
        self.assertNotIn("size", evidence)
        self.assertEqual(evidence["paper"]["quote"], "250g 铜版纸")


if __name__ == "__main__":
    unittest.main()
