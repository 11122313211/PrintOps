from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

from pricing_engine import estimate_order, load_rate_card
from pricing_schema import validate_rate_card
_calibration_spec = importlib.util.spec_from_file_location("calibrate_pricing", Path(__file__).resolve().parents[1] / "tools/calibrate_pricing.py")
_calibration = importlib.util.module_from_spec(_calibration_spec)
_calibration_spec.loader.exec_module(_calibration)
calibrate, REQUIRED_COLUMNS = _calibration.calibrate, _calibration.REQUIRED_COLUMNS


TODAY = date(2026, 11, 15)
CARD_ORDER = {"productType": "名片", "quantity": "1200 张", "quantityValue": 1200,
              "quantityUnit": "张", "size": "90×54 mm", "paper": "250g 铜版纸",
              "printing": "双面四色", "finishing": "无特殊工艺", "binding": "无需装订"}


class PricingEngineTest(unittest.TestCase):
    def estimate(self, patch=None, **kwargs):
        return estimate_order({**CARD_ORDER, **(patch or {})}, as_of=TODAY, **kwargs)

    def test_decimal_golden_breakdown_and_provenance(self):
        # Hand-derived against rate card 2026.11.01 (合版 50 + 0.5/m², 铜版纸
        # 7.5 元/kg): area 0.00486 m², material 1.63296 kg -> 12.25, run
        # 11.664 m²·面 -> 5.83, packing 24.00; subtotal 92.08 below the 100
        # make-ready floor, so mid = 100 and the adjustment tops up 7.92;
        # low bound = max(100, 100 × 0.85) = 100.
        value = self.estimate()
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["range"], {"low": "100.00", "mid": "100.00", "high": "125.00"})
        self.assertEqual([row["amount"] for row in value["lineItems"]], ["12.25", "50.00", "5.83", "24.00", "7.92"])
        self.assertEqual(sum(Decimal(row["amount"]) for row in value["lineItems"]), Decimal(value["range"]["mid"]))
        self.assertEqual(value["basis"]["confidence"], "illustrative")
        self.assertFalse(value["basis"]["isSupplierPrice"])
        self.assertTrue(all(row["rateSource"]["sourceId"] for row in value["lineItems"]))
        json.dumps(value, allow_nan=False)

    def test_quantity_never_reduces_total_at_former_tier_boundaries(self):
        quantities = [1, 10, 499, 500, 501, 999, 1000, 1001, 4999, 5000, 5001, 20000, 20001]
        totals = [Decimal(self.estimate({"quantity": f"{qty} 张", "quantityValue": qty})["range"]["mid"]) for qty in quantities]
        self.assertEqual(totals, sorted(totals))
        self.assertGreater(totals[-1], totals[-2])

    def test_no_hidden_quantity_or_unit_assumption(self):
        for patch in ({"quantity": "", "quantityValue": None}, {"quantity": "0 张", "quantityValue": 0},
                      {"quantity": "1k张", "quantityValue": None}, {"quantity": "1200", "quantityUnit": ""}):
            with self.subTest(patch=patch):
                value = self.estimate(patch)
                self.assertEqual(value["status"], "blocked")
                self.assertIsNone(value["range"])
                self.assertIn("quantity", value["coverage"]["missingDrivers"])

    def test_dimensions_gsm_sides_and_pages_change_costs(self):
        base = self.estimate({"productType": "单页", "quantityValue": 5000, "quantity": "5000 张", "size": "210×297 mm"})
        larger = self.estimate({"productType": "单页", "quantityValue": 5000, "quantity": "5000 张", "size": "420×297 mm"})
        heavier = self.estimate({"productType": "单页", "quantityValue": 5000, "quantity": "5000 张", "size": "210×297 mm", "paper": "350g 铜版纸"})
        one_side = self.estimate({"productType": "单页", "quantityValue": 5000, "quantity": "5000 张", "size": "210×297 mm", "printing": "单面四色"})
        for value in (larger, heavier):
            self.assertGreater(Decimal(value["range"]["mid"]), Decimal(base["range"]["mid"]))
        self.assertLess(Decimal(one_side["range"]["mid"]), Decimal(base["range"]["mid"]))
        booklet = {"productType": "宣传册", "size": "210×297 mm", "quantity": "1200 本", "quantityUnit": "本", "binding": "骑马钉", "pages": "16 页"}
        self.assertGreater(Decimal(self.estimate({**booklet, "pages": "32 页"})["range"]["mid"]), Decimal(self.estimate(booklet)["range"]["mid"]))

    def test_multi_finishing_is_additive_and_minimum_is_max(self):
        value = self.estimate({"finishing": "烫金、哑膜"})
        codes = [row["code"] for row in value["lineItems"]]
        self.assertIn("finishing:烫金", codes)
        self.assertIn("finishing:哑膜", codes)
        tiny = self.estimate({"quantity": "1 张", "quantityValue": 1})
        # Hand check: material 0.01 + setup 50.00 + run 0.00 + packing 0.02
        # = 50.03, topped up to the 100 make-ready floor by a 49.97 adjustment.
        self.assertEqual(tiny["range"]["mid"], "100.00")
        self.assertEqual(tiny["range"]["low"], "100.00")
        self.assertEqual(tiny["range"]["high"], "125.00")
        self.assertEqual(sum(Decimal(row["amount"]) for row in tiny["lineItems"]), Decimal("100"))
        self.assertEqual(tiny["lineItems"][-1]["code"], "minimum_adjustment")

    def test_unknown_process_and_material_do_not_receive_generic_prices(self):
        value = self.estimate({"finishing": "纳米覆膜"})
        self.assertEqual(value["status"], "partial")
        self.assertIsNone(value["range"])
        self.assertTrue(value["lineItems"])
        self.assertIn("纳米覆膜", value["unpriced"][0])
        self.assertEqual(self.estimate({"paper": "特种纸"})["status"], "blocked")
        self.assertEqual(self.estimate({"printing": "四色印刷"})["status"], "blocked")

    def test_newly_calibrated_categories_price_and_keep_unknowns_partial(self):
        # 2026.11.01 narrowed blocked from 7 to 3 categories; the newly
        # calibrated ones must price end-to-end while genuinely unsupported
        # processes (打孔/穿绳 combos outside the card) stay partial.
        tag = self.estimate({"productType": "吊牌", "quantity": "2000 个", "quantityUnit": "个",
                             "size": "50×90 mm", "paper": "350g 白卡纸", "binding": "模切"})
        self.assertEqual(tag["status"], "ready")
        self.assertIn("binding:模切", [row["code"] for row in tag["lineItems"]])
        punched = self.estimate({"productType": "吊牌", "quantity": "2000 个", "quantityUnit": "个",
                                 "size": "50×90 mm", "paper": "350g 白卡纸", "binding": "打孔穿绳"})
        self.assertEqual(punched["status"], "partial")
        self.assertIsNone(punched["range"])
        pvc = self.estimate({"productType": "PVC卡", "quantity": "500 张", "quantityUnit": "张",
                             "size": "86×54 mm", "paper": "PVC卡基", "printing": "双面四色"})
        self.assertEqual(pvc["status"], "ready")
        envelope = self.estimate({"productType": "信封封套", "quantity": "500 个", "quantityUnit": "个",
                                  "dimensions": {"dieCutSize": "220×160 mm"}, "paper": "250g 白卡纸",
                                  "printing": "双面四色", "productSpecs": {"boxStructure": "信封"}})
        self.assertEqual(envelope["status"], "ready")
        self.assertTrue({"binding:模切", "binding:糊盒"}.issubset({row["code"] for row in envelope["lineItems"]}))
        bag = self.estimate({"productType": "手提袋", "quantity": "500 个", "quantityUnit": "个",
                             "dimensions": {"expandedSize": "600×450 mm"}, "paper": "250g 白卡纸",
                             "printing": "双面四色", "binding": "穿绳",
                             "productSpecs": {"boxStructure": "手提袋"}})
        self.assertEqual(bag["status"], "ready")
        self.assertIn("binding:穿绳", [row["code"] for row in bag["lineItems"]])
        # Schema 3.0 unblocked the last three categories: each now estimates
        # ready once its calibrated drivers are supplied.
        cup = self.estimate({"productType": "纸杯", "quantity": "10000 个", "quantityUnit": "个",
                             "paper": "280g淋膜纸", "printing": "单面四色",
                             "productSpecs": {"cupVolume": "250ml"}})
        self.assertEqual(cup["status"], "ready")
        self.assertIn("batch_setup", [row["code"] for row in cup["lineItems"]])
        form = self.estimate({"productType": "联单", "quantity": "500 本", "quantityUnit": "本",
                              "size": "190×95 mm", "paper": "40g无碳纸", "printing": "单面单色",
                              "productSpecs": {"paperParts": "三联", "numbering": "需要"}})
        self.assertEqual(form["status"], "ready")
        self.assertIn("binding:打号", [row["code"] for row in form["lineItems"]])
        board = self.estimate({"productType": "PVC", "quantity": "12 块", "quantityUnit": "块",
                               "size": "600×900 mm", "printing": "单面四色",
                               "productSpecs": {"displayMaterial": "PVC板", "boardThickness": "5mm",
                                                "install": "自行安装"}})
        self.assertEqual(board["status"], "ready")
        self.assertIn("tier_processing", [row["code"] for row in board["lineItems"]])

    def test_category_coverage_is_independent_and_packaging_requires_flat_area(self):
        box = self.estimate({"productType": "包装盒", "quantity": "1200 个", "quantityUnit": "个", "size": "600×400×200 mm", "productSpecs": {"boxStructure": "折叠盒"}})
        self.assertEqual(box["status"], "blocked")
        self.assertIn("dimensions", box["coverage"]["missingDrivers"])
        ready = self.estimate({"productType": "包装盒", "quantity": "1200 个", "quantityUnit": "个", "dimensions": {"expandedSize": "800×600 mm"}, "productSpecs": {"boxStructure": "折叠盒"}})
        self.assertEqual(ready["status"], "ready")
        self.assertTrue({"binding:模切", "binding:糊盒"}.issubset({row["code"] for row in ready["lineItems"]}))
        cup = self.estimate({"productType": "纸杯"})
        self.assertEqual(cup["status"], "blocked")
        self.assertIn("cupVolume", cup["coverage"]["missingDrivers"])
        self.assertEqual(self.estimate()["coverage"]["ratio"], 1)

    def test_structured_measurements_and_centimeters_have_same_cost(self):
        value = self.estimate({"size": "", "measurements": {"finishedSize": {"width": 90, "height": 54, "unit": "mm", "semantic": "finished"}}})
        self.assertEqual(value["range"], self.estimate({"size": "9×5.4 cm"})["range"])

    def test_rate_card_expiration_and_validation_fail_closed(self):
        card = load_rate_card()
        card["expiresAt"] = "2026-11-14"
        self.assertEqual(self.estimate(rate_card=card)["reason"], "rate_card_out_of_date")
        card = load_rate_card(); card["printing"]["合版"]["setup"] = "-1"
        with self.assertRaises(ValueError):
            validate_rate_card(card)
        card = load_rate_card(); card["confidence"] = "market_price"
        with self.assertRaises(ValueError):
            validate_rate_card(card)

    def test_different_cover_paper_is_costed_separately(self):
        base = {"productType": "画册", "quantity": "1200 本", "quantityUnit": "本", "size": "A4", "pages": "32 页", "binding": "胶装", "paper": "157g 哑粉纸"}
        plain = self.estimate(base)
        covered = self.estimate({**base, "productSpecs": {"coverPaper": "350g 白卡纸"}})
        self.assertIn("cover_material", [row["code"] for row in covered["lineItems"]])
        self.assertGreater(Decimal(covered["range"]["mid"]), Decimal(plain["range"]["mid"]))

    def test_label_and_display_have_independent_required_drivers(self):
        label = {"productType": "标签", "size": "50×50 mm", "printing": "单面四色", "productSpecs": {
            "labelMaterial": "铜版不干胶", "labelShape": "圆形", "adhesive": "普通胶", "labelFormat": "张装"}}
        value = self.estimate(label)
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["coverage"]["ratio"], 1)
        unpriced = self.estimate({**label, "productSpecs": {**label["productSpecs"], "adhesive": "可移胶"}})
        self.assertEqual(unpriced["status"], "partial")
        self.assertIsNone(unpriced["range"])
        self.assertIn("adhesive", unpriced["coverage"]["missingDrivers"])
        poster = {"productType": "海报", "size": "600×900 mm", "printing": "单面四色",
                  "productSpecs": {"displayMaterial": "背胶", "install": "墙面张贴"}}
        value = self.estimate(poster)
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["coverage"]["ratio"], 1)
        board = self.estimate({**poster, "productSpecs": {"displayMaterial": "KT板", "install": "裱板"}})
        self.assertEqual(board["status"], "blocked")
        self.assertIsNone(board["range"])

    def test_custom_dimensions_without_unit_are_never_assumed_mm(self):
        value = self.estimate({"size": "90×54"})
        self.assertEqual(value["status"], "blocked")
        self.assertIn("dimensions", value["coverage"]["missingDrivers"])

    def test_business_card_copies_are_rejected_without_guessing_sheet_count(self):
        value = self.estimate({"quantity": "1200 份", "quantityUnit": "份"})
        self.assertEqual(value["status"], "blocked")
        self.assertIsNone(value["range"])
        self.assertEqual(value["reason"], "quantity_unit_incompatible")
        self.assertEqual(value["quantityValidation"], {"receivedUnit": "份", "acceptedUnits": ["张"], "converted": False})
        self.assertIn("名片数量单位须为张", "；".join(value["missing"]))

    def test_recommendations_and_price_bases_use_canonical_units(self):
        from tools import recommend_processes
        options = recommend_processes({**CARD_ORDER, "productType": "单页", "size": "A4"})
        self.assertIn("210×297 mm", options[0]["description"])
        for option in options:
            self.assertIn("g/m²", option["paper"])
            self.assertNotRegex(option["description"], r"\d+g(?:\s|$)")
            material_rows = [row for row in option["estimate"]["lineItems"] if row["code"] == "material"]
            self.assertEqual(len(material_rows), 1)
            self.assertIn("g/m²", material_rows[0]["basis"])

    def test_ambiguous_or_additional_inks_never_receive_a_complete_estimate(self):
        for printing in ("单面四色/双面四色", "不要双面四色，单面黑白", "双面四色+白墨"):
            with self.subTest(printing=printing):
                value = self.estimate({"printing": printing})
                self.assertEqual(value["status"], "blocked")
                self.assertIsNone(value["range"])
                self.assertIn("printing", value["coverage"]["missingDrivers"])
        for color in ("专色", "黑白"):
            value = self.estimate({"productSpecs": {"cardColor": color}})
            self.assertEqual(value["status"], "blocked")
            self.assertIsNone(value["range"])
        self.assertEqual(self.estimate({"printing": "双面 四色印刷"})["status"], "ready")
        booklet = self.estimate({"productType": "宣传册", "quantity": "1200 本", "quantityUnit": "本",
                                 "pages": "32 页", "binding": "圆角"})
        self.assertEqual(booklet["status"], "partial")
        self.assertIsNone(booklet["range"])
        self.assertIn("binding", booklet["coverage"]["missingDrivers"])

    def test_rate_card_rejects_incomplete_or_untraceable_parameters(self):
        for mutation in (
            lambda card: card["printing"]["合版"].pop("perM2"),
            lambda card: card["materials"]["铜版纸"].update(sourceId="nonexistent"),
            lambda card: card["categories"]["名片"].update(wasteFactor="0.9"),
            lambda card: card.update(effectiveFrom=None),
            lambda card: card.update(isSupplierPrice=True),
            lambda card: card.update(taxBasis="inclusive"),
        ):
            card = load_rate_card()
            mutation(card)
            with self.subTest(card=card["rateCardId"]), self.assertRaises(ValueError):
                validate_rate_card(card)


