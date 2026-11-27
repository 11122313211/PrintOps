from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

from printops.pricing.engine import estimate_order, load_rate_card
from printops.pricing.simulator import (
    DEFAULT_TIER_PRESETS,
    build_adjustable_options,
    sanitize_overrides,
    simulate_quantity_tiers,
    simulate_what_if,
)

TODAY = date(2026, 11, 15)
ROOT = Path(__file__).resolve().parents[1]


def _sample_orders() -> dict[str, dict]:
    return {
        "名片": {
            "productType": "名片",
            "quantity": "200 张",
            "quantityValue": 200,
            "quantityUnit": "张",
            "size": "90×54 mm",
            "paper": "250g 铜版纸",
            "printing": "双面四色",
            "finishing": "无特殊工艺",
            "binding": "无需装订",
        },
        "单页": {
            "productType": "单页",
            "quantity": "500 张",
            "quantityValue": 500,
            "quantityUnit": "张",
            "size": "210×297 mm",
            "paper": "157g 铜版纸",
            "printing": "双面四色",
            "finishing": "无特殊工艺",
        },
        "宣传册": {
            "productType": "宣传册",
            "quantity": "100 本",
            "quantityValue": 100,
            "quantityUnit": "本",
            "size": "210×297 mm",
            "pages": "16 页",
            "paper": "157g 铜版纸",
            "printing": "双面四色",
            "binding": "骑马钉",
            "finishing": "无特殊工艺",
        },
        "包装盒": {
            "productType": "包装盒",
            "quantity": "300 个",
            "quantityValue": 300,
            "quantityUnit": "个",
            "dimensions": {"expandedSize": "600×400 mm"},
            "paper": "300g 白卡纸",
            "printing": "单面四色",
            "finishing": "哑膜",
            "productSpecs": {"boxStructure": "折叠盒"},
        },
        "纸杯": {
            "productType": "纸杯",
            "quantity": "1000 个",
            "quantityValue": 1000,
            "quantityUnit": "个",
            "paper": "280g 淋膜纸",
            "printing": "单面四色",
            "finishing": "无特殊工艺",
            "productSpecs": {"cupVolume": "250ml"},
        },
        "联单": {
            "productType": "联单",
            "quantity": "50 本",
            "quantityValue": 50,
            "quantityUnit": "本",
            "size": "190×95 mm",
            "paper": "40g 无碳纸",
            "printing": "单面单色",
            "finishing": "无特殊工艺",
            "productSpecs": {"paperParts": "三联"},
        },
        "PVC": {
            "productType": "PVC",
            "quantity": "5 块",
            "quantityValue": 5,
            "quantityUnit": "块",
            "size": "600×900 mm",
            "printing": "单面四色",
            "finishing": "无特殊工艺",
            "productSpecs": {
                "displayMaterial": "PVC板",
                "boardThickness": "5mm",
                "install": "自行安装",
            },
        },
        "标签": {
            "productType": "标签",
            "quantity": "500 张",
            "quantityValue": 500,
            "quantityUnit": "张",
            "size": "50×50 mm",
            "printing": "单面四色",
            "finishing": "无特殊工艺",
            "productSpecs": {
                "labelMaterial": "铜版不干胶",
                "labelShape": "圆形",
                "adhesive": "普通胶",
                "labelFormat": "张装",
            },
        },
    }


