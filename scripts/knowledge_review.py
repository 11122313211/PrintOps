#!/usr/bin/env python3
"""Knowledge review gate: manifest freshness, rate card window, coverage.

Pure local file analysis; performs no network requests.  Reports the knowledge
manifest's review cadence state, the active rate card's validity window, the
category coverage gap between PRODUCT_CATALOG and the rate card, and the
completeness of both sides' source records.

Exit codes: 0 = structural checks pass (review-cadence and expiry warnings are
printed but do not fail the gate), 1 = structural inconsistency (coverage gap,
incomplete source records, unreadable or invalid rate card, manifest drift),
2 = usage error.  ``--today`` and ``--rate-card`` exist so tests can inject
state without touching the repository.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stdio_utf8
from pricing_schema import validate_rate_card
from product_knowledge import (KNOWLEDGE_MANIFEST, PRODUCT_CATALOG,
                               RATE_CARD_PATH, REVIEW_OVERDUE_GRACE_DAYS,
                               knowledge_review_status)

EXPIRY_WARNING_DAYS = 60


def read_rate_card(path: Path) -> tuple[dict | None, str]:
    """Load and schema-validate a rate card without raising."""
    try:
        card = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, f"rate card 不可读（{path}）：{error}"
    try:
        validate_rate_card(card)
    except ValueError as error:
        return None, f"rate card 校验失败（{path}）：{error}"
    return card, ""


def build_report(*, today: date | None = None,
                 rate_card_path: Path | None = None) -> dict:
    """Assemble the full knowledge-review report as a JSON-safe dict."""
    today = today or date.today()
    path = Path(rate_card_path) if rate_card_path else RATE_CARD_PATH
    status = knowledge_review_status(today)
    problems: list[str] = []
    warnings: list[str] = []

    if status["declaredNextReviewAt"] != status["nextReviewAt"]:
        problems.append(
            "manifest nextReviewAt（{0}）与 lastReviewed+cadence 推导值（{1}）不一致".format(
                status["declaredNextReviewAt"] or "未声明", status["nextReviewAt"]))
    if status["status"] == "due":
        warnings.append(
            "知识库已到复核期：上次复核 {0}，周期 {1} 天，请安排人工复核".format(
                status["lastReviewed"], status["reviewCadenceDays"]))
    elif status["status"] == "overdue":
        warnings.append(
            "知识库复核已逾期：上次复核 {0}，已超过周期与 {1} 天宽限期，必须尽快复核".format(
                status["lastReviewed"], REVIEW_OVERDUE_GRACE_DAYS))

    card, error = read_rate_card(path)
    card_info: dict = {"path": str(path), "readable": card is not None,
                       "version": None, "effectiveFrom": None,
                       "expiresAt": None, "daysUntilExpiry": None}
    coverage_gap: dict = {"catalogOnly": [], "rateCardOnly": []}
    manifest_source_ids: list[str] = []
    card_source_ids: list[str] = []
    if card is None:
        problems.append(error)
    else:
        card_info.update({key: card.get(key) for key in
                          ("version", "effectiveFrom", "expiresAt")})
        if card.get("expiresAt"):
            days_left = (date.fromisoformat(card["expiresAt"]) - today).days
            card_info["daysUntilExpiry"] = days_left
            if days_left < 0:
                warnings.append(
                    "rate card {0} 已于 {1} 过期（余量 {2} 天），本地估算将被阻断".format(
                        card["version"], card["expiresAt"], days_left))
            elif days_left <= EXPIRY_WARNING_DAYS:
                warnings.append(
                    "rate card {0} 距到期仅 {1} 天（{2}），请安排换版".format(
                        card["version"], days_left, card["expiresAt"]))

        catalog = set(PRODUCT_CATALOG)
        covered = set(card.get("categories") or {})
        coverage_gap = {"catalogOnly": sorted(catalog - covered),
                        "rateCardOnly": sorted(covered - catalog)}
        if coverage_gap["catalogOnly"] or coverage_gap["rateCardOnly"]:
            problems.append("品类覆盖差集非空：" + json.dumps(coverage_gap, ensure_ascii=False))

        for source in KNOWLEDGE_MANIFEST.get("sources", []):
            missing = [field for field in ("id", "title", "scope")
                       if not str(source.get(field) or "").strip()]
            if missing:
                problems.append("知识来源 {0} 缺少描述字段：{1}".format(
                    source.get("id") or "<缺失id>", "、".join(missing)))
            else:
                manifest_source_ids.append(str(source["id"]))
        for source_id, source in (card.get("sources") or {}).items():
            missing = [field for field in ("description", "enteredBy")
                       if not str(source.get(field) or "").strip()]
            if missing:
                problems.append("rate card 来源 {0} 缺少：{1}".format(
                    source_id, "、".join(missing)))
            else:
                card_source_ids.append(str(source_id))

    return {
        "today": today.isoformat(),
        "knowledge": status,
        "rateCard": card_info,
        "coverageGap": coverage_gap,
        "sources": {"manifest": sorted(manifest_source_ids),
                    "rateCard": sorted(card_source_ids)},
        "problems": problems,
        "warnings": warnings,
    }


def print_report(report: dict) -> None:
    knowledge = report["knowledge"]
    print("知识版本 {0}：上次复核 {1}，周期 {2} 天，下次复核 {3}，状态 {4}（距上次复核 {5} 天）".format(
        knowledge["knowledgeVersion"], knowledge["lastReviewed"],
        knowledge["reviewCadenceDays"], knowledge["nextReviewAt"],
        knowledge["status"], knowledge["daysSinceReview"]))
    card = report["rateCard"]
    if card.get("readable"):
        print("rate card {0}：生效 {1} — {2}，距到期 {3} 天".format(
            card["version"], card["effectiveFrom"], card["expiresAt"],
            card["daysUntilExpiry"]))
    else:
        print("rate card：不可读（{0}）".format(card["path"]))
    gap = report["coverageGap"]
    if gap["catalogOnly"] or gap["rateCardOnly"]:
        print("品类覆盖差集：目录未覆盖 {0}；rate card 未覆盖 {1}".format(
            gap["catalogOnly"] or "无", gap["rateCardOnly"] or "无"))
    else:
        print("品类覆盖差集：无（目录与 rate card 品类一致）")
    print("来源记录：manifest {0} 条 / rate card {1} 条".format(
        len(report["sources"]["manifest"]), len(report["sources"]["rateCard"])))
    for problem in report["problems"]:
        print("[问题] " + problem)
    for warning in report["warnings"]:
        print("[警告] " + warning)


def main() -> int:
    stdio_utf8.configure()
    parser = argparse.ArgumentParser(description="知识库复核时效与 rate card 覆盖检查（纯本地，无网络）")
    parser.add_argument("--today", default=None, help="注入核算日期（ISO 格式，默认今天；测试用）")
    parser.add_argument("--rate-card", default=None, help="替代默认 rate card 路径（测试用）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出完整报告")
    args = parser.parse_args()
    try:
        today = date.fromisoformat(args.today) if args.today else None
    except ValueError:
        print(f"无效日期：{args.today}", file=sys.stderr)
        return 2
    report = build_report(
        today=today,
        rate_card_path=Path(args.rate_card) if args.rate_card else None)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
