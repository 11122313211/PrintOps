"""Deterministic regression suite for the PrintOps order Agent.

v0.9.1: 100+ desensitized order cases covering the ROADMAP v0.9.0 checklist —
negation, modification, ambiguity, multi-product orders and size formats —
with per-tag accuracy tracking.  The suite intentionally uses the same public
chat flow as the UI; it is a baseline for rule/LLM changes, not a substitute
for human acceptance or supplier production data.
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


def _hand_cases() -> list[dict[str, Any]]:
    return [
        # ---------------------------------------------------------- 基础
        {"name": "标准画册", "tag": "基础",
         "turns": [{"text": "做 500 份 A4 宣传册，32页骑马钉，157g哑粉纸，双面四色，下周内"}],
         "expected": {"productType": "宣传册", "quantity": "500 份", "size": "A4", "pages": "32 页",
                      "paper": "157g 哑粉纸", "printing": "双面四色", "deadline": "下周"}},
        {"name": "尺寸识别", "tag": "基础",
         "turns": [{"text": "做 2000 张 B4 折页，210*267mm，157克哑粉纸，双面四色，下周内"}],
         "expected": {"productType": "折页", "quantity": "2000 张", "size": "B4 / 210×267MM",
                      "paper": "157g 哑粉纸"}},
        {"name": "标签专属参数", "tag": "基础",
         "turns": [{"text": "做 2000 张圆形透明不干胶标签，50*50mm，四色印刷，下周内"}],
         "expected": {"productType": "标签", "quantity": "2000 张", "size": "50×50MM", "printing": "四色印刷",
                      "productSpecs.labelMaterial": "透明不干胶", "productSpecs.labelShape": "圆形"}},
        {"name": "结构品类澄清", "tag": "基础",
         "turns": [{"text": "做 500 个天地盖包装盒，60*40*20cm，350g白卡纸，双面四色，下周内"}],
         "expected": {"productType": "包装盒", "quantity": "500 个", "size": "60×40×20CM",
                      "productSpecs.boxStructure": "天地盖", "productSpecs.boxSize": "60×40×20CM"}},
        {"name": "纸杯容量与材质", "tag": "基础",
         "turns": [{"text": "做 2000 个纸杯，350ml，双PE"}],
         "expected": {"productType": "纸杯", "quantity": "2000 个",
                      "productSpecs.cupVolume": "350ml", "productSpecs.cupMaterial": "双PE"}},
        {"name": "手提袋材料提手", "tag": "基础",
         "turns": [{"text": "做 500 个无纺布手提袋，38*30*10cm，棉绳提手"}],
         "expected": {"productType": "手提袋", "quantity": "500 个", "productSpecs.bagMaterial": "无纺布",
                      "productSpecs.handle": "棉绳", "productSpecs.bagSize": "38×30×10CM"}},
        {"name": "PVC卡厚度卡型", "tag": "基础",
         "turns": [{"text": "做 300 张0.76mm PVC人像证卡"}],
         "expected": {"productType": "PVC卡", "quantity": "300 张",
                      "productSpecs.cardType": "人像证卡", "productSpecs.cardThickness": "0.76mm"}},
        {"name": "PVC卡芯片", "tag": "基础",
         "turns": [{"text": "做 600 张智能卡PVC卡，厚度0.76mm"}],
         "expected": {"productType": "PVC卡", "productSpecs.cardType": "智能卡",
                      "productSpecs.cardThickness": "0.76mm", "productSpecs.chip": "需要芯片/磁条"}},
        {"name": "吊牌挂孔穿绳", "tag": "基础",
         "turns": [{"text": "做 500 张吊牌，圆孔，配棉绳"}],
         "expected": {"productType": "吊牌", "quantity": "500 张",
                      "productSpecs.hangHole": "圆孔", "productSpecs.string": "棉绳"}},
        {"name": "海报介质安装", "tag": "基础",
         "turns": [{"text": "做 600 张海报，背胶，墙面张贴"}],
         "expected": {"productType": "海报", "quantity": "600 张",
                      "productSpecs.displayMaterial": "背胶", "productSpecs.install": "墙面张贴"}},
        {"name": "喷画观看距离", "tag": "基础",
         "turns": [{"text": "做 200 张户外灯布喷画，观看距离 10 米"}],
         "expected": {"productType": "喷画", "quantity": "200 张", "productSpecs.displayMaterial": "灯布",
                      "productSpecs.install": "户外", "productSpecs.viewingDistance": "10米"}},
        {"name": "PVC板厚度", "tag": "基础",
         "turns": [{"text": "做 100 张 PVC 展板，厚度 3mm"}],
         "expected": {"productType": "PVC", "quantity": "100 张", "productSpecs.boardThickness": "3mm"}},
        {"name": "信封规格开口", "tag": "基础",
         "turns": [{"text": "做 500 个信封，成品尺寸 220*110mm，上开口"}],
         "expected": {"productType": "信封封套", "quantity": "500 个", "size": "220×110MM",
                      "productSpecs.opening": "上开口"}},
        {"name": "名片圆角专色", "tag": "基础",
         "turns": [{"text": "做 1000 张名片，250g铜版纸，圆角，专色"}],
         "expected": {"productType": "名片", "quantity": "1000 张", "paper": "250g 铜版纸",
                      "productSpecs.cardCorners": "圆角", "productSpecs.cardColor": "专色"}},
        {"name": "联单联数流水号", "tag": "基础",
         "turns": [{"text": "做 300 本三联无碳联单，A4，需要流水号"}],
         "expected": {"productType": "联单", "quantity": "300 本", "size": "A4",
                      "productSpecs.paperParts": "三联", "productSpecs.numbering": "需要连续编号"}},
        {"name": "版式方向", "tag": "基础",
         "turns": [{"text": "做 500 张单页，横版"}],
         "expected": {"productType": "单页", "orientation": "横版"}},
        {"name": "预算金额", "tag": "基础",
         "turns": [{"text": "做 500 张单页，预算控制在 1000 元"}],
         "expected": {"productType": "单页", "budget": "预算 ¥1000"}},
        {"name": "页数识别", "tag": "基础",
         "turns": [{"text": "做 200 本画册，48页，胶装"}],
         "expected": {"productType": "画册", "pages": "48 页", "binding": "胶装"}},
        {"name": "出血参数", "tag": "基础",
         "turns": [{"text": "做 500 张单页，尺寸 210×285mm，出血 3mm"}],
         "expected": {"productType": "单页", "size": "210×285MM", "productSpecs.bleed": "3mm"}},
        {"name": "名片默认单位", "tag": "基础",
         "turns": [{"text": "做 1000 张 250g 白卡纸名片"}],
         "expected": {"productType": "名片", "quantity": "1000 张", "paper": "250g 白卡纸"}},
        {"name": "内尺寸包装盒", "tag": "歧义",
         "turns": [{"text": "做 500 个包装盒，内尺寸 60*40*20cm"}],
         "expected": {"productType": "包装盒", "productSpecs.boxSize": "60×40×20CM",
                      "productSpecs.boxSizeInner": "60×40×20CM", "dimensions.packageSize": "60×40×20CM"}},
        {"name": "外尺寸包装盒", "tag": "歧义",
         "turns": [{"text": "做 500 个包装盒，外尺寸 65*45*25cm"}],
         "expected": {"productType": "包装盒", "productSpecs.boxSize": "65×45×25CM",
                      "productSpecs.boxSizeOuter": "65×45×25CM"}},
        {"name": "成品与展开并存", "tag": "歧义",
         "turns": [{"text": "做 500 张 A4 折页，成品尺寸 210*267mm，展开尺寸 420*534mm"}],
         "expected": {"size": "210×267MM", "dimensions.finishedSize": "210×267MM",
                      "dimensions.expandedSize": "420×534MM"}},
        {"name": "成品优先于刀模", "tag": "歧义",
         "turns": [{"text": "做 500 张折页，成品尺寸 210*285mm，刀模尺寸 426*291mm"}],
         "expected": {"size": "210×285MM", "dimensions.dieCutSize": "426×291MM"}},
        {"name": "B4数字不被当数量", "tag": "歧义",
         "turns": [{"text": "做 B4 折页 500 张"}],
         "expected": {"productType": "折页", "quantity": "500 张", "size": "B4"}},
        {"name": "小尺寸标签", "tag": "歧义",
         "turns": [{"text": "做 2000 张 5*5cm 圆形不干胶标签"}],
         "expected": {"productType": "标签", "quantity": "2000 张", "size": "5×5CM",
                      "productSpecs.labelShape": "圆形", "productSpecs.labelMaterial": "不干胶"}},
        {"name": "骑马钉页数风险", "tag": "歧义",
         "turns": [{"text": "做 100 张 A4 宣传册，骑马钉，48页"}],
         "expected": {"productType": "宣传册", "pages": "48 页", "binding": "骑马钉"}},
        {"name": "页面数不串数量", "tag": "歧义",
         "turns": [{"text": "做 500 份 A4 宣传册，共 24 页"}],
         "expected": {"quantity": "500 份", "pages": "24 页"}},
        {"name": "数码可变数据", "tag": "基础",
         "turns": [{"text": "做 500 张数码印刷，可变数据，需要打样"}],
         "expected": {"productType": "数码印刷", "quantity": "500 张",
                      "productSpecs.variableData": "需要可变数据", "productSpecs.proofing": "需要打样"}},

        # ---------------------------------------------------------- 否定
        {"name": "否定覆膜", "tag": "否定",
         "turns": [{"text": "做 1000 张单页，双面四色，覆膜，下周内"}, {"text": "不要覆膜"}],
         "expected": {"productType": "单页", "finishing": "无特殊工艺"}},
        {"name": "否定烫金改哑膜", "tag": "否定",
         "turns": [{"text": "做 500 张名片，烫金"}, {"text": "不要烫金，改哑膜"}],
         "expected": {"productType": "名片", "finishing": "哑膜"}},
        {"name": "否定编号", "tag": "否定",
         "turns": [{"text": "做 300 本三联无碳联单，A4，需要流水号"}, {"text": "不需要编号"}],
         "expected": {"productType": "联单", "productSpecs.numbering": "不需要编号"}},
        {"name": "否定装订", "tag": "否定",
         "turns": [{"text": "做 500 张折页，157克哑粉纸"}, {"text": "不要装订"}],
         "expected": {"productType": "折页", "binding": "无需装订"}},
        {"name": "否定圆角", "tag": "否定",
         "turns": [{"text": "做 500 张名片，圆角"}, {"text": "改直角"}],
         "expected": {"productType": "名片", "productSpecs.cardCorners": "直角"}},
        {"name": "否定淋膜", "tag": "否定",
         "turns": [{"text": "做 2000 个纸杯，350ml"}, {"text": "不需要内淋膜"}],
         "expected": {"productType": "纸杯", "productSpecs.innerCoating": "不需要内淋膜"}},
        {"name": "否定刀模", "tag": "否定",
         "turns": [{"text": "做 500 个天地盖包装盒，60*40*20cm，需要刀模"}, {"text": "没有刀模"}],
         "expected": {"productType": "包装盒", "productSpecs.dieCut": "需确认刀模文件"}},
        {"name": "否定全部工艺", "tag": "否定",
         "turns": [{"text": "做 1000 张单页，烫金 局部UV"}, {"text": "工艺都不要了"}],
         "expected": {"productType": "单页"}},

        # ---------------------------------------------------------- 修改
        {"name": "修改数量", "tag": "修改",
         "turns": [{"text": "做 500 张 A4 单页，双面四色，下周内"}, {"text": "数量改成 1200 张"}],
         "expected": {"productType": "单页", "quantity": "1200 张", "quantityValue": 1200}},
        {"name": "修改尺寸", "tag": "修改",
         "turns": [{"text": "做 500 张 A4 单页，双面四色"}, {"text": "尺寸改成 B4"}],
         "expected": {"productType": "单页", "size": "B4"}},
        {"name": "修改页数", "tag": "修改",
         "turns": [{"text": "做 300 本画册，32页，胶装"}, {"text": "页数改成 48 页"}],
         "expected": {"productType": "画册", "pages": "48 页", "binding": "胶装"}},
        {"name": "修改交期", "tag": "修改",
         "turns": [{"text": "做 500 张单页，下周内"}, {"text": "交期改成月底"}],
         "expected": {"productType": "单页", "deadline": "月底"}},
        {"name": "修改印刷面", "tag": "修改",
         "turns": [{"text": "做 500 张名片，双面四色"}, {"text": "改单面"}],
         "expected": {"productType": "名片", "printing": "单面四色"}},
        {"name": "修改盒型", "tag": "修改",
         "turns": [{"text": "做 500 个天地盖包装盒"}, {"text": "改成抽屉盒"}],
         "expected": {"productType": "包装盒", "productSpecs.boxStructure": "抽屉盒"}},
        {"name": "修改纸张克重", "tag": "修改",
         "turns": [{"text": "做 1000 张单页，157克哑粉纸"}, {"text": "纸换成 250g 铜版纸"}],
         "expected": {"productType": "单页", "paper": "250g 铜版纸"}},
        {"name": "修改胶水", "tag": "修改",
         "turns": [{"text": "做 2000 张不干胶标签，普通胶"}, {"text": "改成可移胶"}],
         "expected": {"productType": "标签", "productSpecs.adhesive": "可移胶"}},
        {"name": "修改预算", "tag": "修改",
         "turns": [{"text": "做 500 张名片，下周内"}, {"text": "预算控制在 800 元"}],
         "expected": {"productType": "名片", "budget": "预算 ¥800"}},
        {"name": "修改为万级数量", "tag": "修改",
         "turns": [{"text": "做 500 张单页"}, {"text": "数量改成 2万张"}],
         "expected": {"productType": "单页", "quantity": "20000 张", "quantityValue": 20000}},

        # ---------------------------------------------------------- 多产品
        {"name": "两产品数量归属", "tag": "多产品",
         "turns": [{"text": "做 500 张名片和 1000 张折页，下周内"}],
         "expected": {"items.0.productType": "名片", "items.0.quantity": "500 张",
                      "items.1.productType": "折页", "items.1.quantity": "1000 张",
                      "items.0.deadline": "下周", "items.1.deadline": "下周"}},
        {"name": "三产品拆分", "tag": "多产品",
         "turns": [{"text": "做 500 张名片、1000 张折页和 2000 张单页"}],
         "expected": {"items.0.quantity": "500 张", "items.1.quantity": "1000 张",
                      "items.2.quantity": "2000 张"}},
        {"name": "两产品共享纸张", "tag": "多产品",
         "turns": [{"text": "500 张名片和 1000 张折页，157g哑粉纸"}],
         "expected": {"items.0.paper": "157g 哑粉纸", "items.1.paper": "157g 哑粉纸"}},
        {"name": "两产品共享颜色", "tag": "多产品",
         "turns": [{"text": "做 300 本画册和 500 张单页，双面四色，下周内"}],
         "expected": {"items.0.quantity": "300 本", "items.1.quantity": "500 张",
                      "items.0.printing": "双面四色", "items.1.printing": "双面四色"}},
        {"name": "两产品共享尺寸", "tag": "多产品",
         "turns": [{"text": "做 1000 张单页和 2000 张不干胶标签，50*50mm"}],
         "expected": {"items.0.size": "50×50MM", "items.1.size": "50×50MM",
                      "items.1.productSpecs.labelMaterial": "不干胶"}},
        {"name": "盒与单页参数隔离", "tag": "多产品",
         "turns": [{"text": "做 500 个天地盖包装盒和 800 张单页"}],
         "expected": {"items.0.productSpecs.boxStructure": "天地盖", "items.0.quantity": "500 个",
                      "items.1.productType": "单页"}},
        {"name": "标签材质不串项", "tag": "多产品",
         "turns": [{"text": "做 2000 张透明不干胶标签和 1000 张单页"}],
         "expected": {"items.0.productSpecs.labelMaterial": "透明不干胶",
                      "items.1.productType": "单页"}},
        {"name": "联单与单页单位", "tag": "多产品",
         "turns": [{"text": "做 500 本联单和 1000 张单页"}],
         "expected": {"items.0.quantity": "500 本", "items.1.quantity": "1000 张"}},
    ]


def _matrix_cases() -> list[dict[str, Any]]:
    """Programmatic coverage for size formats and quantity expressions."""
    cases: list[dict[str, Any]] = []

    size_formats = [
        ("A4", "A4"), ("B5", "B5"), ("210*267mm", "210×267MM"), ("210 X 285", "210×285"),
        ("60*40*20cm", "60×40×20CM"), ("5*5cm", "5×5CM"), ("889×1194mm", "889×1194MM"),
        ("210＊285", "210×285"),
    ]
    for product in ("折页", "单页", "标签", "吊牌"):
        for expr, expected_size in size_formats:
            cases.append({
                "name": f"尺寸格式 {product} {expr}", "tag": "尺寸格式",
                "turns": [{"text": f"做 1000 张{product}，{expr}，157克哑粉纸，双面四色，下周内"}],
                "expected": {"productType": product, "quantity": "1000 张", "size": expected_size},
            })

    quantity_forms = [
        (lambda product, unit: f"做 500 {product}", "500 {unit}", 500),
        (lambda product, unit: f"做 500 {unit} {product}", "500 {unit}", 500),
        (lambda product, unit: f"做 1,000 {unit} {product}", "1000 {unit}", 1000),
        (lambda product, unit: f"做 2万{unit}{product}", "20000 {unit}", 20000),
        (lambda product, unit: f"做 3千{unit}{product}", "3000 {unit}", 3000),
    ]
    for product, unit in (("名片", "张"), ("宣传册", "本"), ("包装盒", "个")):
        for index, (build, expected_template, value) in enumerate(quantity_forms):
            cases.append({
                "name": f"数量表达 {product} #{index + 1}", "tag": "数量表达",
                "turns": [{"text": build(product, unit)}],
                "expected": {"productType": product,
                             "quantity": expected_template.format(unit=unit), "quantityValue": value},
            })
    # Multi-turn quantity revisions keep the default unit of the category.
    for product, unit in (("名片", "张"), ("宣传册", "本"), ("包装盒", "个")):
        cases.append({
            "name": f"数量表达 {product} 改为无单位", "tag": "数量表达",
            "turns": [{"text": f"做 100 {unit} {product}"}, {"text": "数量改为 3000"}],
            "expected": {"productType": product, "quantity": f"3000 {unit}", "quantityValue": 3000},
        })
    for product in ("名片", "宣传册", "包装盒"):
        cases.append({
            "name": f"数量表达 {product} 份覆盖默认单位", "tag": "数量表达",
            "turns": [{"text": f"做 800 份 {product}"}],
            "expected": {"productType": product, "quantity": "800 份", "quantityValue": 800},
        })
        cases.append({
            "name": f"数量表达 {product} 印刷前缀", "tag": "数量表达",
            "turns": [{"text": f"印刷 1500 {product}"}],
            "expected": {"productType": product, "quantity": f"1500 {DEFAULT_UNITS[product]}",
                         "quantityValue": 1500},
        })
    return cases


DEFAULT_UNITS = {"名片": "张", "宣传册": "本", "包装盒": "个"}
CASES: list[dict[str, Any]] = _hand_cases() + _matrix_cases()

# Most cases intentionally feed partial input (they test single fields), so
# the completion metric only counts cases that receive one extra turn with
# the remaining base fields.  This keeps the gate meaningful instead of
# rewarding verbose cases.
COMPLETION_TURNS: dict[str, str] = {
    "纸杯容量与材质": "A5 版面，单面四色，下周内",
    "PVC卡厚度卡型": "90×54mm，单面四色，12天内",
    "海报介质安装": "尺寸 500*700mm，单面四色，下周内",
    "信封规格开口": "250g 白卡纸，单面四色，12天内",
    "名片圆角专色": "90×54mm，单面四色，下周内",
    "联单联数流水号": "60g 胶版纸，单色印刷，30天内",
    "出血参数": "157g 哑粉纸，单面四色，下周内",
    "B4数字不被当数量": "157g 哑粉纸，双面四色，下周内",
    "页面数不串数量": "157g 哑粉纸，双面四色，下周内",
    "手提袋材料提手": "单面四色，12天内",
}
EXPECT_COMPLETE: set[str] = set(COMPLETION_TURNS) | {"标准画册", "尺寸识别", "标签专属参数", "结构品类澄清"}


def _normal(value: Any) -> str:
    return "".join(str(value or "").split()).replace("＊", "×").replace("*", "×").lower()


def _field(order: dict[str, Any], key: str) -> Any:
    """Read ``items.0.quantity`` style paths as well as plain/spec keys."""
    value: Any = order
    for part in key.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit():
            index = int(part)
            value = value[index] if index < len(value) else None
        else:
            return ""
        if value is None:
            return ""
    return value


def run_suite(cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cases = CASES if cases is None else cases
    case_results: list[dict[str, Any]] = []
    total_fields = 0
    matched_fields = 0
    total_turns = 0
    turns_to_ready: list[int] = []
    latencies: list[float] = []
    completed_cases = 0
    expected_complete_total = 0
    expected_complete_done = 0
    tag_stats: dict[str, dict[str, int]] = {}
    with tempfile.TemporaryDirectory(prefix="printops-eval-") as directory:
        for case_index, case in enumerate(cases):
            agent = Agent(Memory(Path(directory) / f"{case_index}.sqlite3"))
            final: dict[str, Any] = {}
            ready_turn = None
            turns = list(case["turns"])
            if case["name"] in COMPLETION_TURNS:
                turns.append({"text": COMPLETION_TURNS[case["name"]]})
            for index, turn in enumerate(turns, start=1):
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
            if case["name"] in EXPECT_COMPLETE:
                expected_complete_total += 1
                expected_complete_done += int(complete)
            completed_cases += int(complete)
            stats = tag_stats.setdefault(case["tag"], {"cases": 0, "matched": 0, "total": 0})
            stats["cases"] += 1
            stats["matched"] += sum(1 for item in checks if item["ok"])
            stats["total"] += len(checks)
            case_results.append({"name": case["name"], "tag": case["tag"], "turns": len(case["turns"]),
                                 "workflowStage": final.get("workflowStage"), "complete": complete,
                                 "fieldAccuracy": round(sum(item["ok"] for item in checks) / len(checks) * 100) if checks else 100,
                                 "checks": checks})
    tag_report = {tag: {"cases": stats["cases"],
                        "fieldAccuracy": round(stats["matched"] / stats["total"] * 100) if stats["total"] else 100}
                  for tag, stats in sorted(tag_stats.items())}
    return {
        "cases": len(cases), "passedCases": sum(item["fieldAccuracy"] == 100 for item in case_results),
        "completionRate": round(expected_complete_done / expected_complete_total * 100) if expected_complete_total else 100,
        "expectedCompleteCases": expected_complete_total,
        "fieldAccuracy": round(matched_fields / total_fields * 100) if total_fields else 100,
        "averageTurnsToReady": round(sum(turns_to_ready) / len(turns_to_ready), 2) if turns_to_ready else None,
        "averageResponseMs": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "totalTurns": total_turns, "tagReport": tag_report, "results": case_results,
    }


# ---------------------------------------------------------------------------
# 真实脱敏订单语料（v0.11.0 起支持）
#
# 合成语料由实现同一套规则的人编写，不能证明真实用户话术下的表现。把脱敏后的
# 真实订单放进 tests/eval_cases_real.json（格式见该文件内说明），本脚本会单独
# 运行并单独报告。语料达到 REAL_CORPUS_MIN_CASES 例后启用硬门槛：字段准确率
# 低于 REAL_CORPUS_ACCURACY_GATE 时以非零码退出（CI 失败）。
# ---------------------------------------------------------------------------

REAL_CORPUS_PATH = Path(__file__).resolve().parent / "eval_cases_real.json"
REAL_CORPUS_MIN_CASES = 20
REAL_CORPUS_ACCURACY_GATE = 95


def load_real_cases() -> tuple[list[dict[str, Any]], str]:
    """Return (cases, status); status is ok | missing | invalid | empty."""
    if not REAL_CORPUS_PATH.is_file():
        return [], "missing"
    try:
        data = json.loads(REAL_CORPUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], "invalid"
    raw = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return [], "invalid"
    cases = [case for case in raw
             if isinstance(case, dict) and case.get("turns") and case.get("expected")]
    return cases, ("ok" if cases else "empty")


def real_corpus_verdict(real_cases: list[dict[str, Any]], real_report: dict[str, Any] | None,
                        real_status: str) -> str:
    """Decide the real-corpus gate: fail | pending | record | pass.

    ``fail`` blocks the release exit code; ``pending`` means no usable corpus
    yet; ``record`` means fewer than REAL_CORPUS_MIN_CASES cases (report only,
    no gate); ``pass`` means the corpus is large enough and met the gate.
    """
    if real_status == "invalid":
        return "fail"
    if not real_cases:
        return "pending"
    if len(real_cases) < REAL_CORPUS_MIN_CASES:
        return "record"
    if real_report is not None and real_report["fieldAccuracy"] < REAL_CORPUS_ACCURACY_GATE:
        return "fail"
    return "pass"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic PrintOps Agent evaluation suite")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    parser.add_argument("--only-failed", action="store_true", help="只打印未通过的用例")
    args = parser.parse_args()
    report = run_suite()
    real_cases, real_status = load_real_cases()
    real_report = run_suite(real_cases) if real_cases else None
    verdict = real_corpus_verdict(real_cases, real_report, real_status)

    def failed_cases(suite: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in suite["results"] if item["fieldAccuracy"] < 100]

    if args.json:
        print(json.dumps({**report, "realCorpus": real_report, "realCorpusVerdict": verdict},
                         ensure_ascii=False, indent=2))
    else:
        print(f"PrintOps evaluation: {report['cases']} cases")
        print(f"字段准确率：{report['fieldAccuracy']}%")
        print(f"可完成用例的基础订单完整率：{report['completionRate']}%（{report['expectedCompleteCases']} 例）")
        print(f"平均达到基础完整的轮数：{report['averageTurnsToReady']}")
        print(f"平均响应耗时：{report['averageResponseMs']}ms")
        for tag, stats in report["tagReport"].items():
            print(f"- [{tag}] {stats['cases']} 例，字段准确率 {stats['fieldAccuracy']}%")
        for label, suite in (("合成语料", report), ("真实语料", real_report)):
            if suite is None:
                continue
            failed = failed_cases(suite)
            if failed:
                print(f"[{label}] 未通过用例：{len(failed)}")
                for item in failed:
                    bad = [c for c in item["checks"] if not c["ok"]]
                    detail = "；".join(f"{c['field']} 期望 {c['expected']!r} 实际 {c['actual']!r}" for c in bad)
                    print(f"- [{item['tag']}] {item['name']}：{detail}")
        if verdict == "record":
            print(f"真实脱敏语料：{real_report['cases']} 例，字段准确率 {real_report['fieldAccuracy']}%，"
                  f"基础订单完整率 {real_report['completionRate']}%"
                  f"（不足 {REAL_CORPUS_MIN_CASES} 例，本轮仅记录，不设门槛）")
        elif verdict == "pass":
            print(f"真实脱敏语料：{real_report['cases']} 例，字段准确率 {real_report['fieldAccuracy']}%，"
                  f"达到 ≥{REAL_CORPUS_ACCURACY_GATE}% 门槛。")
        elif verdict == "fail":
            print(f"真实脱敏语料：未达门槛"
                  + (f"（{real_report['cases']} 例，字段准确率 {real_report['fieldAccuracy']}%，"
                     f"要求 ≥{REAL_CORPUS_ACCURACY_GATE}%）" if real_report else "（文件格式无效）。"))
        else:
            print(f"真实脱敏语料：待补充（放入 tests/eval_cases_real.json，≥{REAL_CORPUS_MIN_CASES} 例"
                  f"后启用字段准确率 ≥{REAL_CORPUS_ACCURACY_GATE}% 门槛）。")

    if report["fieldAccuracy"] < 90 or report["completionRate"] < 80:
        return 1
    if verdict == "fail":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
