"""tests for tools/compare_modes.py（V5-01 TD-08 双轨对比报告 CLI）。

加载习惯沿用 tests/test_pricing_engine.py 的 MarketReferenceCardTest：
importlib 按文件路径加载 tools 模块；CLI 冒烟走子进程（同 test_knowledge_review）。
全部离线、标准库、无网络。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import compare_modes as _compare_modes

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "compare_modes.py"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(TOOL), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(ROOT), env=env)


class CompareModesCasesTest(unittest.TestCase):
    def test_default_case_set_covers_intent_matrix(self) -> None:
        """内置默认集 ≥8 例、名称唯一，覆盖六意图 + 否定 + 多产品 + 缺字段。"""
        cases = _compare_modes.DEFAULT_CASES
        self.assertGreaterEqual(len(cases), 8)
        names = [case["name"] for case in cases]
        self.assertEqual(len(names), len(set(names)), "用例名必须唯一")
        joined = " ".join(names)
        for intent in ("询价", "估价", "能力", "校验", "推荐", "交接"):
            self.assertIn(intent, joined, f"默认集缺少意图：{intent}")
        self.assertIn("否定", joined)
        self.assertIn("多产品", joined)
        self.assertIn("缺字段", joined)
        for case in cases:
            self.assertTrue(case["text"])
            self.assertIsInstance(case["plans"], list)
            self.assertIsInstance(case["setup"], list)

    def test_load_cases_validates_case_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            good.write_text(json.dumps([
                {"name": "示例", "text": "做 500 张名片", "setup": [],
                 "plans": [{"reply": "已登记", "patch": {"quantity": "500张"}, "tool": None}]},
            ], ensure_ascii=False), encoding="utf-8")
            loaded = _compare_modes.load_cases(good)
            self.assertEqual(loaded[0]["name"], "示例")
            self.assertEqual(loaded[0]["plans"][0]["patch"]["quantity"], "500张")

            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps([{"name": "", "text": "句子"}], ensure_ascii=False),
                           encoding="utf-8")
            with self.assertRaises(ValueError):
                _compare_modes.load_cases(bad)

            duplicate = Path(tmp) / "dup.json"
            duplicate.write_text(json.dumps([
                {"name": "a", "text": "一"}, {"name": "a", "text": "二"},
            ], ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                _compare_modes.load_cases(duplicate)

            empty = Path(tmp) / "empty.json"
            empty.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                _compare_modes.load_cases(empty)


class CompareModesComparisonTest(unittest.TestCase):
    def test_default_run_known_divergence_only(self) -> None:
        """内置默认集：TD-25 偿还后等价面零分歧，无新增白名单外差异，结论可合入。"""
        report = _compare_modes.run_comparison(_compare_modes.DEFAULT_CASES)
        self.assertEqual(report["totals"]["cases"], len(_compare_modes.DEFAULT_CASES))
        self.assertEqual(report["totals"]["fieldsCompared"],
                         len(_compare_modes.DEFAULT_CASES) * _compare_modes.FIELDS_PER_CASE)
        self.assertEqual(report["totals"]["unexpectedDiffs"], 0)
        self.assertEqual(report["totals"]["equivalenceDiffs"],
                         report["totals"]["knownDivergences"])
        self.assertEqual(report["totals"]["equivalenceDiffs"], 0)
        self.assertEqual(report["conclusion"]["verdict"], "可合入")
        # TD-25 已偿还：估价用例两轨 workflowStage 均为 quote，零等价面分歧。
        price = next(item for item in report["cases"]
                     if item["name"] == "估价-完整订单-模型调估价工具")
        self.assertEqual(price["equivalence"], [])
        # 多产品门用例两轨零差异。
        multi = next(item for item in report["cases"] if item["name"] == "多产品门-先于模型")
        self.assertEqual(multi["equivalence"], [])
        self.assertEqual(multi["whitelist"], [])
        # 元信息带提示词版本。
        self.assertEqual(report["meta"]["promptVersion"], "1")

    def test_case_file_with_new_divergence_is_flagged(self) -> None:
        """自定义用例引入未预登记的等价面差异 → 裁定为待人工、结论需人工裁定。"""
        with tempfile.TemporaryDirectory() as tmp:
            case_file = Path(tmp) / "cases.json"
            case_file.write_text(json.dumps([
                {"name": "自定义-模型改数量", "text": "做 500 张名片", "setup": [],
                 "plans": [{"reply": "按模型理解登记。", "patch": {"quantity": "800张"},
                            "tool": None}]},
            ], ensure_ascii=False), encoding="utf-8")
            cases = _compare_modes.load_cases(case_file)
            report = _compare_modes.run_comparison(cases)
        self.assertEqual(report["totals"]["unexpectedDiffs"], 2)
        surfaces = {diff["surface"] for diff in report["cases"][0]["equivalence"]}
        self.assertEqual(surfaces, {"order.quantity", "order.quantityValue"})
        self.assertTrue(all(diff["verdict"] == "待人工裁定"
                            for diff in report["cases"][0]["equivalence"]))
        self.assertEqual(report["conclusion"]["verdict"], "需人工裁定")

    def test_baseline_detects_behavior_change(self) -> None:
        """基线对比必须能回答"切换后哪些用例行为变了"：把模型剧本的数量改掉，
        changed_cases 只点名该用例；完全相同的两次运行则点名零用例。"""
        with tempfile.TemporaryDirectory() as tmp:
            case_file = Path(tmp) / "cases.json"
            plan = {"reply": "已登记", "patch": {"quantity": "500张"}, "tool": None}
            case_file.write_text(json.dumps([
                {"name": "补数量", "text": "做 500 张名片", "setup": [], "plans": [plan]},
                {"name": "询价", "text": "请报价", "setup": [], "plans": [None]},
            ], ensure_ascii=False), encoding="utf-8")
            cases = _compare_modes.load_cases(case_file)
            baseline = _compare_modes.run_comparison(cases)
            # 相同输入复跑：无行为变化。
            repeat = _compare_modes.run_comparison(cases)
            self.assertEqual(_compare_modes.changed_cases(baseline, repeat), [])
            self.assertEqual(_compare_modes._conclude(repeat, baseline)["verdict"], "可合入")
            # 提示词/模型变更模拟：模型剧本把数量改成 800 张。
            drifted = _compare_modes.load_cases(case_file)
            drifted[0]["plans"][0]["patch"]["quantity"] = "800张"
            after = _compare_modes.run_comparison(drifted)
            changes = _compare_modes.changed_cases(baseline, after)
            self.assertEqual([change["case"] for change in changes], ["补数量"])
            self.assertIn("等价面", changes[0]["kind"])
            conclusion = _compare_modes._conclude(after, baseline)
            self.assertEqual(conclusion["verdict"], "需人工裁定")


class CompareModesReportTest(unittest.TestCase):
    def test_markdown_report_has_all_sections(self) -> None:
        """§12 结构：元信息/总体差异率/差异清单/行动项/结论五节齐全，且
        元信息可回答"提示词版本"。"""
        report = _compare_modes.run_comparison(_compare_modes.DEFAULT_CASES)
        text = _compare_modes.render_markdown(report, baseline=None)
        for section in ("## 1. 元信息", "## 2. 总体差异率", "## 3. 差异清单（逐条）",
                        "## 4. 行动项", "## 5. 结论"):
            self.assertIn(section, text)
        self.assertIn("PLANNER_PROMPT_VERSION=1", text)
        self.assertIn("等价面零漂移", text)
        self.assertIn("可合入", text)
        self.assertNotIn("api_key", text)

    def test_baseline_section_lists_changed_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = Path(tmp) / "baseline.json"
            case_file = Path(tmp) / "cases.json"
            case_file.write_text(json.dumps([
                {"name": "补数量", "text": "做 500 张名片", "setup": [],
                 "plans": [{"reply": "已登记", "patch": {"quantity": "500张"}, "tool": None}]},
            ], ensure_ascii=False), encoding="utf-8")
            baseline = _compare_modes.run_comparison(_compare_modes.load_cases(case_file))
            baseline_path.write_text(json.dumps(baseline, ensure_ascii=False),
                                     encoding="utf-8")
            drifted_cases = _compare_modes.load_cases(case_file)
            drifted_cases[0]["plans"][0]["patch"]["quantity"] = "800张"
            after = _compare_modes.run_comparison(drifted_cases)
            after["conclusion"] = _compare_modes._conclude(after, baseline)
            text = _compare_modes.render_markdown(after, baseline=json.loads(
                baseline_path.read_text(encoding="utf-8")))
        self.assertIn("### 与基线相比行为变化的用例", text)
        self.assertIn("**补数量**", text)
        self.assertIn("需人工裁定", text)


class CompareModesCliTest(unittest.TestCase):
    def test_help_exits_zero(self) -> None:
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--case-file", proc.stdout)
        self.assertIn("--baseline", proc.stdout)

    def test_cli_writes_markdown_and_json_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "reports"
            json_path = Path(tmp) / "report.json"
            case_file = Path(tmp) / "cases.json"
            case_file.write_text(json.dumps([
                {"name": "cli-冒烟", "text": "做 500 张名片", "setup": [],
                 "plans": [{"reply": "已登记", "patch": {"quantity": "500张"}, "tool": None}]},
            ], ensure_ascii=False), encoding="utf-8")
            proc = run_cli("--case-file", str(case_file), "--out", str(out_dir),
                           "--json", str(json_path))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("双轨一致性报告", proc.stdout)
            written = list(out_dir.glob("consistency-*.md"))
            self.assertEqual(len(written), 1, "目录输出应生成 consistency-时间戳.md")
            report_text = written[0].read_text(encoding="utf-8")
            self.assertIn("## 5. 结论", report_text)
            self.assertTrue(json_path.exists())
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["cases"][0]["name"], "cli-冒烟")
            self.assertIn("promptVersion", data["meta"])

    def test_cli_baseline_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_file = Path(tmp) / "cases.json"
            case_file.write_text(json.dumps([
                {"name": "冒烟", "text": "能印吗", "setup": [], "plans": [None]},
            ], ensure_ascii=False), encoding="utf-8")
            json_path = Path(tmp) / "baseline.json"
            first = run_cli("--case-file", str(case_file), "--json", str(json_path))
            self.assertEqual(first.returncode, 0, first.stderr)
            second = run_cli("--case-file", str(case_file),
                             "--baseline", str(json_path))
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("相比无行为变化", second.stdout)

    def test_cli_rejects_missing_baseline(self) -> None:
        proc = run_cli("--baseline", "does-not-exist.json")
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
