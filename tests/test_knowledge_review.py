"""Knowledge review cadence and coverage alignment, all dates injected."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from pricing_engine import load_rate_card
from product_knowledge import (KNOWLEDGE_MANIFEST, PRODUCT_CATALOG,
                               knowledge_review_status)

ROOT = Path(__file__).resolve().parents[1]
REVIEW_TOOL = ROOT / "tools" / "knowledge_review.py"
LAST_REVIEWED = date.fromisoformat(KNOWLEDGE_MANIFEST["lastReviewed"])
CADENCE = int(KNOWLEDGE_MANIFEST["reviewCadenceDays"])
GRACE = 30


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(REVIEW_TOOL), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(ROOT), env=env)


class KnowledgeReviewStatusTest(unittest.TestCase):
    def test_status_boundaries_ok_due_overdue(self):
        # ok strictly before the cadence, due at the cadence, overdue only
        # after the 30-day grace period beyond it.
        self.assertEqual(knowledge_review_status(LAST_REVIEWED)["status"], "ok")
        ok = knowledge_review_status(LAST_REVIEWED + timedelta(days=CADENCE - 1))
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["daysSinceReview"], CADENCE - 1)
        due = knowledge_review_status(LAST_REVIEWED + timedelta(days=CADENCE))
        self.assertEqual(due["status"], "due")
        still_due = knowledge_review_status(
            LAST_REVIEWED + timedelta(days=CADENCE + GRACE - 1))
        self.assertEqual(still_due["status"], "due")
        overdue = knowledge_review_status(
            LAST_REVIEWED + timedelta(days=CADENCE + GRACE))
        self.assertEqual(overdue["status"], "overdue")
        self.assertEqual(overdue["daysSinceReview"], CADENCE + GRACE)

    def test_next_review_date_matches_manifest_schedule(self):
        # 2026-09-05 + 90 days must be 2026-12-04, and the manifest's declared
        # value must agree with the derivation.
        self.assertEqual(str(LAST_REVIEWED), "2026-09-05")
        self.assertEqual(CADENCE, 90)
        status = knowledge_review_status(date(2026, 11, 15))
        self.assertEqual(status["nextReviewAt"], "2026-12-04")
        self.assertEqual(status["declaredNextReviewAt"], status["nextReviewAt"])
        self.assertEqual(status["knowledgeVersion"], KNOWLEDGE_MANIFEST["version"])

    def test_rate_card_margin_is_tracked_and_today_is_injectable(self):
        card = load_rate_card()
        today = date(2026, 11, 15)
        status = knowledge_review_status(today)
        expected = (date.fromisoformat(card["expiresAt"]) - today).days
        self.assertEqual(status["rateCard"]["status"], "tracked")
        self.assertEqual(status["rateCard"]["daysUntilExpiry"], expected)
        self.assertEqual(status["rateCard"]["expiresAt"], card["expiresAt"])
        self.assertEqual(status["rateCard"]["version"], card["version"])

    def test_catalog_and_rate_card_categories_are_aligned(self):
        # 邀请函 existed only in the rate card and the NLU fallback; this
        # round added it to PRODUCT_CATALOG so the差集 must be empty.
        card = load_rate_card()
        self.assertIn("邀请函", PRODUCT_CATALOG)
        self.assertEqual(set(PRODUCT_CATALOG), set(card["categories"]))
        self.assertTrue(PRODUCT_CATALOG["邀请函"]["aliases"])
        # The sample-pricing knowledge source must point at the active rate
        # card source id so the manifest stays traceable to the card.
        self.assertIn("industry-standard-flow-2026-10", KNOWLEDGE_MANIFEST["sources"][2]["scope"])


class KnowledgeReviewCliTest(unittest.TestCase):
    def test_happy_path_exits_zero_with_json_report(self):
        result = run_cli("--today", "2026-11-15", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["coverageGap"], {"catalogOnly": [], "rateCardOnly": []})
        self.assertEqual(report["knowledge"]["status"], "ok")
        self.assertTrue(report["rateCard"]["readable"])

    def test_review_due_and_overdue_warn_without_failing(self):
        due_day = (LAST_REVIEWED + timedelta(days=CADENCE + 6)).isoformat()
        result = run_cli("--today", due_day)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[警告]", result.stdout)
        self.assertIn("复核期", result.stdout)
        overdue_day = (LAST_REVIEWED + timedelta(days=CADENCE + GRACE + 10)).isoformat()
        result = run_cli("--today", overdue_day, "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("逾期", json.loads(result.stdout)["warnings"][0])

    def test_unreadable_rate_card_is_structural_and_exits_one(self):
        result = run_cli("--today", "2026-11-15",
                         "--rate-card", str(ROOT / "rate_cards" / "missing-file.json"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("rate card 不可读", result.stdout)

    def test_coverage_gap_and_missing_source_description_exit_one(self):
        card = load_rate_card()
        del card["categories"]["邀请函"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gap-card.json"
            path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
            result = run_cli("--today", "2026-11-15", "--rate-card", str(path))
            self.assertEqual(result.returncode, 1)
            self.assertIn("差集", result.stdout)
            self.assertIn("邀请函", result.stdout)

            card = load_rate_card()
            source_id = next(iter(card["sources"]))
            card["sources"][source_id]["description"] = ""
            path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
            result = run_cli("--today", "2026-11-15", "--rate-card", str(path))
            self.assertEqual(result.returncode, 1)
            self.assertIn("校验失败", result.stdout)

    def test_invalid_injected_date_is_a_usage_error(self):
        result = run_cli("--today", "not-a-date")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