class CalibrationTest(unittest.TestCase):
    def row(self, number, **patch):
        row = dict(zip(REQUIRED_COLUMNS, ["none"] * len(REQUIRED_COLUMNS)))
        row.update({"quote_id": f"q{number}", "source_id": "authorized-example", "source_description": "授权脱敏样例",
                    "quote_date": "2026-10-01", "valid_until": "2026-12-31", "product": "名片", "width_mm": "90", "height_mm": "54",
                    "dimension_semantic": "finished", "quantity": "1000", "quantity_unit": "张", "material": "铜版纸", "gsm": "250",
                    "pages": "0", "printing": "四色", "sides": "2", "print_mode": "合版", "product_specs": "{}",
                    "region": "CN-SH", "lead_workdays": "5", "currency": "CNY", "total_amount": "113", "tax_basis": "inclusive",
                    "tax_rate": "0.13", "delivery_basis": "ex_works", "shipping_net_amount": "0"})
        row.update(patch)
        return row

    def run_calibration(self, rows, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quotes.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS); writer.writeheader(); writer.writerows(rows)
            return calibrate(path, as_of=TODAY, min_samples=3, **kwargs)

    def test_comparable_quotes_only_and_tax_delivery_normalization(self):
        rows = [self.row(1), self.row(2, total_amount="124.30", delivery_basis="delivered", shipping_net_amount="10"),
                self.row(3, total_amount="100", tax_basis="exclusive"), self.row(4, width_mm="100")]
        result = self.run_calibration(rows)
        eligible = next(bucket for bucket in result["buckets"] if bucket["status"] == "review_required")
        self.assertEqual(eligible["sampleCount"], 3)
        self.assertEqual(eligible["statistics"], {"p25": "100.00", "median": "100.00", "p75": "100.00"})
        self.assertEqual(sum(bucket["statistics"] is None for bucket in result["buckets"]), 1)
        self.assertFalse(result["published"])

    def test_expired_duplicate_bad_units_and_missing_source_are_excluded(self):
        rows = [self.row(1), self.row(1), self.row(2, valid_until="2026-11-14"),
                self.row(3, quantity_unit="kg"), self.row(4, source_id=""), self.row(5, quote_date="2026-11-16")]
        result = self.run_calibration(rows)
        self.assertEqual(len(result["rejectedRows"]), 4)
        self.assertEqual(result["expiredQuoteIds"], ["q2"])
        self.assertIsNone(result["buckets"][0]["statistics"])

    def test_offline_cli_writes_only_a_draft_and_rejects_active_rate_directory(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / "tools/calibrate_pricing.py"), "missing.csv", "--output", str(root / "rate_cards/draft.json")],
                                capture_output=True, text=True, encoding="utf-8",
                                env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("活动 rate_cards", result.stderr)


