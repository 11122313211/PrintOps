from __future__ import annotations

import csv
import importlib.util
import json
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


TODAY = date(2026, 10, 15)
CARD_ORDER = {"productType": "名片", "quantity": "1200 张", "quantityValue": 1200,
              "quantityUnit": "张", "size": "90×54 mm", "paper": "250g 铜版纸",
              "printing": "双面四色", "finishing": "无特殊工艺", "binding": "无需装订"}


class PricingEngineTest(unittest.TestCase):
    def estimate(self, patch=None, **kwargs):
        return estimate_order({**CARD_ORDER, **(patch or {})}, as_of=TODAY, **kwargs)

    def test_decimal_golden_breakdown_and_provenance(self):
        value = self.estimate()
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["range"], {"low": "100.00", "mid": "103.48", "high": "129.35"})
        self.assertEqual([row["amount"] for row in value["lineItems"]], ["13.06", "60.00", "6.42", "24.00"])
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
        self.assertEqual(tiny["range"]["mid"], "100.00")
        self.assertEqual(tiny["range"]["low"], "100.00")
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

    def test_category_coverage_is_independent_and_packaging_requires_flat_area(self):
        box = self.estimate({"productType": "包装盒", "quantity": "1200 个", "quantityUnit": "个", "size": "600×400×200 mm", "productSpecs": {"boxStructure": "折叠盒"}})
        self.assertEqual(box["status"], "blocked")
        self.assertIn("dimensions", box["coverage"]["missingDrivers"])
        ready = self.estimate({"productType": "包装盒", "quantity": "1200 个", "quantityUnit": "个", "dimensions": {"expandedSize": "800×600 mm"}, "productSpecs": {"boxStructure": "折叠盒"}})
        self.assertEqual(ready["status"], "ready")
        self.assertTrue({"binding:模切", "binding:糊盒"}.issubset({row["code"] for row in ready["lineItems"]}))
        cup = self.estimate({"productType": "纸杯"})
        self.assertEqual(cup["reason"], "category_uncalibrated")
        self.assertEqual(cup["coverage"]["ratio"], 0)
        self.assertEqual(self.estimate()["coverage"]["ratio"], 1)

    def test_structured_measurements_and_centimeters_have_same_cost(self):
        value = self.estimate({"size": "", "measurements": {"finishedSize": {"width": 90, "height": 54, "unit": "mm", "semantic": "finished"}}})
        self.assertEqual(value["range"], self.estimate({"size": "9×5.4 cm"})["range"])

    def test_rate_card_expiration_and_validation_fail_closed(self):
        card = load_rate_card()
        card["expiresAt"] = "2026-10-14"
        self.assertEqual(self.estimate(rate_card=card)["reason"], "rate_card_out_of_date")
        card = load_rate_card(); card["printing"]["合版"]["setup"] = "-1"
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
        rows = [self.row(1), self.row(1), self.row(2, valid_until="2026-10-14"),
                self.row(3, quantity_unit="kg"), self.row(4, source_id=""), self.row(5, quote_date="2026-10-16")]
        result = self.run_calibration(rows)
        self.assertEqual(len(result["rejectedRows"]), 4)
        self.assertEqual(result["expiredQuoteIds"], ["q2"])
        self.assertIsNone(result["buckets"][0]["statistics"])

    def test_offline_cli_writes_only_a_draft_and_rejects_active_rate_directory(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / "tools/calibrate_pricing.py"), "missing.csv", "--output", str(root / "rate_cards/draft.json")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("活动 rate_cards", result.stderr)


if __name__ == "__main__":
    unittest.main()