class PricingSimulatorMultiCategoryTest(unittest.TestCase):
    def setUp(self):
        self.card = load_rate_card()
        self.orders = _sample_orders()

    def test_all_eight_categories_simulate_tiers_monotonically(self):
        for name, order in self.orders.items():
            with self.subTest(category=name):
                snapshot = deepcopy(order)
                result = simulate_what_if(order, rate_card=self.card, as_of=TODAY)

                # 绝不原地修改输入订单
                self.assertEqual(order, snapshot)
                self.assertEqual(result["type"], "pricing_simulation")
                self.assertTrue(result["readOnly"])
                self.assertEqual(result["baseline"]["status"], "ready")
                self.assertEqual(result["simulated"]["status"], "ready")
                self.assertFalse(result["hasOverrides"])
                self.assertIn("参考", result["disclaimer"])

                tiers = result["tiers"]
                self.assertGreaterEqual(len(tiers), 5)
                self.assertLessEqual(len(tiers), 6)

                # 严格升序去重且包含当前数量
                qtys = [tier["quantity"] for tier in tiers]
                self.assertEqual(qtys, sorted(set(qtys)))
                self.assertIn(order["quantityValue"], qtys)
                self.assertEqual(sum(1 for tier in tiers if tier["isCurrent"]), 1)

                # 总价单调不减，折合单价单调不增且不四舍五入归零
                totals = [Decimal(tier["mid"]) for tier in tiers]
                units = [Decimal(tier["mid"]) / Decimal(tier["quantity"]) for tier in tiers]
                self.assertEqual(totals, sorted(totals))
                self.assertEqual(units, sorted(units, reverse=True))
                for tier in tiers:
                    self.assertEqual(tier["status"], "ready")
                    self.assertIsNotNone(tier["unitMid"])
                    self.assertGreater(Decimal(tier["unitMid"]), Decimal("0"))

                # 可严格序列化为无 NaN JSON
                json.dumps(result, allow_nan=False)

    def test_sweet_spot_detection_and_chinese_summary(self):
        # 200 张名片仍在 100 元最低收费内，升至高档位单价显著下降，必能识别拐点
        card_sim = simulate_quantity_tiers(self.orders["名片"], rate_card=self.card, as_of=TODAY)
        sweet = card_sim["sweetSpot"]
        self.assertIsNotNone(sweet)
        self.assertGreater(sweet["quantity"], 200)
        self.assertLessEqual(sweet["quantity"], 200 * 5)
        self.assertGreaterEqual(sweet["unitDropPct"], 15.0)
        self.assertIn("升至", sweet["summary"])
        self.assertIn("折合单价下降约", sweet["summary"])
        flagged = [t for t in card_sim["tiers"] if t["isSweetSpot"]]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["quantity"], sweet["quantity"])

        # 50000 张单页已处于边际收益平缓区（单价降幅远小于 15%），不应强推拐点
        huge_flyer = {
            **self.orders["单页"],
            "quantity": "50000 张",
            "quantityValue": 50000,
        }
        flat_sim = simulate_quantity_tiers(huge_flyer, rate_card=self.card, as_of=TODAY)
        self.assertIsNone(flat_sim["sweetSpot"])
        self.assertFalse(any(t["isSweetSpot"] for t in flat_sim["tiers"]))

    def test_very_low_unit_price_keeps_four_decimals_not_zero(self):
        # 10000 张名片折合单价远低于 0.1 元，必须保留足够精度绝非 "0.00"
        bulk_card = {
            **self.orders["名片"],
            "quantity": "10000 张",
            "quantityValue": 10000,
        }
        sim = simulate_quantity_tiers(
            bulk_card, rate_card=self.card, quantities=[1000, 5000, 10000, 50000], as_of=TODAY
        )
        for tier in sim["tiers"]:
            self.assertNotEqual(tier["unitMid"], "0.00")
            self.assertGreater(Decimal(tier["unitMid"]), Decimal("0"))

    def test_what_if_process_and_material_deltas_match_engine_exact_decimal(self):
        base = {**self.orders["名片"], "quantity": "1000 张", "quantityValue": 1000}
        snapshot = deepcopy(base)

        # 1. 名片增加烫金 + 哑膜 + 切圆角
        sim = simulate_what_if(
            base,
            {"finishing": ["哑膜", "烫金"], "productSpecs": {"cardCorners": "圆角"}},
            rate_card=self.card,
            as_of=TODAY,
        )
        self.assertEqual(base, snapshot)
        self.assertTrue(sim["hasOverrides"])
        self.assertEqual(sim["appliedOverrides"]["finishing"], "哑膜、烫金")
        self.assertEqual(sim["appliedOverrides"]["productSpecs"], {"cardCorners": "圆角"})
        self.assertTrue(sim["delta"]["comparable"])
        self.assertEqual(sim["delta"]["direction"], "up")

        expected_base = estimate_order(base, rate_card=self.card, as_of=TODAY)
        expected_sim = estimate_order(
            {**base, "finishing": "哑膜、烫金", "productSpecs": {"cardCorners": "圆角"}},
            rate_card=self.card,
            as_of=TODAY,
        )
        expected_diff = Decimal(expected_sim["range"]["mid"]) - Decimal(expected_base["range"]["mid"])
        self.assertEqual(Decimal(sim["delta"]["midDiff"]), expected_diff)
        self.assertTrue(sim["delta"]["midDiff"].startswith("+"))

        # 2. 切换专版（开机费 50 -> 260）显著增费
        offset_sim = simulate_what_if(base, {"printMode": "专版"}, rate_card=self.card, as_of=TODAY)
        self.assertEqual(base, snapshot)
        self.assertGreater(Decimal(offset_sim["delta"]["midDiff"]), Decimal("100"))
        self.assertEqual(offset_sim["simulated"]["drivers"]["printMode"], "专版")

        # 3. 修改纸张克重与装订/封面纸（宣传册）
        booklet = deepcopy(self.orders["宣传册"])
        booklet_snap = deepcopy(booklet)
        booklet_sim = simulate_what_if(
            booklet,
            {
                "paper": "200g 哑粉纸",
                "binding": "胶装",
                "productSpecs": {"coverPaper": "300g 白卡纸"},
            },
            rate_card=self.card,
            as_of=TODAY,
        )
        self.assertEqual(booklet, booklet_snap)
        self.assertEqual(booklet_sim["simulated"]["status"], "ready")
        self.assertEqual(booklet_sim["delta"]["direction"], "up")
        self.assertIn("cover_material", [r["code"] for r in booklet_sim["simulated"]["lineItems"]])

        # 4. 展板板厚降级（5mm -> 3mm）应省钱（负差额）
        pvc = deepcopy(self.orders["PVC"])
        pvc_sim = simulate_what_if(
            pvc,
            {"quantity": 20, "productSpecs": {"boardThickness": "3mm"}},
            rate_card=self.card,
            as_of=TODAY,
        )
        self.assertEqual(pvc_sim["simulated"]["status"], "ready")
        # 同数量降板厚必为负差额
        pvc_same_qty = simulate_what_if(
            {**pvc, "quantity": "20 块", "quantityValue": 20},
            {"productSpecs": {"boardThickness": "3mm"}},
            rate_card=self.card,
            as_of=TODAY,
        )
        self.assertEqual(pvc_same_qty["delta"]["direction"], "down")
        self.assertTrue(pvc_same_qty["delta"]["midDiff"].startswith("-"))

        # 5. 标签与纸杯专属驱动试算
        cup_sim = simulate_what_if(
            self.orders["纸杯"],
            {"quantity": 5000, "productSpecs": {"cupVolume": "500ml"}},
            rate_card=self.card,
            as_of=TODAY,
        )
        self.assertEqual(cup_sim["simulated"]["status"], "ready")
        self.assertEqual(cup_sim["simulated"]["drivers"]["cupVolume"], "500ml")

        label_sim = simulate_what_if(
            self.orders["标签"],
            {"paper": "PET"},
            rate_card=self.card,
            as_of=TODAY,
        )
        self.assertEqual(label_sim["simulated"]["status"], "ready")
        self.assertEqual(label_sim["simulatedValues"]["material"], "PET")

        # 6. 清空表面工艺应产生负差额（从 包装盒 含哑膜 -> 无工艺）
        box_clear = simulate_what_if(
            self.orders["包装盒"],
            {"finishing": []},
            rate_card=self.card,
            as_of=TODAY,
        )
        self.assertEqual(box_clear["simulated"]["status"], "ready")
        self.assertEqual(box_clear["delta"]["direction"], "down")
        self.assertTrue(box_clear["delta"]["midDiff"].startswith("-"))

        # 7. 折页切换折法（二折 -> 风琴折）与画册清空封面纸恢复同内页
        folded = {
            "productType": "折页",
            "quantity": "1000 张",
            "quantityValue": 1000,
            "quantityUnit": "张",
            "dimensions": {"expandedSize": "420×297 mm"},
            "paper": "157g 铜版纸",
            "printing": "双面四色",
            "finishing": "无特殊工艺",
            "productSpecs": {"folding": "二折"},
        }
        folded_sim = simulate_what_if(
            folded,
            {"productSpecs": {"folding": "风琴折"}},
            rate_card=self.card,
            as_of=TODAY,
        )
        self.assertEqual(folded_sim["simulated"]["status"], "ready")
        self.assertEqual(folded_sim["delta"]["direction"], "up")

        album_with_cover = {
            **self.orders["宣传册"],
            "productSpecs": {"coverPaper": "350g 白卡纸"},
        }
        album_plain = simulate_what_if(
            album_with_cover,
            {"productSpecs": {"coverPaper": ""}},
            rate_card=self.card,
            as_of=TODAY,
        )
        self.assertEqual(album_plain["simulated"]["status"], "ready")
        self.assertEqual(album_plain["delta"]["direction"], "down")
        self.assertNotIn("cover_material", [r["code"] for r in album_plain["simulated"]["lineItems"]])

    def test_adjustable_options_only_offer_calibrated_choices_and_preserve_current(self):
        for name, order in self.orders.items():
            with self.subTest(category=name):
                opts = build_adjustable_options(order, self.card)
                self.assertTrue(opts["materials"])
                self.assertEqual(opts["finishings"], list(self.card["finishing"].keys()))
                self.assertEqual(opts["printModes"], list(self.card["printing"].keys()))

                if name == "宣传册":
                    self.assertEqual(opts["bindings"], ["骑马钉", "胶装", "锁线胶装"])
                    self.assertIn("coverPaper", opts["specOptions"])
                else:
                    self.assertEqual(opts["bindings"], [])

                # 任选一个建议材料试算都应保持 ready
                sample_mat = opts["materials"][-1]
                mat_field = opts["materialField"]
                if mat_field == "paper":
                    ov = {"paper": sample_mat}
                else:
                    spec_key = mat_field.split(".", 1)[1]
                    ov = {"productSpecs": {spec_key: sample_mat}}
                sim = simulate_what_if(order, ov, rate_card=self.card, as_of=TODAY)
                self.assertEqual(sim["simulated"]["status"], "ready")

        # 订单中带有未标定材料时，也保留在选项中供 UI 忠实回显
        custom = {**self.orders["名片"], "paper": "320g 荷兰白卡"}
        opts = build_adjustable_options(custom, self.card)
        self.assertIn("320g 荷兰白卡", opts["materials"])

    def test_default_tier_presets_cover_all_calibrated_categories(self):
        for category_name, meta in self.card["categories"].items():
            if meta.get("supported"):
                self.assertIn(category_name, DEFAULT_TIER_PRESETS)


class PricingSimulatorValidationAndBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.card = load_rate_card()
        self.base_card = _sample_orders()["名片"]

    def test_sanitize_overrides_whitelist_and_bounds(self):
        self.assertEqual(sanitize_overrides(None), {})
        self.assertEqual(
            sanitize_overrides({
                "quantity": "1,000 张",
                "paper": "300g 铜版纸",
                "finishing": ["哑膜", "局部 UV", "哑膜"],
                "pages": 24,
                "productSpecs": {"cardCorners": "圆角"},
            }),
            {
                "quantity": 1000,
                "paper": "300g 铜版纸",
                "finishing": "哑膜、局部UV",
                "pages": "24 页",
                "productSpecs": {"cardCorners": "圆角"},
            },
        )
        self.assertEqual(sanitize_overrides({"finishing": []}), {"finishing": "无特殊工艺"})

        bad_cases = [
            "not-a-dict",
            {"unknownField": "x"},
            {"size": "100×100 mm"},
            {"productType": "单页"},
            {"productSpecs": {"illegalSpec": "foo"}},
            {"productSpecs": "not-a-dict"},
            {"quantity": 0},
            {"quantity": -50},
            {"quantity": True},
            {"quantity": 10_000_001},
            {"quantity": "100 箱"},
            {"pages": 0},
            {"pages": 5000},
            {"paper": "a" * 129},
            {"paper": "bad\x00char"},
            {"finishing": 123},
            {"finishing": ["哑膜"] * 13},
            {"printMode": ["合版"]},
        ]
        for bad in bad_cases:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sanitize_overrides(bad)

    def test_custom_quantities_validation(self):
        with self.assertRaises(ValueError):
            simulate_quantity_tiers(self.base_card, rate_card=self.card, quantities=[], as_of=TODAY)
        with self.assertRaises(ValueError):
            simulate_quantity_tiers(
                self.base_card, rate_card=self.card, quantities=[1, 2, 3, 4, 5, 6, 7, 8, 9], as_of=TODAY
            )
        with self.assertRaises(ValueError):
            simulate_quantity_tiers(self.base_card, rate_card=self.card, quantities=[0, 100], as_of=TODAY)

    def test_uncalibrated_process_degrades_to_partial_without_fake_price(self):
        sim = simulate_what_if(
            self.base_card, {"finishing": "纳米覆膜"}, rate_card=self.card, as_of=TODAY
        )
        self.assertEqual(sim["baseline"]["status"], "ready")
        self.assertEqual(sim["simulated"]["status"], "partial")
        self.assertIsNone(sim["simulated"]["range"])
        self.assertFalse(sim["delta"]["comparable"])
        self.assertIsNone(sim["delta"]["midDiff"])
        self.assertIsNone(sim["sweetSpot"])
        self.assertTrue(any("纳米覆膜" in u for u in sim["simulated"]["unpriced"]))

    def test_missing_dimensions_or_uncalibrated_category_returns_blocked(self):
        no_size = {**self.base_card, "size": ""}
        sim = simulate_what_if(no_size, rate_card=self.card, as_of=TODAY)
        self.assertEqual(sim["baseline"]["status"], "blocked")
        self.assertEqual(sim["simulated"]["status"], "blocked")
        self.assertIsNone(sim["sweetSpot"])
        self.assertTrue(all(t["status"] == "blocked" for t in sim["tiers"]))

        unknown_cat = {"productType": "不存在的品类", "quantity": "100 个", "quantityValue": 100, "quantityUnit": "个"}
        sim_unknown = simulate_what_if(unknown_cat, rate_card=self.card, as_of=TODAY)
        self.assertEqual(sim_unknown["baseline"]["status"], "blocked")
        self.assertEqual(sim_unknown["baseline"]["reason"], "category_uncalibrated")
        self.assertIsNone(sim_unknown["sweetSpot"])

        # 数量单位不兼容绝不悄悄换算
        bad_unit = {**self.base_card, "quantity": "200 份", "quantityUnit": "份"}
        sim_unit = simulate_what_if(bad_unit, rate_card=self.card, as_of=TODAY)
        self.assertEqual(sim_unit["baseline"]["status"], "blocked")
        self.assertEqual(sim_unit["baseline"]["reason"], "quantity_unit_incompatible")
        self.assertTrue(all(t["status"] == "blocked" for t in sim_unit["tiers"]))


