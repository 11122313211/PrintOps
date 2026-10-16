"""Deterministic, explainable local cost estimates. No network or supplier quotes."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

from pricing_schema import (DISCLAIMER, PRICING_SCHEMA_VERSION, decimal_value,
                            money, rate_basis, validate_rate_card)
from unit_normalizer import dimension_values
from domain_vocab import normalize_print_term

DEFAULT_RATE_CARD = Path(__file__).resolve().parent / "rate_cards" / "cn-local-reference.json"
DRIVER_LABELS = {"quantity": "数量（有效正整数和单位）", "dimensions": "计价平面尺寸",
                 "material": "已标定材料及纸张克重", "printing": "印刷面数和颜色",
                 "pages": "页数（含封面）", "binding": "装订方式", "folding": "折页方式",
                 "boxStructure": "已标定盒型", "labelShape": "标签形状",
                 "adhesive": "标签胶型", "labelFormat": "标签交付形式（张装/卷装）",
                 "install": "安装加工方式", "categoryRates": "此品类的成本参数"}


def load_rate_card(path: str | Path | None = None) -> dict[str, Any]:
    card = json.loads(Path(path or DEFAULT_RATE_CARD).read_text(encoding="utf-8"))
    validate_rate_card(card)
    return card


def _measurement(value: Any) -> tuple[Decimal, Decimal] | None:
    if isinstance(value, dict):
        try:
            if value.get("depth") not in (None, "", 0, "0"):
                return None
            factor = {"mm": Decimal(1), "cm": Decimal(10), "in": Decimal("25.4")}.get(str(value.get("unit", "")).lower())
            width, height = decimal_value(value.get("width")), decimal_value(value.get("height"))
            if factor is not None and 0 < width <= 100000 and 0 < height <= 100000:
                return width * factor, height * factor
        except ValueError:
            return None
    values = dimension_values(value)
    return values if values is not None and len(values) == 2 else None


def _dimensions(order: dict[str, Any], family: str) -> tuple[Decimal, Decimal] | None:
    # Packaging needs actual flat dieline/expanded area. L×W×H is not enough
    # to infer the material usage of a structural product.
    keys = ("dieCutSize", "expandedSize") if family == "box" else ("expandedSize",) if family == "folded" else ("finishedSize",)
    measurements = order.get("measurements") or {}
    dimensions = order.get("dimensions") or {}
    for key in keys:
        for collection in (measurements, dimensions):
            if isinstance(collection, dict) and (parsed := _measurement(collection.get(key))):
                return parsed
    if isinstance(measurements, dict) and "width" in measurements:
        semantic = str(measurements.get("semantic", ""))
        if (family not in {"box", "folded"} and "package" not in semantic) or any(word in semantic for word in ("expanded", "die_cut")):
            if parsed := _measurement(measurements):
                return parsed
    return _measurement(order.get("size")) if family not in {"box", "folded"} else None


def _quantity(order: dict[str, Any]) -> Decimal | None:
    raw = order.get("quantityValue")
    if raw in (None, ""):
        text = str(order.get("quantity") or "").replace(",", "").strip()
        match = re.fullmatch(r"(\d+)\s*(张|份|本|个|块|套)?", text)
        raw = match[1] if match else None
    try:
        value = decimal_value(raw)
        return value if value == value.to_integral_value() and 0 < value <= 100000000 else None
    except ValueError:
        return None


def _paper(text: Any, card: dict[str, Any]) -> tuple[str, Decimal | None, dict[str, Any]] | None:
    text = str(text or "").strip()
    # Substring matching does not turn an unknown substrate or generic paper
    # preference into a calibrated material.
    compact = re.sub(r"\s+", "", text)
    for name in sorted(card["materials"], key=len, reverse=True):
        rate = card["materials"][name]
        if "perKg" in rate:
            match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:g/m²|g/m2|g|克)(?:/平方米)?" + re.escape(name), compact, re.I)
            if match and 20 <= Decimal(match[1]) <= 1000:
                return name, Decimal(match[1]), rate
        elif compact == re.sub(r"\s+", "", name):
            return name, None, rate
    return None


def _processes(value: Any) -> tuple[list[str], list[str]]:
    text = str(value or "").strip()
    if not text or text in {"无特殊工艺", "无特殊表面工艺", "无需", "无", "无工艺", "不覆膜"}:
        return [], []
    parts = [part.strip().replace("局部 UV", "局部UV") for part in re.split(r"[、，,+＋/;；]+", text) if part.strip()]
    return list(dict.fromkeys(parts)), []


def estimate_order(order: dict[str, Any], *, rate_card: dict[str, Any] | None = None,
                   as_of: date | None = None) -> dict[str, Any]:
    card = deepcopy(rate_card) if rate_card is not None else load_rate_card()
    validate_rate_card(card)
    today = as_of or date.today()
    product = str(order.get("productType") or "")
    category = card["categories"].get(product, {"supported": False, "requiredDrivers": ["categoryRates"]})
    required = list(category["requiredDrivers"])
    covered: set[str] = set()
    missing: list[str] = []
    unknown: list[str] = []
    assumptions: list[str] = []
    result: dict[str, Any] = {
        "schemaVersion": PRICING_SCHEMA_VERSION, "type": "estimate", "status": "blocked",
        "currency": "CNY", "range": None, "displayRange": "待补充计价参数", "lineItems": [],
        "basis": rate_basis(card), "missing": missing, "assumptions": assumptions,
        "includes": ["材料", "常规损耗", "印刷准备和运行", "已标定后道", "常规包装"],
        "excludes": ["税费", "运输", "打样", "文件修改", "安装施工"],
        "disclaimer": DISCLAIMER, "pricingModelVersion": card["version"],
        "requiresHumanConfirmation": True,
    }

    def finish(status: str) -> dict[str, Any]:
        absent = [key for key in required if key not in covered]
        for key in absent:
            label = DRIVER_LABELS.get(key, key)
            if label not in missing:
                missing.append(label)
        result["status"] = status
        result["coverage"] = {"category": product or "未知品类", "supported": bool(category.get("supported")),
                              "requiredDrivers": required, "coveredDrivers": sorted(covered),
                              "missingDrivers": absent, "unknownProcesses": list(dict.fromkeys(unknown)),
                              "ratio": round(len(covered.intersection(required)) / max(1, len(required)), 3)}
        if status != "ready":
            result["displayRange"] = "部分成本已核算，待补充参数" if status == "partial" else "暂不能估算"
            result["unpriced"] = list(dict.fromkeys(unknown))
        return result

    if today < date.fromisoformat(card["effectiveFrom"]) or (card["expiresAt"] and today > date.fromisoformat(card["expiresAt"])):
        missing.append("有效价格参数表（当前版本未生效或已过期）")
        result["reason"] = "rate_card_out_of_date"
        return finish("blocked")
    if not category.get("supported"):
        result["reason"] = "category_uncalibrated"
        return finish("blocked")
    family = category["family"]
    specs = order.get("productSpecs") if isinstance(order.get("productSpecs"), dict) else {}
    quantity = _quantity(order)
    unit = str(order.get("quantityUnit") or "")
    if not unit:
        match = re.search(r"(张|份|本|个|块|套)\s*$", str(order.get("quantity") or ""))
        unit = match[1] if match else ""
    accepted_units = category.get("units", [])
    if quantity is not None and unit in accepted_units:
        covered.add("quantity")
    else:
        if unit and unit not in accepted_units:
            missing.append(f"{product}数量单位须为{' / '.join(accepted_units)}；收到“{unit}”，请确认实际计数，不自动换算")
            result["reason"] = "quantity_unit_incompatible"
            result["quantityValidation"] = {"receivedUnit": unit, "acceptedUnits": list(accepted_units), "converted": False}
        quantity = None
    dims = _dimensions(order, family)
    if dims:
        covered.add("dimensions")
    else:
        missing.append("展开/刀模平面尺寸（mm）" if family in {"box", "folded"} else "成品平面尺寸（mm）")
    material_text = normalize_print_term("paper", specs.get("labelMaterial") if family == "label" else specs.get("displayMaterial") if family == "display" else order.get("paper"))
    material = _paper(material_text, card)
    if material:
        covered.add("material")
    printing = str(order.get("printing") or "")
    # This is a cost-driver contract, not a second natural-language parser.
    # Alternatives, negations and additional inks must be resolved upstream.
    print_match = re.fullmatch(r"(单面|双面|两面)(四色|彩印|彩色|黑白|单色)(?:印刷)?", re.sub(r"\s+", "", printing))
    sides = (Decimal(1) if print_match[1] == "单面" else Decimal(2)) if print_match else None
    color_key = ("单色" if print_match[2] in {"黑白", "单色"} else "四色") if print_match else None
    colors = decimal_value(card["colorFactors"][color_key]) if color_key else None
    spec_color = str(specs.get("cardColor") or "")
    unsupported_color = spec_color not in {"", "四色", "彩色", "彩印", "黑白", "单色"}
    color_conflict = bool(spec_color and not unsupported_color and color_key and
                          ("单色" if spec_color in {"黑白", "单色"} else "四色") != color_key)
    if print_match and not unsupported_color and not color_conflict:
        covered.add("printing")
    else:
        if printing and not print_match:
            missing.append("明确一种印刷面数和颜色（例如双面四色）；不能包含否定、备选或额外印色要求")
        if unsupported_color:
            unknown.append(f"名片印色未标定：{spec_color}")
        if color_conflict:
            missing.append("印刷颜色与名片专属印色要求不一致，请确认")
        if any(term in printing for term in ("专色", "白墨", "金墨", "银墨")):
            unknown.append("专色或额外印色未标定")
    selected = order.get("selectedOption") if isinstance(order.get("selectedOption"), dict) else {}
    mode = str(order.get("printMode") or selected.get("printMode") or "合版")
    if not order.get("printMode") and not selected.get("printMode"):
        assumptions.append("印刷方式未指定，按合版模拟；不是供应商已确认的生产方式。")
    print_rate = card["printing"].get(mode)
    if not print_rate:
        unknown.append(f"印刷方式未标定：{mode}")
    pages = Decimal(2)
    if family == "booklet":
        page_match = re.fullmatch(r"\s*(\d+)\s*(?:页|P)?\s*", str(order.get("pages") or ""), re.I)
        if page_match and 4 <= int(page_match[1]) <= 2000 and int(page_match[1]) % 2 == 0:
            pages = Decimal(page_match[1]); covered.add("pages")
            assumptions.append("页数按含封面的总页数计算；封面未独立指定时按正文同纸。")
        else:
            missing.append("有效总页数（含封面，正偶数）")
    binding = str(order.get("binding") or specs.get("binding") or "")
    if family == "booklet":
        if binding in {"骑马钉", "胶装", "锁线胶装"} and binding in card["binding"]:
            covered.add("binding")
        else:
            unknown.append(f"装订未标定：{binding or '未填写'}")
        if binding == "骑马钉" and pages % 4:
            missing.append("骑马钉总页数须为4的倍数")
    if family == "folded":
        folding = str(specs.get("folding") or "")
        if folding in {"二折", "三折", "风琴折"}:
            covered.add("folding")
        else:
            unknown.append(f"折页方式未标定：{folding or '未填写'}")
    if family == "box":
        if specs.get("boxStructure") in category.get("supportedStructures", []):
            covered.add("boxStructure")
        else:
            unknown.append(f"盒型未标定：{specs.get('boxStructure') or '未填写'}")
    if family == "label":
        for key, allowed in {"labelShape": {"方形", "矩形", "圆形"}, "adhesive": {"普通胶", "永久胶"}, "labelFormat": {"张装"}}.items():
            if specs.get(key) in allowed:
                covered.add(key)
            else:
                unknown.append(f"{DRIVER_LABELS[key]}未标定：{specs.get(key) or '未填写'}")
    if family == "display":
        install = str(specs.get("install") or "")
        if install in {"无需安装", "无", "自行安装", "仅画面", "墙面张贴"}:
            covered.add("install")
        else:
            unknown.append(f"安装加工未标定：{install or '未填写'}")
    core = {"quantity", "dimensions", "material", "printing"}
    if not core.issubset(covered) or (family == "booklet" and "pages" not in covered) or not print_rate:
        return finish("blocked")
    width, height = dims
    area = width * height / Decimal(1000000)
    sheets = (pages / sides).to_integral_value(rounding=ROUND_CEILING) if family == "booklet" else Decimal(1)
    paper_area = quantity * area * sheets
    waste = decimal_value(category["wasteFactor"])
    result["drivers"] = {"quantity": str(quantity), "unit": unit, "widthMm": str(width), "heightMm": str(height),
                         "areaM2PerSheet": str(area), "sheetsPerUnit": str(sheets), "printingSides": str(sides),
                         "pages": str(pages) if family == "booklet" else None, "printMode": mode, "wasteFactor": str(waste)}
    rows = result["lineItems"]

    def add(code: str, name: str, amount: Decimal, basis: str, rate: dict[str, Any], qty: Decimal, rate_unit: str, unit_price: Decimal) -> None:
        source = deepcopy(card.get("sources", {}).get(rate.get("sourceId"), {}))
        source.update({"sourceId": rate.get("sourceId"), "rateCardId": card["rateCardId"], "version": card["version"],
                       "region": card["region"], "effectiveFrom": card["effectiveFrom"], "expiresAt": card["expiresAt"],
                       "confidence": card["confidence"], "category": product, "isSupplierPrice": False})
        rows.append({"code": code, "name": name, "amount": money(amount), "basis": basis,
                     "quantity": str(qty), "unit": rate_unit, "unitPrice": str(unit_price), "rateSource": source})

    def material_cost(text: str, parsed: tuple, usage: Decimal, code: str = "material") -> None:
        name, gsm, rate = parsed
        amount_qty = usage * waste * gsm / 1000 if gsm is not None else usage * waste
        price = decimal_value(rate.get("perKg", rate.get("perM2")))
        add(code, "封面材料成本" if code == "cover_material" else "材料成本", amount_qty * price,
            f"{text}；{width}×{height} mm；用量 {usage} m² × 损耗 {waste}", rate, amount_qty, "kg" if gsm is not None else "m²", price)

    cover_text = normalize_print_term("coverPaper", str(specs.get("coverPaper") or ""))
    cover = _paper(cover_text, card) if cover_text else None
    if family == "booklet" and cover_text and not cover:
        unknown.append(f"封面材料未标定：{cover_text}")
    if family == "booklet" and cover:
        cover_area = quantity * area * (Decimal(4) / sides).to_integral_value(rounding=ROUND_CEILING)
        material_cost(str(material_text), material, max(Decimal(0), paper_area - cover_area))
        material_cost(cover_text, cover, cover_area, "cover_material")
    else:
        material_cost(str(material_text), material, paper_area)
    setup = decimal_value(print_rate["setup"])
    add("print_setup", "印刷准备费", setup, f"{mode}一次开机准备", print_rate, Decimal(1), "次", setup)
    impressions = quantity * area * (pages if family == "booklet" else sides)
    run_price = decimal_value(print_rate["perM2"]) * colors
    add("print_run", "印刷运行费", impressions * run_price, f"{printing}；计印面积 {impressions} m²", print_rate, impressions, "m²·面", run_price)
    finishes, _ = _processes(order.get("finishing"))
    if not order.get("finishing"):
        assumptions.append("未填写表面工艺，当前按无表面工艺模拟；新增工艺会重新计价。")
    # Book finishing is priced on the outside cover faces. Flat products use
    # one decorated face; this explicit assumption avoids silently doubling it.
    finish_area = quantity * area * (Decimal(2) if family == "booklet" else Decimal(1))
    if finishes:
        assumptions.append("表面工艺按书刊封面外侧2面/其他物料1面全幅计算；局部覆盖率未单独标定。")
    for process in finishes:
        rate = card["finishing"].get(process)
        if rate is None:
            unknown.append(f"表面工艺未标定：{process}"); continue
        amount = decimal_value(rate["setup"]) + finish_area * decimal_value(rate["perM2"])
        add(f"finishing:{process}", f"{process}工艺费", amount, f"准备费 {rate['setup']} + {finish_area} m² × {rate['perM2']}", rate, finish_area, "m²", decimal_value(rate["perM2"]))
    post = []
    if binding and binding not in {"无需装订", "无", "无需"}:
        post.append(binding)
    if family == "folded" and "folding" in covered:
        post.append(str(specs["folding"]))
    if family == "box":
        post.extend(["模切", "糊盒"])
    if family == "label":
        post.append("模切")
    if specs.get("cardCorners") and specs.get("cardCorners") not in {"直角", "无需", "无"}:
        post.append("圆角" if "圆角" in str(specs["cardCorners"]) else str(specs["cardCorners"]))
    for process in dict.fromkeys(post):
        rate = card["binding"].get(process)
        if rate is None:
            unknown.append(f"后道未标定：{process}"); continue
        price = decimal_value(rate["perUnit"])
        add(f"binding:{process}", f"{process}后道费", decimal_value(rate["setup"]) + quantity * price,
            f"准备费 {rate['setup']} + {quantity} × {price}", rate, quantity, unit, price)
    pack = decimal_value(category["packingPerUnit"])
    add("packing", "常规包装费", quantity * pack, f"{quantity} × {pack}", category, quantity, unit, pack)
    # Sum rounded lines so the user can reproduce the displayed total exactly.
    subtotal = sum((decimal_value(row["amount"]) for row in rows), Decimal(0))
    result["knownSubtotal"] = money(subtotal)
    if set(required) - covered or unknown or missing:
        return finish("partial")
    minimum = decimal_value(category["minimumTotal"])
    total = max(subtotal, minimum)
    if total > subtotal:
        add("minimum_adjustment", "最低收费补差", total - subtotal,
            f"max(成本合计 {money(subtotal)}, 最低收费 {money(minimum)})；仅计差额", category, Decimal(1), "单", total - subtotal)
    result["range"] = {"low": money(max(minimum, total * decimal_value(card["band"]["low"]))), "mid": money(total), "high": money(total * decimal_value(card["band"]["high"]))}
    result["displayRange"] = f"¥{result['range']['low']} - ¥{result['range']['high']}"
    return finish("ready")
