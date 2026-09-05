"""End-to-end checks for the real desensitized-order corpus pipeline.

The real cases themselves arrive from the team (see docs/RELEASE_CHECKLIST.md,
门槛 4); these tests make sure that the moment they are dropped into
tests/eval_cases_real.json, the loader, the suite runner and the ≥95% gate
behave exactly as documented — using temporary fixtures, never fake "real"
data committed to the repository.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import evaluate_agent

SYNTHETIC_REPORT = {
    "cases": 0, "passedCases": 0, "completionRate": 100, "expectedCompleteCases": 0,
    "fieldAccuracy": 100, "averageTurnsToReady": None, "averageResponseMs": None,
    "totalTurns": 0, "tagReport": {}, "results": [],
}

GOOD_CASE = {"name": "真实-名片", "tag": "真实",
             "turns": [{"text": "做 500 张 A4 名片，250g铜版纸，双面四色，下周内"}],
             "expected": {"productType": "名片", "quantity": "500 张", "size": "A4"}}
BAD_CASE = {"name": "真实-错例", "tag": "真实",
            "turns": [{"text": "做 500 张 A4 名片，250g铜版纸，双面四色，下周内"}],
            "expected": {"productType": "包装盒"}}


class RealCorpusLoaderTest(unittest.TestCase):
    def load(self, content: str | None, present: bool = True):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "eval_cases_real.json"
            if present:
                path.write_text(content if content is not None else "", encoding="utf-8")
            with mock.patch.object(evaluate_agent, "REAL_CORPUS_PATH", path):
                return evaluate_agent.load_real_cases()

    def test_missing_file_reports_missing(self):
        cases, status = self.load(None, present=False)
        self.assertEqual((cases, status), ([], "missing"))

    def test_empty_cases_report_empty(self):
        cases, status = self.load('{"cases": []}')
        self.assertEqual((cases, status), ([], "empty"))

    def test_invalid_json_and_shape_report_invalid(self):
        self.assertEqual(self.load("{not json")[1], "invalid")
        self.assertEqual(self.load('{"cases": "twenty"}')[1], "invalid")
        self.assertEqual(self.load("[1, 2, 3]")[1], "invalid")

    def test_wellformed_cases_are_kept_and_junk_filtered(self):
        good = dict(GOOD_CASE)
        junk = {"name": "坏例", "turns": [], "expected": {}}
        cases, status = self.load(json.dumps({"cases": [good, junk]}, ensure_ascii=False))
        self.assertEqual(status, "ok")
        self.assertEqual([case["name"] for case in cases], ["真实-名片"])


class RealCorpusSuiteTest(unittest.TestCase):
    def test_real_format_cases_run_through_the_suite(self):
        cases = [GOOD_CASE,
                 {"name": "真实-折页修改", "tag": "真实",
                  "turns": [{"text": "做 1000 张 A4 三折页，157g哑粉纸，双面四色，下周"},
                            {"text": "数量改成 2000 张"}],
                  "expected": {"productType": "折页", "quantity": "2000 张",
                               "productSpecs.folding": "三折"}}]
        report = evaluate_agent.run_suite(cases)
        self.assertEqual(report["cases"], 2)
        self.assertEqual(report["passedCases"], 2)
        self.assertEqual(report["fieldAccuracy"], 100)
        self.assertEqual(set(report["tagReport"]), {"真实"})


class RealCorpusGateTest(unittest.TestCase):
    def test_all_four_verdicts(self):
        v = evaluate_agent.real_corpus_verdict
        self.assertEqual(v([], None, "missing"), "pending")
        self.assertEqual(v([], None, "empty"), "pending")
        self.assertEqual(v([], None, "invalid"), "fail")

        few = [{"case": i} for i in range(5)]
        self.assertEqual(v(few, {"cases": 5, "fieldAccuracy": 50}, "ok"), "record")

        enough = [{"case": i} for i in range(evaluate_agent.REAL_CORPUS_MIN_CASES)]
        self.assertEqual(v(enough, {"cases": 20, "fieldAccuracy": 94.9}, "ok"), "fail")
        self.assertEqual(v(enough, {"cases": 20, "fieldAccuracy": 95}, "ok"), "pass")
        self.assertEqual(v(enough, {"cases": 20, "fieldAccuracy": 100}, "ok"), "pass")


class RealCorpusMainExitCodeTest(unittest.TestCase):
    def run_main(self, corpus_payload: str):
        """Run main() with the slow synthetic suite stubbed to a perfect report."""
        real_run_suite = evaluate_agent.run_suite

        def stub_suite(cases=None):
            if cases is None:
                return dict(SYNTHETIC_REPORT)
            return real_run_suite(cases)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "eval_cases_real.json"
            path.write_text(corpus_payload, encoding="utf-8")
            with mock.patch.object(evaluate_agent, "REAL_CORPUS_PATH", path), \
                 mock.patch.object(evaluate_agent, "run_suite", stub_suite), \
                 mock.patch.object(evaluate_agent.sys, "argv", ["evaluate_agent.py"]):
                return evaluate_agent.main()

    def test_empty_corpus_keeps_synthetic_only_gate(self):
        self.assertEqual(self.run_main('{"cases": []}'), 0)

    def test_invalid_corpus_fails_the_build(self):
        self.assertEqual(self.run_main('{"cases": "oops"}'), 1)

    def test_gated_corpus_below_threshold_fails_the_build(self):
        # 16 例全对（3 字段/例）+ 4 例判错品类：48/52 ≈ 92.3% < 95% 门槛。
        payload = {"cases": [dict(GOOD_CASE) for _ in range(16)] + [dict(BAD_CASE) for _ in range(4)]}
        self.assertEqual(self.run_main(json.dumps(payload, ensure_ascii=False)), 1)

    def test_gated_corpus_meeting_threshold_passes(self):
        payload = {"cases": [dict(GOOD_CASE) for _ in range(evaluate_agent.REAL_CORPUS_MIN_CASES)]}
        self.assertEqual(self.run_main(json.dumps(payload, ensure_ascii=False)), 0)


if __name__ == "__main__":
    unittest.main()