class PricingSimulatorFrontendAssetTest(unittest.TestCase):
    def test_js_syntax_exports_and_no_innerhtml(self):
        js_path = ROOT / "web" / "js" / "pricing_simulator.js"
        self.assertTrue(js_path.is_file())
        source = js_path.read_text(encoding="utf-8")

        # 严禁 innerHTML（XSS 红线）
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("outerHTML", source)
        self.assertNotIn("insertAdjacentHTML", source)

        # 顶层 ESM 导出检查
        exports = set(
            re.findall(r"^export\s+(?:async\s+)?(?:function|const|let|var)\s+([A-Za-z0-9_$]+)", source, re.M)
        )
        self.assertIn("renderPricingSimulator", exports)
        self.assertIn("resetSimulatorState", exports)

        # node --check 语法校验
        node = shutil.which("node")
        if node:
            proc = subprocess.run(
                [node, "--check", str(js_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_css_balanced_braces_tokens_and_responsive_rules(self):
        css_path = ROOT / "web" / "css" / "pricing-simulator.css"
        self.assertTrue(css_path.is_file())
        text = css_path.read_text(encoding="utf-8")
        cleaned = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        self.assertEqual(cleaned.count("{"), cleaned.count("}"))
        self.assertIn('html[data-theme="high-contrast"]', text)
        self.assertIn("var(--paper-light)", text)
        self.assertIn("@media (max-width: 440px)", text)


if __name__ == "__main__":
    unittest.main()
