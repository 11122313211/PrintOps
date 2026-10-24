"""Versioned, JSON-safe contracts for local reference estimates.

Amounts travel as two-decimal strings. Decimal arithmetic is never converted
to binary floating point at the transport boundary.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

PRICING_SCHEMA_VERSION = "2.0"
CENT = Decimal("0.01")
DISCLAIMER = "示例价格参数表仅用于本地参考估算，不构成供应商报价；实际以供应商回复为准。"
# The engine only publishes illustrative, hand-maintained reference figures.
# Tightening this to a closed enum keeps a future "market price" relabel from
# sneaking in through the schema instead of through an explicit redesign.
CONFIDENCE_LEVELS = ("illustrative",)


def decimal_value(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("金额或数量必须是有限十进制数")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("无效十进制数") from error
    if not number.is_finite():
        raise ValueError("金额或数量必须有限")
    return number


def money(value: Any) -> str:
    return format(decimal_value(value).quantize(CENT, rounding=ROUND_HALF_UP), ".2f")


def rate_basis(card: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "local_reference", **{
        key: card.get(key) for key in (
            "rateCardId", "version", "region", "sourceType", "sourceDescription",
            "effectiveFrom", "expiresAt", "confidence", "enteredBy", "taxBasis",
            "deliveryBasis")}, "isSupplierPrice": False}


def validate_rate_card(card: dict[str, Any]) -> None:
    required = ("rateCardId", "version", "region", "currency", "unitSystem",
                "sourceType", "sourceDescription", "effectiveFrom", "expiresAt",
                "confidence", "enteredBy", "taxBasis", "deliveryBasis", "categories")
    if not isinstance(card, dict) or any(key not in card for key in required):
        raise ValueError("rate card 缺少必需来源或版本字段")
    if card["currency"] != "CNY" or card["unitSystem"] != "mm":
        raise ValueError("rate card 只支持 CNY/mm")
    for key in ("rateCardId", "version", "region", "sourceType", "sourceDescription", "confidence", "enteredBy"):
        if not isinstance(card[key], str) or not card[key].strip():
            raise ValueError(f"rate card {key} 不能为空")
    if card["confidence"] not in CONFIDENCE_LEVELS:
        raise ValueError("rate card confidence 仅支持 " + "/".join(CONFIDENCE_LEVELS))
    if card["sourceType"] != "local_reference" or card.get("isSupplierPrice") is not False:
        raise ValueError("本地估算引擎只接受明确非供应商实价的 local_reference 成本表")
    if card["taxBasis"] != "exclusive" or card["deliveryBasis"] != "ex_works":
        raise ValueError("成本表必须采用不含税、出厂口径")
    for key in ("effectiveFrom", "expiresAt"):
        if key == "expiresAt" and card[key] is None:
            continue
        if not isinstance(card[key], str):
            raise ValueError(f"rate card {key} 必须为 ISO 日期")
        date.fromisoformat(card[key])
    if card["expiresAt"] is not None and card["expiresAt"] < card["effectiveFrom"]:
        raise ValueError("成本表到期日期不能早于生效日期")
    sources = card.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ValueError("rate card 必须包含来源记录")
    for source_id, source in sources.items():
        if not isinstance(source, dict) or not str(source_id).strip() or not source.get("description") or not source.get("enteredBy"):
            raise ValueError(f"来源记录 {source_id} 必须含 id、说明与录入人")
        if "confidence" in source and source["confidence"] not in CONFIDENCE_LEVELS:
            raise ValueError(f"来源 {source_id} 的 confidence 仅支持 " + "/".join(CONFIDENCE_LEVELS))

    def validate_source(rate, path):
        if not isinstance(rate, dict):
            raise ValueError(f"{path} 必须为对象")
        source_id = rate.get("sourceId")
        source = sources.get(source_id) if isinstance(source_id, str) else None
        if not isinstance(source, dict) or not source.get("description") or not source.get("enteredBy"):
            raise ValueError(f"{path} 必须引用含说明与录入人的有效来源")
        if "confidence" in rate and rate["confidence"] not in CONFIDENCE_LEVELS:
            raise ValueError(f"{path} 的 confidence 仅支持 " + "/".join(CONFIDENCE_LEVELS))

    if not isinstance(card["categories"], dict) or not card["categories"]:
        raise ValueError("rate card 必须按品类声明覆盖范围")
    for section in ("materials", "printing", "finishing", "binding"):
        if not isinstance(card.get(section), dict):
            raise ValueError(f"rate card 缺少 {section}")
        for name, rate in card[section].items():
            validate_source(rate, f"{section}.{name}")
            needed = ("setup", "perUnit") if section == "binding" else ("setup", "perM2")
            if section == "materials":
                if sum(key in rate for key in ("perKg", "perM2")) != 1:
                    raise ValueError(f"materials.{name} 必须明确一种材料计价单位")
            elif any(key not in rate for key in needed):
                raise ValueError(f"{section}.{name} 缺少完整准备费/运行费参数")
            for key in ("perKg", "perM2", "setup", "perUnit"):
                if key in rate and decimal_value(rate[key]) < 0:
                    raise ValueError(f"{section}.{name}.{key} 不能为负数")
    for name, category in card["categories"].items():
        if (not isinstance(category, dict) or not isinstance(category.get("requiredDrivers"), list)
                or any(not isinstance(driver, str) or not driver for driver in category["requiredDrivers"])):
            raise ValueError(f"{name} 必须声明 requiredDrivers")
        validate_source(category, f"categories.{name}")
        if category.get("supported"):
            if category.get("family") not in {"flat", "folded", "booklet", "box", "label", "display"}:
                raise ValueError(f"{name} 未声明可计算的品类成本驱动")
            if not isinstance(category.get("units"), list) or not category["units"] or any(
                    unit not in {"张", "份", "本", "个", "块", "套"} for unit in category["units"]):
                raise ValueError(f"{name} 必须声明计数单位")
            if not {"quantity", "dimensions", "material", "printing"}.issubset(category["requiredDrivers"]):
                raise ValueError(f"{name} 缺少基础成本驱动")
            for key in ("minimumTotal", "packingPerUnit", "wasteFactor"):
                if decimal_value(category.get(key)) < 0:
                    raise ValueError(f"{name}.{key} 不能为负数")
            if decimal_value(category["wasteFactor"]) < 1:
                raise ValueError(f"{name}.wasteFactor 不能低于1")
    band = card.get("band", {})
    if not Decimal("0") < decimal_value(band.get("low")) <= 1 <= decimal_value(band.get("high")):
        raise ValueError("价格区间必须满足 0 < low <= 1 <= high")
    for color in ("单色", "四色"):
        if decimal_value(card.get("colorFactors", {}).get(color)) <= 0:
            raise ValueError("颜色成本系数必须为正数")


ESTIMATE_OUTPUT_SCHEMA = {
    "type": "object", "properties": {
        "type": {"const": "estimate"},
        "status": {"enum": ["ready", "partial", "blocked"]},
        "range": {"type": ["object", "null"], "properties": {
            key: {"type": "string", "pattern": r"^\d+\.\d{2}$"}
            for key in ("low", "mid", "high")}},
        "displayRange": {"type": "string"},
        "missing": {"type": "array"}, "lineItems": {"type": "array"},
        "basis": {"type": "object"}, "coverage": {"type": "object"},
    },
}
