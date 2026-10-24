"""Independent hand-computed examples are stable against pricing refactors."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import unittest

from pricing_engine import estimate_order, load_rate_card


class PricingWorkedExamplesTest(unittest.TestCase):
    def test_documented_examples_match_each_line_and_range(self):
        path = Path(__file__).resolve().parents[1] / "docs/examples/pricing-worked-examples.json"
        examples = json.loads(path.read_text(encoding="utf-8"))
        card = load_rate_card()
        self.assertEqual(examples["version"], card["version"])
        self.assertFalse(examples["isMarketData"])
        for case in examples["cases"]:
            with self.subTest(case=case["id"]):
                result = estimate_order(case["order"], rate_card=card, as_of=date.fromisoformat(examples["asOf"]))
                expected = case["expected"]
                self.assertEqual(result["status"], expected["status"])
                self.assertEqual({row["code"]: row["amount"] for row in result["lineItems"]}, expected["lineItems"])
                self.assertEqual(result["range"], expected["range"])
                if "knownSubtotal" in expected:
                    self.assertEqual(result["knownSubtotal"], expected["knownSubtotal"])
                self.assertFalse(result["basis"]["isSupplierPrice"])
                if result["status"] == "partial":
                    self.assertTrue(result["unpriced"])
                if result["status"] == "blocked":
                    self.assertIn("material", result["coverage"]["missingDrivers"])

    def test_booklet_manual_material_mass_and_total(self):
        # Independent arithmetic against rate card 2026.11.01 (铜版纸 7.5
        # 元/kg, 白卡纸 7 元/kg, 合版 50 + 0.5 元/m²·面, 哑膜 20 + 1.1 元/m²,
        # 骑马钉 30 + 0.10 元/本): 16 pages / 2 sides = 8 finished-size
        # leaves, of which 2 cover leaves use 250 g/m² and 6 use 157 g/m².
        d = Decimal
        q = lambda value: value.quantize(d("0.01"), rounding=ROUND_HALF_UP)
        area = d(148) * d(210) / d(1000000)
        body_kg = d(500) * area * d(6) * d("1.12") * d(157) / d(1000)
        cover_kg = d(500) * area * d(2) * d("1.12") * d(250) / d(1000)
        self.assertEqual(body_kg, d("16.3953216"))
        self.assertEqual(cover_kg, d("8.7024"))
        subtotal = sum([q(body_kg*d("7.5")), q(cover_kg*d("7")), d(50),
                        q(d(500)*area*d(16)*d("0.5")),
                        q(d(20)+d(500)*area*d(2)*d("1.1")), d(30)+d(500)*d("0.1"), d(500)*d("0.02")])
        self.assertEqual(subtotal, d("502.39"))


if __name__ == "__main__":
    unittest.main()
