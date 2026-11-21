"""Versioned, JSON-safe contracts for local reference estimates.

Amounts travel as two-decimal strings. Decimal arithmetic is never converted
to binary floating point at the transport boundary.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

PRICING_SCHEMA_VERSION = "3.0"
CENT = Decimal("0.01")
DISCLAIMER = "示例价格参数表仅用于本地参考估算，不构成供应商报价；实际以供应商回复为准。"
# Closed enum, extended only through explicit redesign. "illustrative" covers
# the hand-maintained baseline rates the engine computes from; "market-listed"
# (2026-11-21, alongside the reference release tier) marks 公开挂牌参考价
# source records — publicly listed, unlogged prices that are neither deal
# prices nor authorized supplier quotes, carried as calibration evidence.
CONFIDENCE_LEVELS = ("illustrative", "market-listed")


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
                "confidence", "enteredBy", "taxBasis", "deliveryBasis", "categories",
                "schemaVersion")
    if card.get("schemaVersion") != PRICING_SCHEMA_VERSION:
        raise ValueError(f"rate card schemaVersion 必须为 {PRICING_SCHEMA_VERSION}")
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
            family = category["family"]
            if family not in {"flat", "folded", "booklet", "box", "label", "display", "cup", "form"}:
                raise ValueError(f"{name} 未声明可计算的品类成本驱动")
            if not isinstance(category.get("units"), list) or not category["units"] or any(
                    unit not in {"张", "份", "本", "个", "块", "套"} for unit in category["units"]):
                raise ValueError(f"{name} 必须声明计数单位")
            # Base drivers are order-supplied. The cup family prices a
            # calibrated capacity band instead of a flat dimension.
            base = {"quantity", "material", "printing"} | ({"dimensions"} if family != "cup" else {"cupVolume"})
            if not base.issubset(category["requiredDrivers"]):
                raise ValueError(f"{name} 缺少基础成本驱动")
            if family == "cup":
                cup_sizes = category.get("cupSizes")
                if not isinstance(cup_sizes, dict) or not cup_sizes:
                    raise ValueError(f"{name} 必须声明 cupSizes 容量档")
                for size_name, blank in cup_sizes.items():
                    if (not isinstance(blank, dict) or not str(size_name).strip()
                            or decimal_value(blank.get("blankAreaM2")) <= 0):
                        raise ValueError(f"{name}.cupSizes.{size_name} 必须声明正数 blankAreaM2")
            if family == "form":
                parts = category.get("paperParts")
                if (not isinstance(parts, dict) or not parts
                        or any(not str(label).strip() or isinstance(count, bool)
                               or not isinstance(count, int) or not 2 <= count <= 8
                               for label, count in parts.items())):
                    raise ValueError(f"{name} 必须声明 paperParts 联数档（标签到 2-8 联的映射）")
            if "boardThickness" in category["requiredDrivers"] or category.get("boardThicknesses") is not None:
                thicknesses = category.get("boardThicknesses")
                if not isinstance(thicknesses, dict) or not thicknesses:
                    raise ValueError(f"{name} 必须声明 boardThicknesses 板厚档")
                for thickness, spec in thicknesses.items():
                    if (not isinstance(spec, dict) or not str(thickness).strip()
                            or decimal_value(spec.get("materialFactor")) <= 0):
                        raise ValueError(f"{name}.boardThicknesses.{thickness} 必须声明正数 materialFactor")
            for key in ("minimumTotal", "packingPerUnit", "wasteFactor"):
                if decimal_value(category.get(key)) < 0:
                    raise ValueError(f"{name}.{key} 不能为负数")
            if decimal_value(category["wasteFactor"]) < 1:
                raise ValueError(f"{name}.wasteFactor 不能低于1")
            # Card-side cost components (schema 3.0). They add cost lines, not
            # order-supplied drivers, so they never enter requiredDrivers.
            imposition = category.get("imposition")
            if imposition is not None:
                per_sheet_up = imposition.get("perSheetUp")
                if (not isinstance(imposition, dict) or isinstance(per_sheet_up, bool)
                        or not isinstance(per_sheet_up, int) or per_sheet_up < 1
                        or decimal_value(imposition.get("sheetWidthMm")) <= 0
                        or decimal_value(imposition.get("sheetHeightMm")) <= 0
                        or not str(imposition.get("sheetName") or "").strip()):
                    raise ValueError(f"{name}.imposition 必须声明每张出成品数、大纸尺寸与 sheetName")
                validate_source(imposition, f"{name}.imposition")
                if family == "booklet":
                    raise ValueError(f"{name}.imposition 与书刊封面分开计料不兼容")
            batch = category.get("batchSetup")
            if batch is not None:
                if not isinstance(batch, dict) or decimal_value(batch.get("amount")) < 0:
                    raise ValueError(f"{name}.batchSetup 必须声明非负 amount")
                validate_source(batch, f"{name}.batchSetup")
            tiers = category.get("quantityTiers")
            if tiers is not None:
                if not isinstance(tiers, list) or not tiers:
                    raise ValueError(f"{name}.quantityTiers 必须为非空数组")
                previous: int | None = 0
                for index, tier in enumerate(tiers):
                    if not isinstance(tier, dict) or decimal_value(tier.get("perUnit")) < 0:
                        raise ValueError(f"{name}.quantityTiers[{index}] 必须为含非负 perUnit 的对象")
                    up_to = tier.get("upTo")
                    if up_to is None and index != len(tiers) - 1:
                        raise ValueError(f"{name}.quantityTiers 仅最后档允许 upTo 为空")
                    if up_to is not None and (isinstance(up_to, bool) or not isinstance(up_to, int) or up_to <= 0):
                        raise ValueError(f"{name}.quantityTiers[{index}].upTo 必须为正整数")
                    if up_to is not None and previous is not None and up_to <= previous:
                        raise ValueError(f"{name}.quantityTiers[{index}].upTo 必须严格递增")
                    previous = up_to
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