def synthetic_card() -> dict:
    """A minimal schema-3.0 card exercising the cup/form/board components."""
    return {
        "schemaVersion": "3.0", "rateCardId": "synthetic-schema3", "version": "2026.12.01",
        "region": "CN-reference-only", "currency": "CNY", "unitSystem": "mm",
        "sourceType": "local_reference", "sourceDescription": "单测合成成本表，仅验证计算方法",
        "effectiveFrom": "2026-01-01", "expiresAt": "2027-12-31",
        "confidence": "illustrative", "enteredBy": "test", "taxBasis": "exclusive",
        "deliveryBasis": "ex_works", "isSupplierPrice": False,
        "sources": {"synthetic-source": {"description": "合成来源", "enteredBy": "test"}},
        "band": {"low": "0.85", "high": "1.25"},
        "materials": {"淋膜纸": {"sourceId": "synthetic-source", "perKg": "8"},
                      "无碳纸": {"sourceId": "synthetic-source", "perKg": "6"},
                      "PVC板": {"sourceId": "synthetic-source", "perM2": "18"}},
        "printing": {"合版": {"sourceId": "synthetic-source", "setup": "50", "perM2": "0.5"}},
        "finishing": {},
        "binding": {"打号": {"sourceId": "synthetic-source", "setup": "30", "perUnit": "0.02"}},
        "colorFactors": {"四色": "1", "单色": "0.65"},
        "categories": {
            "纸杯": {"supported": True, "family": "cup",
                      "requiredDrivers": ["quantity", "cupVolume", "material", "printing"],
                      "minimumTotal": "300", "packingPerUnit": "0.02", "wasteFactor": "1.1",
                      "sourceId": "synthetic-source", "units": ["个"],
                      "cupSizes": {"250ml": {"blankAreaM2": "0.0132"}},
                      "batchSetup": {"amount": "120", "sourceId": "synthetic-source"}},
            "联单": {"supported": True, "family": "form",
                      "requiredDrivers": ["quantity", "dimensions", "paperParts", "material", "printing"],
                      "minimumTotal": "50", "packingPerUnit": "0.01", "wasteFactor": "1.1",
                      "sourceId": "synthetic-source", "units": ["本", "份"],
                      "paperParts": {"二联": 2, "三联": 3, "四联": 4},
                      "imposition": {"perSheetUp": 4, "sheetWidthMm": "600", "sheetHeightMm": "400",
                                     "sheetName": "无碳纸大纸", "sourceId": "synthetic-source"}},
            "PVC": {"supported": True, "family": "display",
                    "requiredDrivers": ["quantity", "dimensions", "boardThickness", "material", "printing", "install"],
                    "minimumTotal": "100", "packingPerUnit": "0.02", "wasteFactor": "1.05",
                    "sourceId": "synthetic-source", "units": ["块"],
                    "boardThicknesses": {"5mm": {"materialFactor": "1"}, "10mm": {"materialFactor": "1.8"}},
                    "quantityTiers": [{"upTo": 49, "perUnit": "0.35"}, {"upTo": None, "perUnit": "0.2"}]},
        },
    }


