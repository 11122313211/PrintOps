"""Small deterministic regression suite for the PrintOps order Agent.

The suite intentionally uses the same public chat flow as the UI.  It is a
baseline for future rule/LLM changes, not a substitute for human acceptance or
supplier production data.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import Agent, Memory


CASES: list[dict[str, Any]] = [
    {
        "name": "标准画册",
        "turns": [{"text": "做 500 份 A4 宣传册，32页骑马钉，157g哑粉纸，双面四色，下周内"}],
        "expected": {"productType": "宣传册", "quantity": "500 份", "size": "A4", "pages": "32 页",
                     "paper": "157g 哑粉纸", "printing": "双面四色", "deadline": "下周"},
    },
    {
        "name": "尺寸识别",
        "turns": [{"text": "做 2000 张 B4 折页，210*267mm，157克哑粉纸，双面四色，下周内"}],
        "expected": {"productType": "折页", "quantity": "2000 张", "size": "B4 / 210×267MM", "paper": "157g 哑粉纸"},
    },
    {
        "name": "标签专属参数",
        "turns": [{"text": "做 2000 张圆形透明不干胶标签，50*50mm，四色印刷，下周内"}],
        "expected": {"productType": "标签", "quantity": "2000 张", "size": "50×50MM", "printing": "四色印刷",
                     "productSpecs.labelMaterial": "透明不干胶", "productSpecs.labelShape": "圆形"},
    },
    {
        "name": "多轮修改",
        "turns": [{"text": "做 500 份 A4 名片，250g铜版纸，双面四色，下周内"},
                  {"text": "数量改为 1200 份"}],
        "expected": {"productType": "名片", "quantity": "1200 份", "size": "A4", "paper": "250g 铜版纸"},
    },
    {
        "name": "结构品类澄清",
        "turns": [{"text": "做 500 个天地盖包装盒，60*40*20cm，350g白卡纸，双面四色，下周内"}],
        "expected": {"productType": "包装盒", "quantity": "500 个", "size": "60×40×20CM",
                     "productSpecs.boxStructure": "天地盖", "productSpecs.boxSize": "60×40×20CM"},
    },
]


def _normal(value: Any) -> str:
    return "".join(str(value or "").split()).replace("＊", "×").replace("*", "×").lower()


def _field(order: dict[str, Any], key: str) -> Any:
    if "." not in key:
        return order.get(key, "")
    root, child = key.split(".", 1)
    return (order.get(root) or {}).get(child, "")


def run_suite() -> dict[str, Any]:
    case_results: list[dict[str, Any]] = []
    total_fields = 0
    matched_fields = 0
    total_turns = 0
    turns_to_ready: list[int] = []
    latencies: list[float] = []
    completed_cases = 0
    with tempfile.TemporaryDirectory(prefix="printops-eval-") as directory:
        for case in CASES:
            agent = Agent(Memory(Path(directory) / f"{len(case_results)}.sqlite3"))
            final: dict[str, Any] = {}
            ready_turn = None
            for index, turn in enumerate(case["turns"], start=1):
                started = time.perf_counter()
                final = agent.chat(turn["text"], turn.get("patch"))
                latencies.append((time.perf_counter() - started) * 1000)
                total_turns += 1
                if ready_turn is None and not final.get("missingFields"):
                    ready_turn = index
            if ready_turn is not None:
                turns_to_ready.append(ready_turn)
            checks = []
            for key, expected in case["expected"].items():
                actual = _field(final.get("order") or {}, key)
                matched = _normal(actual) == _normal(expected)
                total_fields += 1
                matched_fields += int(matched)
                checks.append({"field": key, "expected": expected, "actual": actual, "ok": matched})
            complete = not final.get("missingFields")
            completed_cases += int(complete)
            case_results.append({"name": case["name"], "turns": len(case["turns"]),
                                 "workflowStage": final.get("workflowStage"), "complete": complete,
                                 "fieldAccuracy": round(sum(item["ok"] for item in checks) / len(checks) * 100) if checks else 100,
                                 "checks": checks})
    return {
        "cases": len(CASES), "passedCases": sum(item["fieldAccuracy"] == 100 for item in case_results),
        "completionRate": round(completed_cases / len(CASES) * 100),
        "fieldAccuracy": round(matched_fields / total_fields * 100) if total_fields else 100,
        "averageTurnsToReady": round(sum(turns_to_ready) / len(turns_to_ready), 2) if turns_to_ready else None,
        "averageResponseMs": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "totalTurns": total_turns, "results": case_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic PrintOps Agent evaluation suite")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    args = parser.parse_args()
    report = run_suite()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"PrintOps evaluation: {report['cases']} cases")
        print(f"字段准确率：{report['fieldAccuracy']}%")
        print(f"基础订单完整率：{report['completionRate']}%")
        print(f"平均达到基础完整的轮数：{report['averageTurnsToReady']}")
        print(f"平均响应耗时：{report['averageResponseMs']}ms")
        for item in report["results"]:
            print(f"- {item['name']}：{item['fieldAccuracy']}%，{item['workflowStage']}")
    return 0 if report["fieldAccuracy"] >= 90 and report["completionRate"] >= 80 else 1


if __name__ == "__main__":
    raise SystemExit(main())
