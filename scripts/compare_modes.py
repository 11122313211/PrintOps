#!/usr/bin/env python3
"""双轨一致性对比报告（规则轨 vs ScriptedPlanner LLM 轨）— V5-01 TD-08。

对一组句子各跑一次规则轨（``Agent(memory)``）与 LLM 轨
（``Agent(memory, planner=ScriptedPlanner(...))``），输出
docs/plan/path-05-eval-quality.md §12 结构的一致性报告：

    1. 元信息（模型名、提示词版本、语料版本、生成时间、git HEAD）
    2. 总体差异率（字段级不一致数 / 总比较字段数，分用例矩阵）
    3. 差异清单（逐条：用例名、字段、规则结果、LLM 结果、人工裁定）
    4. 行动项（需修规则 / 需修提示词 / 需补语料）
    5. 结论（本次模型/提示词变更是否可合入）

报告可直接回答"切换提示词后哪些用例行为变了"：先跑一次留机器可读 JSON
（--json），提示词/模型变更后再跑并 --baseline 对比，报告列出行为变化的用例。

标准库实现、零第三方依赖、不联网。LLM 轨使用 tests/test_planner_loop.py 的
ScriptedPlanner 离线替身；真实模型回放 fixture 有意不实现（S5-07 收窄，
见 docs/plan/v15-thin-backlog-plan.md V5-01 节）。

用法：
    python tools/compare_modes.py                     # 内置默认集，报告到 stdout
    python tools/compare_modes.py --case-file c.json  # 自定义用例集
    python tools/compare_modes.py --out reports/      # Markdown 报告写文件
    python tools/compare_modes.py --json r.json       # 机器可读报告（供 --baseline）
    python tools/compare_modes.py --baseline r.json   # 与上次运行对比行为变化
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import stdio_utf8  # noqa: E402  （仓库根目录的 UTF-8 管道助手，同 calibrate_pricing）

from agent import Agent, Memory  # noqa: E402
from llm_adapter import PLANNER_PROMPT_VERSION  # noqa: E402
from tests.test_planner_loop import ScriptedPlanner, final_plan, tool_plan  # noqa: E402

# 等价面：两轨最终订单状态必须一致的标量字段（缺省视为 ""）。
ORDER_FIELDS = (
    "productType", "purpose", "quantity", "quantityValue", "quantityUnit",
    "size", "pages", "orientation", "paper", "printing", "finishing",
    "binding", "deadline", "budget", "platform",
)
# 每用例比较的字段总数：15 个标量字段 + dimensions + productSpecs
# + missingFields + workflowStage。
FIELDS_PER_CASE = len(ORDER_FIELDS) + 4

READY_TEXT = "做 500 张 A4 名片，250g铜版纸，双面四色，下周内"

# 白名单外已知分歧预登记（键 = (用例名, 等价面字段)）。这是"已知并已裁定为
# 暂不修"的行为分歧台账：报告把它们标为"已知分歧（预登记）"，不计入
# "新增待裁定"。该台账只允许缩不允许涨——TD-25 已偿还，当前已知分歧为 0。
KNOWN_DIVERGENCES: dict[tuple[str, str], str] = {}

# 内置默认用例集：与 tests/test_dual_track_equivalence.py 的 10 组差分用例同源，
# 与 Agent.PLANNER_INTENT_PATTERNS 六意图对齐（quote/price/capability/validation/
# recommendation/handoff + 否定 + 多产品 + 缺字段追问×2）。
DEFAULT_CASES: list[dict[str, Any]] = [
    {"name": "询价-已选方案", "text": "请报价",
     "setup": [{"chat": READY_TEXT}, {"choose": "balanced"}],
     "plans": [None]},
    {"name": "估价-完整订单-模型调估价工具", "text": "费用是多少",
     "setup": [{"chat": READY_TEXT}],
     "plans": [tool_plan("estimate_price"), final_plan("模型已完成估价总结。")]},
    {"name": "能力-空订单", "text": "能印吗", "setup": [], "plans": [None]},
    {"name": "校验-模型调校验工具", "text": "订单完整吗", "setup": [],
     "plans": [tool_plan("validate_order"), final_plan("模型已核对订单信息。")]},
    {"name": "推荐-空订单兜底", "text": "怎么印", "setup": [], "plans": [None]},
    {"name": "交接-空订单阻断", "text": "请生成交接单", "setup": [], "plans": [None]},
    {"name": "否定询价-只检查", "text": "先不要询价，只检查订单",
     "setup": [], "plans": [None]},
    {"name": "多产品门-先于模型", "text": "做名片和海报",
     "setup": [], "plans": [final_plan("模型不应看到这句话。")]},
    {"name": "缺字段-模型补数量", "text": "做 500 张名片", "setup": [],
     "plans": [{"reply": "模型已登记数量。", "patch": {"quantity": "500张"},
                "tool": None}]},
    {"name": "追问-模型补尺寸", "text": "尺寸改成A3",
     "setup": [{"chat": "做 500 张名片"}],
     "plans": [{"reply": "模型已更新尺寸。", "patch": {"size": "A3"},
                "tool": None}]},
]


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    """Read a --case-file JSON array and validate its shape.

    用例 schema：{"name", "text", "setup": [{"chat": str}|{"choose": str}],
    "plans": [plan 对象或 null]}。plan 对象 = {"reply", "patch", "tool"}。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("用例文件必须是“非空 JSON 数组”")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 条用例必须是对象")
        name = str(raw.get("name") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not name or not text:
            raise ValueError(f"第 {index + 1} 条用例缺少 name 或 text")
        if name in seen:
            raise ValueError(f"用例名重复：{name}")
        seen.add(name)
        setup = raw.get("setup") or []
        if not isinstance(setup, list):
            raise ValueError(f"用例 {name} 的 setup 必须是数组")
        for step in setup:
            if not isinstance(step, dict) or set(step) - {"chat", "choose"} \
                    or len(step) != 1:
                raise ValueError(f"用例 {name} 的 setup 步骤只支持 {{chat:…}} / {{choose:…}}")
        plans = raw.get("plans") or []
        if not isinstance(plans, list):
            raise ValueError(f"用例 {name} 的 plans 必须是数组")
        for plan in plans:
            if plan is None:
                continue
            if not isinstance(plan, dict) \
                    or not set(plan) <= {"reply", "patch", "tool", "confidence",
                                         "evidence", "questions", "risks",
                                         "knowledgeVersion", "rejectedFields"}:
                raise ValueError(f"用例 {name} 的 plans 含非法 plan 对象")
        cases.append({"name": name, "text": text, "setup": setup, "plans": plans})
    return cases


def _apply_setup(agent: Agent, steps: list[dict[str, Any]]) -> None:
    for step in steps:
        if "chat" in step:
            agent.chat(str(step["chat"]))
        elif "choose" in step:
            agent.choose(str(step["choose"]))


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through both tracks; LLM 轨的剧本在 setup 后才挂上，
    保证 setup 轮不消费剧本（与差分测试同一约定）。"""
    temporary_msg = None
    with tempfile.TemporaryDirectory() as tmp:
        rule_agent = Agent(Memory(Path(tmp) / "rule.sqlite3"))
        llm_agent = Agent(Memory(Path(tmp) / "llm.sqlite3"))
        try:
            _apply_setup(rule_agent, case["setup"])
            _apply_setup(llm_agent, case["setup"])
        except Exception as error:  # 用例 setup 本身出错：记为用例级失败
            temporary_msg = f"setup 执行失败：{error.__class__.__name__}: {error}"
        if temporary_msg:
            return {"name": case["name"], "error": temporary_msg,
                    "rule": None, "llm": None, "plannerCalls": 0}
        planner = ScriptedPlanner(*case["plans"])
        llm_agent.planner = planner
        rule = rule_agent.chat(case["text"])
        llm = llm_agent.chat(case["text"])
        return {"name": case["name"], "error": None,
                "rule": rule, "llm": llm, "plannerCalls": len(planner.calls)}


def _meta_projection(field_meta: dict) -> dict:
    """fieldMeta 稳定投影：runId/updatedAt/evidence 是易变字段，不参与差分。"""
    return {key: {"source": value.get("source"), "confidence": value.get("confidence")}
            for key, value in field_meta.items() if isinstance(value, dict)}


def compare_case(case_result: dict[str, Any]) -> dict[str, Any]:
    """把一个用例的两轨信封拆成 等价面差异 / 白名单差异 两个清单。

    等价面差异（订单字段、missingFields、workflowStage）是预期为零的面，
    除非命中 KNOWN_DIVERGENCES；白名单差异（消息文案、fieldMeta、toolTrace、
    toolReceipts、planner 抢跑）是允许不同、逐项列出的面。
    """
    name = case_result["name"]
    if case_result.get("error"):
        return {"name": name, "error": case_result["error"],
                "equivalence": [], "whitelist": [], "verdict": "用例执行失败"}
    rule, llm = case_result["rule"], case_result["llm"]
    equivalence: list[dict[str, Any]] = []
    for key in ORDER_FIELDS:
        rule_value, llm_value = rule["order"].get(key, ""), llm["order"].get(key, "")
        if rule_value != llm_value:
            equivalence.append({"surface": f"order.{key}",
                                "rule": rule_value, "llm": llm_value})
    for key in ("dimensions", "productSpecs"):
        rule_value, llm_value = rule["order"].get(key) or {}, llm["order"].get(key) or {}
        if rule_value != llm_value:
            equivalence.append({"surface": f"order.{key}",
                                "rule": rule_value, "llm": llm_value})
    if rule["missingFields"] != llm["missingFields"]:
        equivalence.append({"surface": "missingFields",
                            "rule": rule["missingFields"], "llm": llm["missingFields"]})
    if rule["workflowStage"] != llm["workflowStage"]:
        equivalence.append({"surface": "workflowStage",
                            "rule": rule["workflowStage"], "llm": llm["workflowStage"]})

    whitelist: list[dict[str, Any]] = []
    if rule["messages"] != llm["messages"]:
        whitelist.append({"surface": "message", "rule": rule["messages"][0],
                          "llm": llm["messages"][0]})
    rule_meta, llm_meta = _meta_projection(rule["fieldMeta"]), _meta_projection(llm["fieldMeta"])
    for key in sorted(set(rule_meta) | set(llm_meta)):
        if rule_meta.get(key) != llm_meta.get(key):
            whitelist.append({"surface": f"fieldMeta.{key}",
                              "rule": rule_meta.get(key), "llm": llm_meta.get(key)})
    rule_receipts = [item.get("name") for item in rule["toolReceipts"]]
    llm_receipts = [item.get("name") for item in llm["toolReceipts"]]
    if rule_receipts != llm_receipts:
        whitelist.append({"surface": "toolReceipts", "rule": rule_receipts,
                          "llm": llm_receipts})
    rule_trace = [item for item in rule["toolTrace"] if item != "感知需求"]
    llm_trace = [item for item in llm["toolTrace"] if item != "感知需求"]
    if rule_trace != llm_trace:
        whitelist.append({"surface": "toolTrace", "rule": rule_trace, "llm": llm_trace})
    if case_result.get("plannerCalls"):
        whitelist.append({"surface": "plannerPreempt", "rule": False,
                          "llm": f"planner 轮接管（{case_result['plannerCalls']} 次模型调用）"})

    known: list[str] = []
    for diff in equivalence:
        reason = KNOWN_DIVERGENCES.get((name, diff["surface"]))
        diff["verdict"] = f"已知分歧（预登记）：{reason}" if reason else "待人工裁定"
        if reason:
            known.append(diff["surface"])
    unexpected = [diff for diff in equivalence if not diff.get("verdict", "").startswith("已知")]
    verdict = ("用例执行失败" if case_result.get("error")
               else "等价面一致" if not equivalence
               else "仅已知分歧" if not unexpected
               else "存在白名单外差异")
    return {"name": name, "error": None, "equivalence": equivalence,
            "whitelist": whitelist, "known": known, "verdict": verdict}


def run_comparison(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """跑全量用例并汇总成 §12 报告结构的机器可读形态。"""
    case_reports = [compare_case(run_case(case)) for case in cases]
    total_fields = len(cases) * FIELDS_PER_CASE
    equivalence_diffs = sum(len(item["equivalence"]) for item in case_reports)
    unexpected_diffs = sum(
        1 for item in case_reports for diff in item["equivalence"]
        if not diff.get("verdict", "").startswith("已知"))
    known_diffs = equivalence_diffs - unexpected_diffs
    report: dict[str, Any] = {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "planner": "scripted（离线替身，无回放 fixture——S5-07 有意收窄）",
            "model": "scripted",
            "promptVersion": PLANNER_PROMPT_VERSION,
            "corpus": f"cases={len(cases)}"
                      + (" (file)" if cases is not DEFAULT_CASES else " (内置默认集)"),
            "gitHead": _git_head(),
        },
        "totals": {
            "cases": len(cases),
            "fieldsCompared": total_fields,
            "equivalenceDiffs": equivalence_diffs,
            "knownDivergences": known_diffs,
            "unexpectedDiffs": unexpected_diffs,
            "diffRate": round(equivalence_diffs / total_fields * 100, 2) if total_fields else 0.0,
        },
        "cases": case_reports,
    }
    report["conclusion"] = _conclude(report, baseline=None)
    return report


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _conclude(report: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    """§12 第 5 节：本次模型/提示词变更是否可合入。"""
    totals = report["totals"]
    if baseline is not None:
        changes = changed_cases(baseline, report)
        return {
            "verdict": "可合入" if not changes else "需人工裁定",
            "reason": (f"与基线（提示词版本 {baseline.get('meta', {}).get('promptVersion', '?')}）"
                       f"相比无行为变化" if not changes else
                       f"与基线相比 {len(changes)} 例行为变化"),
            "changedCases": changes,
        }
    if totals["unexpectedDiffs"]:
        return {"verdict": "需人工裁定",
                "reason": f"存在 {totals['unexpectedDiffs']} 项白名单外等价面差异（未预登记）",
                "changedCases": []}
    if totals["equivalenceDiffs"]:
        return {"verdict": "可合入",
                "reason": f"等价面差异均为预登记已知分歧（{totals['knownDivergences']} 项）",
                "changedCases": []}
    return {"verdict": "可合入", "reason": "等价面零漂移", "changedCases": []}


def _case_signature(case_report: dict[str, Any]) -> dict[str, Any]:
    """用例行为签名：等价面差异逐条值 + 白名单差异面集合。"""
    return {
        "equivalence": [[diff["surface"], diff["rule"], diff["llm"]]
                        for diff in case_report["equivalence"]],
        "whitelist": [[diff["surface"], diff["rule"], diff["llm"]]
                      for diff in case_report["whitelist"]],
        "error": case_report.get("error"),
    }


def changed_cases(baseline: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """对比两次运行，返回行为发生变化的用例清单（回答"切换提示词后哪些用例
    行为变了"）。签名含白名单面：白名单面变化也意味着行为漂移（例如消息文案）。"""
    baseline_cases = {item["name"]: _case_signature(item)
                      for item in baseline.get("cases", [])}
    changes: list[dict[str, Any]] = []
    for item in current.get("cases", []):
        name = item["name"]
        before = baseline_cases.get(name)
        after = _case_signature(item)
        if before is None:
            changes.append({"case": name, "kind": "新增用例", "before": None, "after": after})
            continue
        if before != after:
            equivalence_changed = before["equivalence"] != after["equivalence"]
            whitelist_changed = before["whitelist"] != after["whitelist"]
            changes.append({
                "case": name,
                "kind": ("等价面+白名单面变化" if equivalence_changed and whitelist_changed
                         else "等价面变化" if equivalence_changed else "白名单面变化"),
                "before": before, "after": after,
            })
    for name, before in baseline_cases.items():
        if name not in {item["name"] for item in current.get("cases", [])}:
            changes.append({"case": name, "kind": "用例被移除", "before": before, "after": None})
    return changes


def load_baseline(path: str | Path) -> dict[str, Any]:
    baseline = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(baseline, dict) or "cases" not in baseline or "totals" not in baseline:
        raise ValueError("基线文件必须是 compare_modes --json 生成的报告 JSON")
    return baseline


def render_markdown(report: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    """渲染 §12 双轨一致性报告（consistency-YYYYMMDD.md）。"""
    meta, totals = report["meta"], report["totals"]
    lines: list[str] = []
    lines.append(f"# 双轨一致性报告（consistency-{meta['generatedAt'][:10].replace('-', '')}）")
    lines.append("")
    lines.append("## 1. 元信息")
    lines.append("")
    lines.append(f"- 生成时间：{meta['generatedAt']}")
    lines.append(f"- git HEAD：{meta['gitHead']}")
    lines.append(f"- planner：{meta['planner']}")
    lines.append(f"- 模型名：{meta['model']}")
    lines.append(f"- 提示词版本：PLANNER_PROMPT_VERSION={meta['promptVersion']}")
    lines.append(f"- 语料版本：{meta['corpus']}")
    if baseline is not None:
        lines.append(f"- 基线：提示词版本 {baseline.get('meta', {}).get('promptVersion', '?')}，"
                     f"生成于 {baseline.get('meta', {}).get('generatedAt', '?')}")
    lines.append("")
    lines.append("## 2. 总体差异率")
    lines.append("")
    lines.append(f"- 字段级不一致：**{totals['equivalenceDiffs']} / {totals['fieldsCompared']}**"
                 f"（{totals['diffRate']}%）——其中预登记已知分歧 {totals['knownDivergences']} 项、"
                 f"新增待裁定 {totals['unexpectedDiffs']} 项")
    lines.append("")
    lines.append("| 用例 | 等价面差异 | 白名单差异 | 裁定 |")
    lines.append("| --- | --- | --- | --- |")
    for item in report["cases"]:
        lines.append(f"| {item['name']} | {len(item['equivalence'])} | "
                     f"{len(item['whitelist'])} | {item['verdict']} |")
    lines.append("")
    lines.append("## 3. 差异清单（逐条）")
    lines.append("")
    if not any(item["equivalence"] or item["whitelist"] for item in report["cases"]):
        lines.append("（无差异）")
    for item in report["cases"]:
        if item.get("error"):
            lines.append(f"- **{item['name']}**：用例执行失败——{item['error']}")
            continue
        for diff in item["equivalence"]:
            verdict = diff.get("verdict", "待人工裁定")
            lines.append(f"- **{item['name']}**｜{diff['surface']}｜规则轨："
                         f"`{_short(diff['rule'])}`｜LLM 轨：`{_short(diff['llm'])}`｜裁定：{verdict}")
        for diff in item["whitelist"]:
            lines.append(f"- **{item['name']}**｜（白名单）{diff['surface']}｜规则轨："
                         f"`{_short(diff['rule'])}`｜LLM 轨：`{_short(diff['llm'])}`｜裁定：白名单内，允许不同")
    lines.append("")
    lines.append("## 4. 行动项")
    lines.append("")
    if totals["unexpectedDiffs"]:
        lines.append(f"- 需人工裁定（转坏例回流评估）：{totals['unexpectedDiffs']} 项白名单外"
                     "等价面差异，见第 3 节“待人工裁定”条目；裁定后登记进 KNOWN_DIVERGENCES 或修复。")
    else:
        lines.append("- 需修规则：无（等价面差异均为预登记已知分歧或零差异）。")
    lines.append("- 需修提示词：本次 LLM 轨为 ScriptedPlanner 离线替身，不消费提示词，"
                 "无法归因提示词；提示词契约由 tests/test_llm_adapter_protocols.py 绊线与"
                 "差分用例守护，真实模型回放按需录制（S5-07 有意收窄）。")
    failed = [item["name"] for item in report["cases"] if item.get("error")]
    lines.append(f"- 需补语料：{'、'.join(failed) if failed else '无（全部用例执行成功）'}。")
    lines.append("")
    lines.append("## 5. 结论")
    lines.append("")
    conclusion = report["conclusion"]
    lines.append(f"- **{conclusion['verdict']}**——{conclusion['reason']}。")
    if conclusion.get("changedCases"):
        lines.append("")
        lines.append("### 与基线相比行为变化的用例")
        lines.append("")
        for change in conclusion["changedCases"]:
            lines.append(f"- **{change['case']}**：{change['kind']}")
            if change.get("before") is not None:
                for surface, rule_value, llm_value in change["before"]["equivalence"]:
                    lines.append(f"  - 变更前等价面 {surface}：规则轨 `{_short(rule_value)}` / "
                                 f"LLM 轨 `{_short(llm_value)}`")
            if change.get("after") is not None:
                for surface, rule_value, llm_value in change["after"]["equivalence"]:
                    lines.append(f"  - 变更后等价面 {surface}：规则轨 `{_short(rule_value)}` / "
                                 f"LLM 轨 `{_short(llm_value)}`")
    lines.append("")
    return "\n".join(lines)


def _short(value: Any, limit: int = 60) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = text.replace("\n", "⏎")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _write_out(path: str | Path, content: str) -> Path:
    """写报告文件：路径以 .md 结尾则写该文件，否则视为目录并在其中生成
    consistency-时间戳.md（建议 reports/，gitignored 不入库）。"""
    target = Path(path)
    if target.suffix == ".md":
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = target / f"consistency-{stamp}.md"
    target.write_text(content, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    stdio_utf8.configure()
    parser = argparse.ArgumentParser(
        description="双轨一致性对比报告（规则轨 vs ScriptedPlanner LLM 轨，纯本地、无网络）")
    parser.add_argument("--case-file", help="用例 JSON 文件（默认用内置 10 例）")
    parser.add_argument("--out", help="Markdown 报告输出路径（以 .md 结尾写该文件，否则视为目录生成 consistency-时间戳.md；建议 reports/，不入库）")
    parser.add_argument("--json", help="机器可读报告输出路径（供下次 --baseline 对比）")
    parser.add_argument("--baseline", help="上次运行的报告 JSON，对比本次行为变化")
    args = parser.parse_args(argv)

    cases = load_cases(args.case_file) if args.case_file else DEFAULT_CASES
    report = run_comparison(cases)
    baseline = load_baseline(args.baseline) if args.baseline else None
    if baseline is not None:
        report["conclusion"] = _conclude(report, baseline)
    text = render_markdown(report, baseline)
    print(text)
    if args.out:
        written = _write_out(args.out, text)
        print(f"[compare_modes] Markdown 报告已写入 {written}")
    if args.json:
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"[compare_modes] 机器可读报告已写入 {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