class PricingSchema3ComponentTest(unittest.TestCase):
    def test_cup_family_prices_capacity_band_and_batch_setup(self):
        order = {"productType": "纸杯", "quantityValue": 10000, "quantityUnit": "个",
                 "paper": "280g淋膜纸", "printing": "单面四色", "finishing": "无", "printMode": "合版",
                 "productSpecs": {"cupVolume": "250ml"}}
        result = estimate_order(order, rate_card=synthetic_card(), as_of=TODAY)
        self.assertEqual(result["status"], "ready")
        amounts = {row["code"]: row["amount"] for row in result["lineItems"]}
        # 10000 × 0.0132 m² × 损耗 1.1 × 280 g/m² = 40.656 kg × 8 = 325.25
        self.assertEqual(amounts, {"material": "325.25", "print_setup": "50.00", "print_run": "66.00",
                                   "batch_setup": "120.00", "packing": "200.00"})
        self.assertEqual(result["range"], {"low": "647.06", "mid": "761.25", "high": "951.56"})
        self.assertEqual(result["drivers"]["cupVolume"], "250ml")
        self.assertNotIn("widthMm", result["drivers"])

    def test_cup_without_calibrated_capacity_is_blocked_not_inferred(self):
        order = {"productType": "纸杯", "quantityValue": 10000, "quantityUnit": "个",
                 "paper": "280g淋膜纸", "printing": "单面四色", "printMode": "合版"}
        result = estimate_order(order, rate_card=synthetic_card(), as_of=TODAY)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("纸杯容量" in item for item in result["missing"]))

    def test_form_family_imposes_large_sheets_and_skips_waste(self):
        order = {"productType": "联单", "quantityValue": 500, "quantityUnit": "本",
                 "size": "190×95 mm", "paper": "40g无碳纸", "printing": "单面单色",
                 "finishing": "无", "binding": "打号", "printMode": "合版",
                 "productSpecs": {"paperParts": "三联"}}
        result = estimate_order(order, rate_card=synthetic_card(), as_of=TODAY)
        self.assertEqual(result["status"], "ready")
        amounts = {row["code"]: row["amount"] for row in result["lineItems"]}
        # ceil(500 × 3联 / 每张4部分) = 375 张大纸 × 0.24 m² × 40 g/m² = 3.6 kg × 6 = 21.60（整张开料不再乘损耗）
        self.assertEqual(amounts["material"], "21.60")
        self.assertEqual(amounts["print_run"], "8.80")
        self.assertEqual(amounts["binding:打号"], "40.00")
        self.assertEqual(result["range"], {"low": "106.59", "mid": "125.40", "high": "156.75"})
        self.assertEqual(result["drivers"]["sheetsPerUnit"], "3")
        self.assertTrue(any("整张大纸开料" in item for item in result["assumptions"]))
        material_basis = next(row["basis"] for row in result["lineItems"] if row["code"] == "material")
        self.assertIn("共 375 张", material_basis)

    def test_form_without_calibrated_part_count_is_blocked(self):
        order = {"productType": "联单", "quantityValue": 500, "quantityUnit": "本",
                 "size": "190×95 mm", "paper": "40g无碳纸", "printing": "单面单色", "printMode": "合版",
                 "productSpecs": {"paperParts": "5联"}}
        result = estimate_order(order, rate_card=synthetic_card(), as_of=TODAY)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("联单联数", result["missing"])

    def test_display_board_thickness_factor_and_quantity_tier(self):
        order = {"productType": "PVC", "quantityValue": 12, "quantityUnit": "块",
                 "size": "600×900 mm", "printing": "单面四色", "finishing": "无", "printMode": "合版",
                 "productSpecs": {"displayMaterial": "PVC板", "boardThickness": "10mm", "install": "自行安装"}}
        result = estimate_order(order, rate_card=synthetic_card(), as_of=TODAY)
        self.assertEqual(result["status"], "ready")
        amounts = {row["code"]: row["amount"] for row in result["lineItems"]}
        # 12 × 0.54 m² × 损耗 1.05 × 板厚系数 1.8 = 12.2472 m² × 18 = 220.45
        self.assertEqual(amounts["material"], "220.45")
        # 数量 12 落入 ≤49 档：12 × 0.35 = 4.20
        self.assertEqual(amounts["tier_processing"], "4.20")
        self.assertEqual(result["range"]["mid"], "278.13")

    def test_quantity_tiers_stay_monotonic_across_boundaries(self):
        base = {"productType": "PVC", "quantityUnit": "块", "size": "600×900 mm",
                "printing": "单面四色", "finishing": "无", "printMode": "合版",
                "productSpecs": {"displayMaterial": "PVC板", "boardThickness": "5mm", "install": "自行安装"}}
        totals = []
        for count in (49, 50, 199, 200):
            result = estimate_order({**base, "quantityValue": count}, rate_card=synthetic_card(), as_of=TODAY)
            self.assertEqual(result["status"], "ready")
            totals.append(Decimal(result["range"]["mid"]))
        self.assertEqual(totals, sorted(totals))
        tier_basis = next(row["basis"] for row in
                          estimate_order({**base, "quantityValue": 200}, rate_card=synthetic_card(), as_of=TODAY)["lineItems"]
                          if row["code"] == "tier_processing")
        self.assertIn("数量阶梯累进", tier_basis)

    def test_uncalibrated_category_carries_card_reason_detail(self):
        card = synthetic_card()
        card["categories"]["烫画"] = {"supported": False, "family": "uncalibrated",
                                      "requiredDrivers": ["categoryRates"],
                                      "reason": "烫画工艺的介质与转印驱动未标定",
                                      "sourceId": "synthetic-source"}
        result = estimate_order({"productType": "烫画"}, rate_card=card, as_of=TODAY)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "category_uncalibrated")
        self.assertEqual(result["reasonDetail"], "烫画工艺的介质与转印驱动未标定")
        # A category the card does not list at all has nothing to quote.
        unknown = estimate_order({"productType": "不存在的品类"}, rate_card=card, as_of=TODAY)
        self.assertNotIn("reasonDetail", unknown)

    def test_schema3_rejects_broken_component_declarations(self):
        card = synthetic_card()
        validate_rate_card(card)
        cases = [
            {**card, "schemaVersion": "2.0"},
            {**card, "categories": {**card["categories"], "纸杯": {**card["categories"]["纸杯"], "cupSizes": {}}}},
            {**card, "categories": {**card["categories"], "联单": {**card["categories"]["联单"], "paperParts": {}}}},
            {**card, "categories": {**card["categories"], "联单": {**card["categories"]["联单"],
                                                                   "paperParts": {"二联": 1}}}},
            {**card, "categories": {**card["categories"], "联单": {**card["categories"]["联单"],
                                                                   "family": "booklet"}}},
            {**card, "categories": {**card["categories"], "PVC": {**card["categories"]["PVC"],
                                                                  "quantityTiers": [{"upTo": None, "perUnit": "0.2"},
                                                                                    {"upTo": 49, "perUnit": "0.35"}]}}},
            {**card, "categories": {**card["categories"], "PVC": {**card["categories"]["PVC"],
                                                                  "quantityTiers": [{"upTo": 49, "perUnit": "0.35"},
                                                                                    {"upTo": 10, "perUnit": "0.2"},
                                                                                    {"upTo": None, "perUnit": "0.1"}]}}},
            {**card, "categories": {**card["categories"], "PVC": {**card["categories"]["PVC"],
                                                                  "imposition": {"perSheetUp": 0, "sheetWidthMm": "600",
                                                                                 "sheetHeightMm": "400",
                                                                                 "sheetName": "大纸",
                                                                                 "sourceId": "synthetic-source"}}}},
        ]
        for index, broken in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    validate_rate_card(broken)


if __name__ == "__main__":
    unittest.main()
