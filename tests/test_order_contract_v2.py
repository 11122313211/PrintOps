import json
import tempfile
import unittest
from pathlib import Path

from agent import Agent, Memory
from nlu import perceive
from order_model import normalize_order_dimensions
from unit_normalizer import normalize_dimension, dimension_values


class OrderContractV2Test(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.memory = Memory(Path(self.directory.name) / "orders.sqlite3")
        self.agent = Agent(self.memory, "contract-test")

    def test_units_convert_independently_without_guessing_missing_units(self):
        self.assertEqual(normalize_dimension("60*40*20cm"), "600×400×200 mm")
        self.assertEqual(normalize_dimension("2.5cm×30mm"), "25×30 mm")
        self.assertEqual(normalize_dimension("2in×3in"), "50.8×76.2 mm")
        self.assertEqual(normalize_dimension("A4"), "210×297 mm")
        self.assertIsNone(dimension_values("210×297"))
        self.assertIsNone(dimension_values("-1×2cm"))

    def test_partial_size_update_retains_other_dimension_meanings(self):
        self.agent.chat("折页成品尺寸210×297mm，展开尺寸420×297mm")
        result = self.agent.chat("成品尺寸改为148×210mm")
        self.assertEqual(result["order"]["dimensions"]["expandedSize"], "420×297 mm")
        self.assertEqual(result["order"]["measurements"]["finishedSize"]["width"], 148)
        result = self.agent.chat("手工改尺寸", {"size": "A4"})
        self.assertEqual(result["order"]["dimensions"]["finishedSize"], "210×297 mm")
        self.assertEqual(result["canonicalOrder"]["measurements"]["finishedSize"]["value"]["width"], 210)

    def test_inner_and_outer_box_sizes_are_independent(self):
        result = self.agent.chat("天地盖包装盒，60×40×20cm，内尺寸，外尺寸62×42×22cm")
        values = result["order"]["measurements"]
        self.assertEqual(values["packageInnerSize"]["depth"], 200)
        self.assertEqual(values["packageOuterSize"]["width"], 620)
        self.assertEqual(values["packageInnerSize"]["semantic"], "package_inner")

    def test_negation_correction_and_no_substring_products(self):
        self.assertNotIn("paper", perceive("不要铜版纸的名片")[0])
        self.assertEqual(perceive("不需要A4，改B5名片")[0]["size"], "176×250 mm")
        self.assertEqual(perceive("宣传册32页骑马钉，不需要装订")[0]["binding"], "无需装订")
        self.assertEqual(perceive("名片不要双面，改单面黑白")[0]["printing"], "单面黑白")
        for text in ("标签机", "PVC管"):
            self.assertNotIn("productType", perceive(text)[0])
        self.agent.chat("名片用铜版纸")
        result = self.agent.chat("不要铜版纸")
        self.assertFalse(result["order"]["paper"])
        self.assertTrue(any(c["status"] == "rejected" for c in result["fieldCandidates"]))

    def test_quantity_evidence_and_explicit_confirmation_survive_restart(self):
        text = "大约1k张名片"
        result = self.agent.chat(text)
        self.assertEqual(result["order"]["quantityValue"], 1000)
        self.assertEqual(result["canonicalOrder"]["fields"]["quantity"]["status"], "pending_confirmation")
        self.assertIsNone(result["canonicalOrder"]["fields"]["quantity"]["value"])
        candidate = next(c for c in result["fieldCandidates"] if c["field"] == "quantity")
        self.assertEqual(text[candidate["start"]:candidate["end"]], candidate["rawText"])
        self.agent.chat("确认数量", {"quantity": "1000 张"})
        restored = Agent(self.memory, self.agent.id).snapshot()
        self.assertEqual(restored["canonicalOrder"]["fields"]["quantity"]["status"], "confirmed")
        self.assertEqual(perceive("数量五百张")[0]["quantityValue"], 500)

    def test_postfix_quantities_and_materials_do_not_cross_items(self):
        result = perceive("A4名片500张，A5宣传册1000本")[0]
        self.assertEqual([item["quantityValue"] for item in result["items"]], [500, 1000])
        self.assertEqual(result["quantityValue"], 500)
        result = perceive("500张名片用300g白卡纸，1000本宣传册")[0]
        self.assertEqual(result["items"][0]["paper"], "300 g/m² 白卡纸")
        self.assertFalse(result["items"][1].get("paper"))

    def test_deadlines_preserve_calendar_semantics(self):
        for text in ("一周内", "2周内", "两周内", "5工作日内"):
            self.assertEqual(perceive(text)[0]["deadline"], text)

    def test_missing_canonical_values_are_null(self):
        result = self.agent.snapshot()["canonicalOrder"]
        self.assertEqual(result["schemaVersion"], "2.0")
        self.assertEqual(result["fields"]["quantity"], {"value": None, "status": "missing"})

    def test_editing_sibling_does_not_confirm_unrelated_dimension(self):
        self.agent.chat("折页成品210×297mm，展开420×297mm")
        self.agent.state["fieldMeta"]["dimensions.expandedSize"]["confidence"] = .4
        result = self.agent.chat("调整成品", {"dimensions": {"finishedSize": "148×210mm"}})
        self.assertEqual(result["fieldMeta"]["dimensions.expandedSize"]["confidence"], .4)
        self.assertIn("dimensions.expandedSize", self.agent._low_confidence_fields())

    def test_unlabelled_units_block_handoff_even_when_user_patch_is_confirmed(self):
        self.agent.chat("做500张名片，300g白卡纸，双面四色，下周内", {"size": "90×54"})
        self.agent.choose("balanced")
        self.assertFalse(self.agent.generate()["orderGenerated"])
        self.assertIn("需明确 mm", " ".join(self.agent.snapshot()["validation"]["missing"]))

    def test_item_view_does_not_inherit_item_one_material(self):
        self.agent.chat("500张300g白卡纸名片和1000本宣传册")
        self.assertFalse(self.agent._item_order(1)["paper"])

    def test_focused_candidate_confirmation_uses_stable_item_identity(self):
        self.agent.chat("500张名片和1000本宣传册")
        self.agent.chat("希望高级有质感", item_index=1)
        result = self.agent.chat("确认偏好", {"budget": "优先视觉质感"}, item_index=1)
        candidates = [c for c in result["fieldCandidates"] if c["field"] == "items.item-2.budget"]
        self.assertTrue(candidates)
        self.assertTrue(all(c["status"] == "confirmed" for c in candidates))

    def test_repeated_literal_evidence_is_scoped_to_each_product(self):
        text = "500张铜版纸名片和1000本铜版纸宣传册"
        result = self.agent.chat(text)
        candidates = {c["field"]: c for c in result["fieldCandidates"]}
        a, b = candidates["items.item-1.paper"], candidates["items.item-2.paper"]
        self.assertLess(a["start"], b["start"])
        self.assertEqual(text[b["start"]:b["end"]], "铜版纸")

    def test_arabic_and_chinese_multipliers_keep_magnitude(self):
        for text, value in (("2万张名片", 20000), ("3千张名片", 3000),
                            ("3 千张名片", 3000), ("三千张名片", 3000)):
            with self.subTest(text=text):
                self.assertEqual(perceive(text)[0]["quantityValue"], value)

    def test_repeated_quantities_stay_in_their_clause(self):
        data, confidence = perceive("名片500张改成800张，宣传册100本")
        self.assertEqual([i["quantityValue"] for i in data["items"]], [800, 100])
        self.assertGreaterEqual(confidence["items.item-1.quantity"], .75)
        data, confidence = perceive("名片500张还是800张，宣传册100本")
        self.assertEqual(data["items"][1]["quantityValue"], 100)
        self.assertLess(confidence["items.item-1.quantity"], .75)

    def test_shared_deadline_does_not_share_item_material(self):
        result = perceive("500张名片用300g白卡纸和1000本宣传册，都下周内") [0]
        self.assertFalse(result["items"][1].get("paper"))
        self.assertTrue(all(item["deadline"] for item in result["items"]))

    def test_material_alternative_remains_pending_until_user_resolves(self):
        result = self.agent.chat("500张名片，铜版纸还是白卡纸，90×54mm，双面四色，下周内")
        self.assertEqual(result["canonicalOrder"]["fields"]["paper"]["status"], "pending_confirmation")
        self.assertIsNone(result["canonicalOrder"]["fields"]["paper"]["value"])
        self.assertEqual(result["canonicalOrder"]["fields"]["quantity"]["status"], "inferred")
        self.assertEqual(result["canonicalOrder"]["fields"]["printing"]["status"], "inferred")
        blocked = self.agent.generate()
        self.assertFalse(blocked["orderGenerated"])
        self.assertIn("paper", blocked["toolResult"]["uncertain"])
        result = self.agent.chat("确认纸张", {"paper": "300g白卡纸"})
        self.assertEqual(result["canonicalOrder"]["fields"]["paper"]["status"], "confirmed")

    def test_pending_quantity_cannot_be_used_for_price_or_supplier_draft(self):
        result = self.agent.chat("大约1000张名片，90×54mm，300g白卡纸，双面四色，无特殊工艺，下周内，估算费用")
        self.assertEqual(result["toolResult"]["reason"], "low_confidence")
        self.assertIsNone(result["toolResult"]["range"])
        self.assertEqual(self.agent.call_tool("estimate_price")["toolResult"]["status"], "blocked")
        self.assertEqual(self.agent.call_tool("prepare_handoff")["toolResult"]["reason"], "low_confidence")
        self.agent.chat("确认数量", {"quantity": "1000张"})
        self.assertIsNotNone(self.agent.call_tool("estimate_price")["toolResult"]["range"])

    def test_canonical_order_references_raw_evidence_without_embedding_it(self):
        text = "500个天地盖包装盒，内尺寸60×40×20cm，350g白卡纸"
        result = self.agent.chat(text)
        canonical = result["canonicalOrder"]
        self.assertNotIn("cm", json.dumps(canonical, ensure_ascii=False))
        self.assertNotIn("boxSizeInner", canonical["productSpecs"])
        self.assertEqual(canonical["measurements"]["packageInnerSize"]["value"]["width"], 600)
        self.assertIn("evidenceRef", canonical["measurements"]["packageInnerSize"]["provenance"])
        self.assertTrue(any("60×40×20cm" in entry["rawText"] for entry in result["fieldCandidates"]))
