"""Feature tests for the v0.9.1 evaluation-and-size-semantics release."""

from __future__ import annotations

import unittest

from nlu import perceive
from tools import validate_order

BASE_BOX = {"productType": "包装盒", "quantity": "500 个", "quantityValue": 500,
            "quantityUnit": "个", "size": "60×40×20CM", "paper": "350g 白卡纸",
            "printing": "双面四色", "deadline": "下周内"}


class SizeSemanticsTest(unittest.TestCase):
    def test_single_digit_dimensions_are_recognized(self) -> None:
        data, _ = perceive("做 2000 张 5*5cm 圆形不干胶标签")
        self.assertEqual(data["size"], "5×5CM")

    def test_inner_and_outer_box_sizes_are_recorded(self) -> None:
        inner, _ = perceive("做 500 个包装盒，内尺寸 60*40*20cm")
        self.assertEqual(inner["productSpecs"]["boxSizeInner"], "60×40×20CM")
        self.assertEqual(inner["productSpecs"]["boxSize"], "60×40×20CM")
        outer, _ = perceive("做 500 个包装盒，外尺寸 65*45*25cm")
        self.assertEqual(outer["productSpecs"]["boxSizeOuter"], "65×45×25CM")

    def test_missing_size_meaning_is_flagged_in_suggestions(self) -> None:
        result = validate_order(BASE_BOX)
        self.assertTrue(any("内尺寸还是外尺寸" in item for item in result["suggestions"]))
        with_inner = validate_order({**BASE_BOX, "productSpecs": {"boxSize": "60×40×20CM",
                                                                  "boxSizeInner": "60×40×20CM"}})
        self.assertFalse(any("内尺寸还是外尺寸" in item for item in with_inner["suggestions"]))


class PerceptionFixTest(unittest.TestCase):
    def test_negation_window_does_not_cross_punctuation(self) -> None:
        data, _ = perceive("不要烫金，改哑膜")
        self.assertEqual(data["finishing"], "哑膜")

    def test_follow_up_turn_keeps_category_spec_context(self) -> None:
        data, confidence = perceive("改成可移胶", product_hint="标签")
        self.assertEqual(data["productSpecs"]["adhesive"], "可移胶")
        self.assertGreaterEqual(confidence["productSpecs.adhesive"], 0.85)
        self.assertNotIn("productType", data)

    def test_inner_coating_negation_matches(self) -> None:
        data, _ = perceive("不需要内淋膜", product_hint="纸杯")
        self.assertEqual(data["productSpecs"]["innerCoating"], "不需要内淋膜")

    def test_smart_card_implies_chip(self) -> None:
        data, _ = perceive("做 600 张智能卡PVC卡，厚度0.76mm")
        self.assertEqual(data["productSpecs"]["chip"], "需要芯片/磁条")

    def test_bao_unit_is_not_stolen_from_package_name(self) -> None:
        data, confidence = perceive("做 500 包装盒")
        self.assertEqual(data["quantity"], "500 个")
        self.assertEqual(data["quantityUnit"], "个")
        self.assertLess(confidence["quantity"], 0.75)

    def test_quantity_follows_its_product_in_multi_orders(self) -> None:
        data, _ = perceive("做 500 张名片、1000 张折页和 2000 张单页")
        quantities = [item["quantity"] for item in data["items"]]
        self.assertEqual(quantities, ["500 张", "1000 张", "2000 张"])

    def test_pre_mention_qualifiers_stay_with_their_item(self) -> None:
        data, _ = perceive("做 500 个天地盖包装盒和 800 张单页")
        self.assertEqual(data["items"][0]["productSpecs"]["boxStructure"], "天地盖")
        self.assertEqual(data["items"][0]["quantity"], "500 个")
        self.assertEqual(data["items"][1]["quantity"], "800 张")

    def test_quantity_revision_uses_current_category_unit(self) -> None:
        data, _ = perceive("数量改为 3000", product_hint="名片")
        self.assertEqual(data["quantity"], "3000 张")


if __name__ == "__main__":
    unittest.main()
