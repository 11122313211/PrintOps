"""End-to-end checks for the real desensitized-order corpus pipeline.

The real cases themselves arrive from the team (see docs/RELEASE_CHECKLIST.md,
门槛 4); these tests make sure that the moment they are dropped into
tests/eval_cases_real.json, the loader, the suite runner and the ≥95% gate
behave exactly as documented — using temporary fixtures, never fake "real"
data committed to the repository.
"""

from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout

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
             "expected": {"productType": "名片", "quantity": "500 张", "size": '210×297 mm'}}
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
        self.assertEqual(v(enough, None, "ok"), "fail")


class RealCorpusMainExitCodeTest(unittest.TestCase):
    def run_main(self, corpus_payload: str | None, *flags: str, synthetic_patch=None):
        """Run main() with the slow synthetic suite stubbed to a perfect report."""
        real_run_suite = evaluate_agent.run_suite

        def stub_suite(cases=None):
            if cases is None:
                return {**SYNTHETIC_REPORT, **(synthetic_patch or {})}
            return real_run_suite(cases)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "eval_cases_real.json"
            if corpus_payload is not None:
                path.write_text(corpus_payload, encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(evaluate_agent, "REAL_CORPUS_PATH", path), \
                 mock.patch.object(evaluate_agent, "run_suite", stub_suite), \
                 mock.patch.object(evaluate_agent.sys, "argv", ["evaluate_agent.py", *flags]), \
                 redirect_stdout(output):
                result = evaluate_agent.main()
            self.last_output = output.getvalue()
            return result

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

    def test_release_gate_blocks_missing_empty_invalid_and_insufficient_corpora(self):
        insufficient = json.dumps({"cases": [dict(GOOD_CASE) for _ in range(19)]}, ensure_ascii=False)
        for payload in (None, '{"cases": []}', '{"cases": "oops"}', insufficient):
            with self.subTest(payload=payload):
                self.assertEqual(self.run_main(payload, "--release-gate", "--json"), 1)
                report = json.loads(self.last_output)
                self.assertTrue(report["releaseGateEnabled"])
                self.assertFalse(report["releaseGatePassed"])

    def test_release_gate_requires_real_accuracy_and_synthetic_regression_to_pass(self):
        good = json.dumps({"cases": [dict(GOOD_CASE) for _ in range(20)]}, ensure_ascii=False)
        bad = json.dumps({"cases": [dict(GOOD_CASE) for _ in range(16)] + [dict(BAD_CASE) for _ in range(4)]}, ensure_ascii=False)
        self.assertEqual(self.run_main(bad, "--release-gate"), 1)
        self.assertEqual(self.run_main(good, "--release-gate", synthetic_patch={"fieldAccuracy": 89.9}), 1)
        self.assertEqual(self.run_main(good, "--release-gate", synthetic_patch={"completionRate": 79.9}), 1)
        self.assertEqual(self.run_main(good, "--release-gate", "--json"), 0)
        self.assertTrue(json.loads(self.last_output)["releaseGatePassed"])


class MarketReferenceVerdictTest(unittest.TestCase):
    """参考版门槛的 rate card 来源检查（2026-11-21 两档拆分）。"""

    def verdict(self, payload: str | None, present: bool = True):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cn-local-reference.json"
            if present:
                path.write_text(payload if payload is not None else "", encoding="utf-8")
            with mock.patch.object(evaluate_agent, "RATE_CARD_PATH", path):
                return evaluate_agent.market_reference_verdict(path)

    def test_missing_invalid_and_empty_sources_fail(self):
        self.assertEqual(self.verdict(None, present=False), (False, "rate card 文件缺失"))
        self.assertEqual(self.verdict("{not json")[0], False)
        self.assertEqual(self.verdict('{"rates": {}}')[0], False)
        self.assertEqual(self.verdict('{"sources": {}}')[0], False)

    def test_market_listed_source_passes_by_confidence_and_keyword(self):
        by_confidence = json.dumps({"sources": {
            "sd2000-listed-2026-11-21": {"confidence": "market-listed", "isSupplierPrice": False}}})
        self.assertTrue(self.verdict(by_confidence)[0])
        by_keyword = json.dumps({"sources": {
            "sd2000-listed-2026-11-21": {
                "description": "公开挂牌参考价：自助网络下单价，不含运费",
                "confidence": "illustrative", "isSupplierPrice": False}}}, ensure_ascii=False)
        ok, detail = self.verdict(by_keyword)
        self.assertTrue(ok)
        self.assertIn("sd2000-listed-2026-11-21", detail)

    def test_illustrative_only_card_fails_with_reason(self):
        card = json.dumps({"sources": {
            "illustrative-cost-design-2026-10": {"confidence": "illustrative"}}},
            ensure_ascii=False)
        ok, detail = self.verdict(card)
        self.assertFalse(ok)
        self.assertIn("公开挂牌参考价来源", detail)


class ReferenceTierMainExitCodeTest(unittest.TestCase):
    def run_main(self, *flags: str, rate_card_payload: str | None):
        real_run_suite = evaluate_agent.run_suite

        def stub_suite(cases=None):
            if cases is None:
                return {**SYNTHETIC_REPORT}
            return real_run_suite(cases)

        with tempfile.TemporaryDirectory() as tmp:
            card_path = Path(tmp) / "cn-local-reference.json"
            if rate_card_payload is not None:
                card_path.write_text(rate_card_payload, encoding="utf-8")
            corpus_path = Path(tmp) / "eval_cases_real.json"
            corpus_path.write_text('{"cases": []}', encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(evaluate_agent, "REAL_CORPUS_PATH", corpus_path), \
                 mock.patch.object(evaluate_agent, "RATE_CARD_PATH", card_path), \
                 mock.patch.object(evaluate_agent, "run_suite", stub_suite), \
                 mock.patch.object(evaluate_agent.sys, "argv", ["evaluate_agent.py", *flags]), \
                 redirect_stdout(output):
                result = evaluate_agent.main()
            self.last_output = output.getvalue()
            return result

    MARKET_CARD = json.dumps({"sources": {
        "sd2000-listed-2026-11-21": {"confidence": "market-listed", "isSupplierPrice": False}}})
    ILLUSTRATIVE_CARD = json.dumps({"sources": {
        "illustrative-cost-design-2026-10": {"confidence": "illustrative"}}})

    def test_reference_gate_passes_with_market_source_and_empty_corpus(self):
        # 空真实语料在参考档不阻断；输出必须携带层级与边界声明。
        self.assertEqual(self.run_main("--release-gate", "--tier", "reference", "--json",
                                       rate_card_payload=self.MARKET_CARD), 0)
        report = json.loads(self.last_output)
        self.assertTrue(report["releaseGatePassed"])
        self.assertEqual(report["releaseTier"], "reference")
        self.assertIn("sd2000-listed-2026-11-21", report["marketReferenceVerdict"])
        self.assertIn("参考版边界声明", report["referenceBoundaryNotice"])

    def test_reference_gate_fails_without_market_source(self):
        self.assertEqual(self.run_main("--release-gate", "--tier", "reference",
                                       rate_card_payload=self.ILLUSTRATIVE_CARD), 1)
        self.assertIn("参考版发布门槛：未通过", self.last_output)

    def test_reference_gate_fails_on_synthetic_regression(self):
        real_run_suite = evaluate_agent.run_suite

        def weak_suite(cases=None):
            if cases is None:
                return {**SYNTHETIC_REPORT, "fieldAccuracy": 89.9}
            return real_run_suite(cases)

        with tempfile.TemporaryDirectory() as tmp:
            card_path = Path(tmp) / "cn-local-reference.json"
            card_path.write_text(self.MARKET_CARD, encoding="utf-8")
            corpus_path = Path(tmp) / "eval_cases_real.json"
            corpus_path.write_text('{"cases": []}', encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(evaluate_agent, "REAL_CORPUS_PATH", corpus_path), \
                 mock.patch.object(evaluate_agent, "RATE_CARD_PATH", card_path), \
                 mock.patch.object(evaluate_agent, "run_suite", weak_suite), \
                 mock.patch.object(evaluate_agent.sys, "argv",
                                   ["evaluate_agent.py", "--release-gate", "--tier", "reference"]), \
                 redirect_stdout(output):
                self.assertEqual(evaluate_agent.main(), 1)

    def test_stable_gate_unchanged_without_tier_flag(self):
        # 默认档不受参考版引入影响：空语料 + market 来源也照旧阻断。
        self.assertEqual(self.run_main("--release-gate", "--json",
                                       rate_card_payload=self.MARKET_CARD), 1)
        report = json.loads(self.last_output)
        self.assertEqual(report["releaseTier"], "stable")
        self.assertFalse(report["releaseGatePassed"])


if __name__ == "__main__":
    unittest.main()
