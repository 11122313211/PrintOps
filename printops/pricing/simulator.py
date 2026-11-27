"""量价拐点模拟器与工艺 What-If 试算器（纯只读、零副作用、仅 Python 标准库）。

所有计算复用本地参考估算引擎 ``estimate_order()``，严格使用 ``Decimal`` 精度运算，
绝不原地修改调用方传入的订单字典；输出附带非供应商正式报价的边界声明。
"""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from printops.domain.vocab import normalize_print_term
from printops.pricing.engine import (
    _paper,
    _processes,
    _quantity,
    estimate_order,
    load_rate_card,
)
from printops.pricing.schema import CENT, DISCLAIMER, decimal_value, validate_rate_card

# 数量合法边界（单次试算正整数范围）
MIN_SIM_QUANTITY = 1
MAX_SIM_QUANTITY = 10_000_000
MAX_CUSTOM_TIERS = 8
MAX_OVERRIDE_STR_LEN = 128

# 白名单顶层覆盖键与规格子键
ALLOWED_OVERRIDE_KEYS: frozenset[str] = frozenset({
    "quantity",
    "paper",
    "finishing",
    "binding",
    "printMode",
    "pages",
    "printing",
    "productSpecs",
})

ALLOWED_SPEC_OVERRIDE_KEYS: frozenset[str] = frozenset({
    "labelMaterial",
    "displayMaterial",
    "coverPaper",
    "folding",
    "boxStructure",
    "labelShape",
    "adhesive",
    "labelFormat",
    "install",
    "cardCorners",
    "cupVolume",
    "paperParts",
    "boardThickness",
})

# 按品类预设的真实印刷采购阶梯数量候选
DEFAULT_TIER_PRESETS: dict[str, tuple[int, ...]] = {
    "名片": (100, 200, 500, 1000, 2000, 5000),
    "PVC卡": (100, 200, 500, 1000, 2000, 5000),
    "单页": (100, 200, 500, 1000, 2000, 5000),
    "折页": (100, 200, 500, 1000, 2000, 5000),
    "邀请函": (100, 200, 500, 1000, 2000, 5000),
    "吊牌": (100, 200, 500, 1000, 2000, 5000),
    "信封封套": (100, 200, 500, 1000, 2000, 5000),
    "信封": (100, 200, 500, 1000, 2000, 5000),
    "数码印刷": (100, 200, 500, 1000, 2000, 5000),
    "宣传册": (50, 100, 200, 500, 1000),
    "画册": (50, 100, 200, 500, 1000),
    "包装盒": (100, 300, 500, 1000, 2000),
    "手提袋": (100, 300, 500, 1000, 2000),
    "标签": (200, 500, 1000, 3000, 5000),
    "海报": (1, 5, 10, 20, 50, 100),
    "喷画": (1, 5, 10, 20, 50, 100),
    "PVC": (1, 5, 10, 20, 50, 100),
    "纸杯": (500, 1000, 3000, 5000, 10000),
    "联单": (10, 20, 50, 100, 200),
}

# 按品类 family 回退的阶梯数量候选
FAMILY_TIER_PRESETS: dict[str, tuple[int, ...]] = {
    "flat": (100, 200, 500, 1000, 2000, 5000),
    "folded": (100, 200, 500, 1000, 2000, 5000),
    "booklet": (50, 100, 200, 500, 1000),
    "box": (100, 300, 500, 1000, 2000),
    "label": (200, 500, 1000, 3000, 5000),
    "display": (1, 5, 10, 20, 50, 100),
    "cup": (500, 1000, 3000, 5000, 10000),
    "form": (10, 20, 50, 100, 200),
}

# 全局标准采购整档（用于当前数量超出预设范围时动态扩展）
_GLOBAL_LADDER: tuple[int, ...] = (
    1, 2, 5, 10, 20, 30, 50, 100, 200, 300, 500,
    1000, 2000, 3000, 5000, 10000, 20000, 30000, 50000,
    100000, 200000, 500000, 1000000, 2000000, 5000000, 10000000,
)

# 规格字段中文显示名（供前端动态渲染控件使用）
SPEC_LABELS: dict[str, str] = {
    "cardCorners": "名片切角",
    "folding": "折页方式",
    "boxStructure": "盒型结构",
    "coverPaper": "封面用纸",
    "labelShape": "标签形状",
    "adhesive": "标签胶型",
    "labelFormat": "交付形式",
    "install": "安装方式",
    "boardThickness": "板材厚度",
    "cupVolume": "纸杯容量",
    "paperParts": "联单联数",
}

_UNIT_CHARS = frozenset({"张", "份", "本", "个", "块", "套"})
_CTRL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_QTY_TEXT_RE = re.compile(r"^\s*(\d+)\s*(张|份|本|个|块|套)?\s*$")
_PAGES_TEXT_RE = re.compile(r"^\s*(\d+)\s*(?:页|P)?\s*$", re.I)


def _extract_unit(order: dict[str, Any]) -> str:
    """提取订单显式声明的数量单位，绝不猜测或换算未填写的单位。"""
    unit = str(order.get("quantityUnit") or "").strip()
    if unit:
        return unit
    match = re.search(r"(张|份|本|个|块|套)\s*$", str(order.get("quantity") or "").strip())
    return match[1] if match else ""


def _parse_int_quantity(value: Any, *, field_name: str = "quantity") -> tuple[int, str]:
    """解析并校验正整数数量（允许正整数或纯数字/合法单位文本）。"""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} 必须为正整数")
    explicit_unit = ""
    if isinstance(value, int):
        qty = value
    elif isinstance(value, str):
        if len(value) > MAX_OVERRIDE_STR_LEN or _CTRL_CHAR_RE.search(value):
            raise ValueError(f"{field_name} 格式非法或超长")
        cleaned = value.replace(",", "").strip()
        match = _QTY_TEXT_RE.fullmatch(cleaned)
        if not match:
            raise ValueError(f"{field_name} 必须为正整数（可附带 张/份/本/个/块/套）")
        qty = int(match[1])
        explicit_unit = match[2] or ""
    else:
        raise ValueError(f"{field_name} 必须为正整数")
    if not (MIN_SIM_QUANTITY <= qty <= MAX_SIM_QUANTITY):
        raise ValueError(f"{field_name} 超出允许范围（{MIN_SIM_QUANTITY}..{MAX_SIM_QUANTITY}）")
    return qty, explicit_unit


def _validate_clean_str(value: Any, *, field_name: str, max_len: int = MAX_OVERRIDE_STR_LEN) -> str:
    """校验标量字符串类型、控制字符与长度上限。"""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须为字符串")
    if _CTRL_CHAR_RE.search(value):
        raise ValueError(f"{field_name} 不能包含控制字符")
    text = value.strip()
    if len(text) > max_len:
        raise ValueError(f"{field_name} 长度不能超过 {max_len} 字符")
    return text


def sanitize_overrides(overrides: Any) -> dict[str, Any]:
    """清洗并白名单校验 What-If 试算覆盖参数；非法字段或越界值抛出 ValueError。"""
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ValueError("overrides 必须为 JSON 对象")

    cleaned: dict[str, Any] = {}
    for key, value in overrides.items():
        if not isinstance(key, str) or key not in ALLOWED_OVERRIDE_KEYS:
            raise ValueError(f"不支持的试算覆盖字段：{key}")
        if value is None:
            continue

        if key == "quantity":
            qty, _unit = _parse_int_quantity(value, field_name="quantity")
            cleaned["quantity"] = qty
            continue

        if key == "pages":
            if isinstance(value, bool):
                raise ValueError("pages 必须为正整数或页数文本")
            if isinstance(value, int):
                page_num = value
            elif isinstance(value, str):
                text = _validate_clean_str(value, field_name="pages", max_len=32)
                if not text:
                    continue
                match = _PAGES_TEXT_RE.fullmatch(text)
                if not match:
                    raise ValueError("pages 格式无效，应为正整数或如“16 页”")
                page_num = int(match[1])
            else:
                raise ValueError("pages 必须为正整数或页数文本")
            if not (1 <= page_num <= 2000):
                raise ValueError("pages 须在 1..2000 范围内")
            cleaned["pages"] = f"{page_num} 页"
            continue

        if key == "finishing":
            if isinstance(value, list):
                if len(value) > 12:
                    raise ValueError("finishing 工艺数量不能超过 12 项")
                items: list[str] = []
                for idx, item in enumerate(value):
                    part = _validate_clean_str(item, field_name=f"finishing[{idx}]", max_len=32)
                    if not part or part in {"无特殊工艺", "无特殊表面工艺", "无需", "无", "无工艺", "不覆膜"}:
                        continue
                    part = part.replace("局部 UV", "局部UV")
                    if part not in items:
                        items.append(part)
                joined = "、".join(items) if items else "无特殊工艺"
                if len(joined) > MAX_OVERRIDE_STR_LEN:
                    raise ValueError(f"finishing 总长度不能超过 {MAX_OVERRIDE_STR_LEN} 字符")
                cleaned["finishing"] = joined
            elif isinstance(value, str):
                text = _validate_clean_str(value, field_name="finishing")
                if not text:
                    cleaned["finishing"] = "无特殊工艺"
                else:
                    parts, _ = _processes(text)
                    cleaned["finishing"] = "、".join(parts) if parts else "无特殊工艺"
            else:
                raise ValueError("finishing 必须为字符串或字符串数组")
            continue

        if key == "productSpecs":
            if not isinstance(value, dict):
                raise ValueError("productSpecs 必须为对象")
            spec_cleaned: dict[str, str] = {}
            for spec_key, spec_val in value.items():
                if not isinstance(spec_key, str) or spec_key not in ALLOWED_SPEC_OVERRIDE_KEYS:
                    raise ValueError(f"不支持的规格覆盖字段：{spec_key}")
                if spec_val is None:
                    continue
                spec_cleaned[spec_key] = _validate_clean_str(
                    spec_val, field_name=f"productSpecs.{spec_key}"
                )
            if spec_cleaned:
                cleaned["productSpecs"] = spec_cleaned
            continue

        # 其余字符串标量字段：paper, binding, printMode, printing
        text = _validate_clean_str(value, field_name=key)
        if text:
            cleaned[key] = text

    return cleaned


def _format_unit_price(amount: Decimal, quantity: Decimal) -> str:
    """精确格式化折合单价，避免极低单价被四舍五入成 0.00。"""
    if quantity <= 0 or amount <= 0:
        return "0.00"
    unit = amount / quantity
    if unit >= Decimal("1"):
        return format(unit.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")
    q4 = unit.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if q4 > 0:
        if q4 >= Decimal("0.10") and q4 == q4.quantize(CENT, rounding=ROUND_HALF_UP):
            return format(q4.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")
        return format(q4, ".4f")
    # 极微单价（< 0.0001）保留至 6 位小数，确保不显示为 0.00
    q6 = unit.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    if q6 > 0:
        return format(q6, ".6f").rstrip("0") or "0.000001"
    return "0.000001"


def _signed_money(diff: Decimal) -> str:
    """将 Decimal 金额差额格式化为带正负号的两位小数字符串。"""
    quantized = diff.quantize(CENT, rounding=ROUND_HALF_UP)
    if quantized > 0:
        return f"+{format(quantized, '.2f')}"
    if quantized < 0:
        return format(quantized, ".2f")
    return "0.00"


def _select_tier_quantities(
    product_type: str,
    family: str,
    current_qty: int | None,
    custom_quantities: Any = None,
) -> list[int]:
    """选取 5~6 个围绕当前数量的邻近档位，或校验自定义档位列表（最多 8 档）。"""
    if custom_quantities is not None:
        if not isinstance(custom_quantities, (list, tuple)) or not custom_quantities:
            raise ValueError("quantities 必须为非空正整数数组")
        if len(custom_quantities) > MAX_CUSTOM_TIERS:
            raise ValueError(f"quantities 最多支持 {MAX_CUSTOM_TIERS} 个档位")
        parsed = [_parse_int_quantity(item, field_name="quantities")[0] for item in custom_quantities]
        return sorted(set(parsed))

    preset = list(
        DEFAULT_TIER_PRESETS.get(product_type)
        or FAMILY_TIER_PRESETS.get(family)
        or (100, 200, 500, 1000, 2000, 5000)
    )
    if current_qty is None or current_qty <= 0:
        return preset

    pool: set[int] = set(preset)
    pool.add(current_qty)

    # 若当前数量接近或超过预设上界，补充 2~3 个更高整档以观察规模效应
    higher = sorted(q for q in pool if q > current_qty)
    if len(higher) < 2:
        for step in _GLOBAL_LADDER:
            if step > current_qty and len(higher) < 3:
                pool.add(step)
                higher = sorted(q for q in pool if q > current_qty)
        # 若仍不足（例如极高数量接近上限），按倍数补档
        for mult in (2, 3, 5):
            cand = current_qty * mult
            if cand <= MAX_SIM_QUANTITY and len(higher) < 2:
                pool.add(cand)
                higher = sorted(q for q in pool if q > current_qty)

    # 若当前数量小于最小预设且 > 1，补一个低于当前数量的对照档
    lower = sorted(q for q in pool if q < current_qty)
    if not lower and current_qty > 1:
        below = [step for step in _GLOBAL_LADDER if step < current_qty]
        if below:
            pool.add(below[-1])

    sorted_pool = sorted(pool)
    target_count = min(len(sorted_pool), 6 if len(preset) >= 6 or len(sorted_pool) >= 6 else 5)
    if len(sorted_pool) <= target_count:
        return sorted_pool

    # 以 current_qty 为锚点窗口截取：优先保证右侧（更高数量）有 2~3 档以便识别拐点
    idx = sorted_pool.index(current_qty)
    right_available = len(sorted_pool) - 1 - idx
    want_right = min(3, right_available)
    if want_right < 2 and right_available > 0:
        want_right = right_available
    want_left = target_count - 1 - want_right
    start = max(0, idx - want_left)
    end = start + target_count
    if end > len(sorted_pool):
        end = len(sorted_pool)
        start = max(0, end - target_count)
    return sorted_pool[start:end]


def _pick_sweet_spot(
    tiers: list[dict[str, Any]],
    tier_decimals: dict[int, tuple[Decimal, Decimal]],
    current_qty: int | None,
) -> dict[str, Any] | None:
    """在高于当前数量的 ready 档位中识别性价比拐点，并标注 isSweetSpot 与中文摘要。"""
    if current_qty is None or current_qty not in tier_decimals:
        return None
    curr_mid, curr_unit = tier_decimals[current_qty]
    if curr_mid <= 0 or curr_unit <= 0:
        return None

    higher_ready: list[tuple[int, dict[str, Any], float, float, float]] = []
    step_rank = 0
    for tier in tiers:
        qty = tier["quantity"]
        if qty <= current_qty or tier["status"] != "ready" or qty not in tier_decimals:
            continue
        step_rank += 1
        cand_mid, cand_unit = tier_decimals[qty]
        unit_drop_pct = float((curr_unit - cand_unit) / curr_unit * Decimal(100))
        total_inc_pct = float((cand_mid - curr_mid) / curr_mid * Decimal(100))
        qty_ratio = qty / current_qty
        higher_ready.append((step_rank, tier, unit_drop_pct, total_inc_pct, qty_ratio))

    if not higher_ready:
        return None

    # 第一优先池：比当前高 1~2 档、不超过 5 倍增量且单价降幅 ≥ 15%
    preferred = [
        item for item in higher_ready
        if item[0] <= 2 and item[4] <= 5.0 and item[2] >= 15.0
    ]
    candidates = preferred or [
        item for item in higher_ready
        if item[4] <= 5.0 and item[2] >= 15.0
    ]
    if not candidates:
        return None

    # 按 unitDropPct / max(10, totalIncreasePct) 最大选优；得分并列时偏好更温和的增量档位
    best = max(
        candidates,
        key=lambda item: (item[2] / max(10.0, item[3]), -item[0]),
    )
    _, best_tier, unit_drop_pct, total_inc_pct, _ = best
    unit_label = best_tier["unit"]
    qty_display = f"{best_tier['quantity']} {unit_label}".strip()
    total_inc_round = round(max(0.0, total_inc_pct), 1)
    unit_drop_round = round(unit_drop_pct, 1)

    if total_inc_round <= 0.0:
        summary = (
            f"升至 {qty_display}：示例总价基本持平（仍在最低收费内），"
            f"折合单价下降约 {unit_drop_round:.1f}%"
        )
    elif total_inc_round <= 50.0:
        summary = (
            f"升至 {qty_display}：示例总价仅增约 {total_inc_round:.1f}%，"
            f"折合单价下降约 {unit_drop_round:.1f}%"
        )
    else:
        summary = (
            f"升至 {qty_display}：示例总价增约 {total_inc_round:.1f}%，"
            f"折合单价下降约 {unit_drop_round:.1f}%"
        )

    best_tier["isSweetSpot"] = True
    best_tier["note"] = summary
    return {
        "quantity": best_tier["quantity"],
        "unit": best_tier["unit"],
        "mid": best_tier["mid"],
        "unitMid": best_tier["unitMid"],
        "displayRange": best_tier["displayRange"],
        "totalIncreasePct": total_inc_round,
        "unitDropPct": unit_drop_round,
        "summary": summary,
    }


def simulate_quantity_tiers(
    order: dict[str, Any],
    *,
    rate_card: dict[str, Any] | None = None,
    quantities: list[int] | tuple[int, ...] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """按邻近或自定义数量档位逐档模拟估算，识别性价比拐点（纯只读、不修改入参）。"""
    if not isinstance(order, dict):
        raise ValueError("order 必须为对象")

    card = deepcopy(rate_card) if rate_card is not None else load_rate_card()
    validate_rate_card(card)

    product_type = str(order.get("productType") or "")
    category = card["categories"].get(product_type, {})
    family = str(category.get("family") or "")
    current_dec = _quantity(order)
    current_qty = int(current_dec) if current_dec is not None else None
    unit = _extract_unit(order)

    tier_quantities = _select_tier_quantities(product_type, family, current_qty, quantities)

    tiers: list[dict[str, Any]] = []
    tier_decimals: dict[int, tuple[Decimal, Decimal]] = {}

    for qty in tier_quantities:
        sim_order = deepcopy(order)
        sim_order["quantityValue"] = qty
        sim_order["quantity"] = f"{qty} {unit}".strip() if unit else str(qty)
        if unit:
            sim_order["quantityUnit"] = unit

        est = estimate_order(sim_order, rate_card=card, as_of=as_of)
        status = est["status"]
        is_current = current_qty is not None and qty == current_qty
        low_val = est["range"]["low"] if status == "ready" and est.get("range") else None
        mid_val = est["range"]["mid"] if status == "ready" and est.get("range") else None
        high_val = est["range"]["high"] if status == "ready" and est.get("range") else None
        unit_mid: str | None = None
        if status == "ready" and mid_val is not None:
            mid_dec = decimal_value(mid_val)
            qty_dec = Decimal(qty)
            unit_dec = mid_dec / qty_dec
            tier_decimals[qty] = (mid_dec, unit_dec)
            unit_mid = _format_unit_price(mid_dec, qty_dec)

        has_min_floor = any(row.get("code") == "minimum_adjustment" for row in est.get("lineItems") or [])
        note = ""
        if is_current:
            note = "当前订单数量（含最低收费托底）" if has_min_floor else "当前订单数量"
        elif status == "ready" and has_min_floor:
            note = "处于最低收费托底区间"
        elif status != "ready":
            note = est.get("displayRange") or "待补充计价参数"

        tiers.append({
            "quantity": qty,
            "unit": unit,
            "status": status,
            "displayRange": est["displayRange"],
            "low": low_val,
            "mid": mid_val,
            "high": high_val,
            "unitMid": unit_mid,
            "isCurrent": is_current,
            "isSweetSpot": False,
            "deltaFromCurrentTotalPct": None,
            "deltaFromCurrentUnitPct": None,
            "note": note,
        })

    # 回填相对当前数量档的总价与单价百分比变化
    if current_qty is not None and current_qty in tier_decimals:
        curr_mid, curr_unit = tier_decimals[current_qty]
        for tier in tiers:
            qty = tier["quantity"]
            if tier["status"] != "ready" or qty not in tier_decimals:
                continue
            if qty == current_qty:
                tier["deltaFromCurrentTotalPct"] = 0.0
                tier["deltaFromCurrentUnitPct"] = 0.0
                continue
            cand_mid, cand_unit = tier_decimals[qty]
            if curr_mid > 0:
                total_pct = round(float((cand_mid - curr_mid) / curr_mid * Decimal(100)), 1)
                tier["deltaFromCurrentTotalPct"] = total_pct
            if curr_unit > 0:
                unit_pct = round(float((cand_unit - curr_unit) / curr_unit * Decimal(100)), 1)
                tier["deltaFromCurrentUnitPct"] = unit_pct
                if not tier["note"]:
                    if unit_pct < -0.5:
                        tier["note"] = f"折合单价降约 {abs(unit_pct):.1f}%"
                    elif unit_pct > 0.5:
                        tier["note"] = f"折合单价高约 {unit_pct:.1f}%"
                    else:
                        tier["note"] = "折合单价基本持平"

    sweet_spot = _pick_sweet_spot(tiers, tier_decimals, current_qty)

    return {
        "productType": product_type,
        "family": family,
        "unit": unit,
        "currentQuantity": current_qty,
        "tiers": tiers,
        "sweetSpot": sweet_spot,
        "disclaimer": DISCLAIMER,
    }


def _material_canonical_key(text: str) -> str:
    """将材料文本归一化为去空白的比较键，避免重复选项（如 250g 与 250 g/m²）。"""
    return re.sub(r"\s+", "", str(normalize_print_term("paper", text) or "")).lower()


def _dedupe_materials(candidates: list[str], card: dict[str, Any], current_values: list[str]) -> list[str]:
    """过滤出当前价卡已标定的材料选项，并保留当前订单已填材料避免 UI 丢失。"""
    result: list[str] = []
    seen_keys: set[str] = set()

    # 先放入当前订单已填材料（保真显示）
    for raw in current_values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = _material_canonical_key(text)
        if key not in seen_keys:
            seen_keys.add(key)
            result.append(text)

    # 再追加当前价卡真正已标定、可直接试算的候选项
    for item in candidates:
        normalized = normalize_print_term("paper", item)
        if _paper(normalized, card) is None:
            continue
        key = _material_canonical_key(item)
        if key not in seen_keys:
            seen_keys.add(key)
            result.append(item)
    return result


def build_adjustable_options(
    order: dict[str, Any],
    card: dict[str, Any],
    *,
    extra_orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """从价卡提取当前品类已标定、可直接试算的可调选项，避免用户选出未标定参数。"""
    product_type = str(order.get("productType") or "")
    category = card["categories"].get(product_type, {})
    family = str(category.get("family") or "")
    required_drivers = set(category.get("requiredDrivers") or [])
    specs = order.get("productSpecs") if isinstance(order.get("productSpecs"), dict) else {}

    all_orders = [order, *(extra_orders or [])]

    def collect_current_spec(spec_key: str) -> list[str]:
        vals: list[str] = []
        for entry in all_orders:
            entry_specs = entry.get("productSpecs") if isinstance(entry.get("productSpecs"), dict) else {}
            v = str(entry_specs.get(spec_key) or "").strip()
            if v and v not in vals:
                vals.append(v)
        return vals

    # 1. 材料选项（按品类/family 给出已标定组合，并指明对应写入字段）
    if family == "label":
        material_field = "productSpecs.labelMaterial"
        material_candidates = ["铜版不干胶", "透明不干胶", "PET"]
        current_materials = collect_current_spec("labelMaterial")
    elif family == "display":
        material_field = "productSpecs.displayMaterial"
        if product_type == "PVC" or "boardThickness" in required_drivers:
            material_candidates = ["PVC板"]
        elif product_type == "海报":
            material_candidates = [
                "海报纸", "相纸", "背胶",
                "157g 铜版纸", "200g 铜版纸", "250g 铜版纸",
            ]
        else:
            material_candidates = ["背胶", "相纸", "灯布", "户外灯布", "海报纸"]
        current_materials = collect_current_spec("displayMaterial")
    else:
        material_field = "paper"
        if product_type == "PVC卡":
            material_candidates = ["PVC卡基"]
        elif family == "cup":
            material_candidates = ["230g 淋膜纸", "260g 淋膜纸", "280g 淋膜纸", "300g 淋膜纸"]
        elif family == "form":
            material_candidates = ["40g 无碳纸", "45g 无碳纸", "50g 无碳纸", "55g 无碳纸"]
        elif product_type == "名片":
            material_candidates = [
                "250g 铜版纸", "300g 铜版纸", "350g 铜版纸",
                "300g 哑粉纸", "300g 白卡纸", "350g 白卡纸", "300g 牛皮纸",
            ]
        elif family == "booklet":
            material_candidates = [
                "128g 铜版纸", "157g 铜版纸", "200g 铜版纸",
                "157g 哑粉纸", "200g 哑粉纸", "100g 胶版纸",
                "120g 胶版纸", "250g 白卡纸",
            ]
        elif family == "box":
            material_candidates = [
                "250g 白卡纸", "300g 白卡纸", "350g 白卡纸",
                "250g 铜版纸", "300g 铜版纸", "250g 牛皮纸", "300g 牛皮纸",
            ]
        else:
            material_candidates = [
                "128g 铜版纸", "157g 铜版纸", "200g 铜版纸",
                "250g 铜版纸", "300g 铜版纸", "157g 哑粉纸",
                "200g 哑粉纸", "250g 白卡纸", "300g 白卡纸",
                "100g 胶版纸", "250g 牛皮纸",
            ]
        current_materials = [
            str(entry.get("paper") or "").strip()
            for entry in all_orders
            if str(entry.get("paper") or "").strip()
        ]

    materials = _dedupe_materials(material_candidates, card, current_materials)

    # 2. 表面工艺、装订、印刷方式、印色面数
    finishings = list((card.get("finishing") or {}).keys())
    bindings = (
        [name for name in ("骑马钉", "胶装", "锁线胶装") if name in (card.get("binding") or {})]
        if family == "booklet"
        else []
    )
    print_modes = list((card.get("printing") or {}).keys())

    printings = ["双面四色", "单面四色", "双面黑白", "单面黑白"]
    for entry in all_orders:
        cur_p = str(entry.get("printing") or "").strip()
        if cur_p and cur_p not in printings:
            printings.insert(0, cur_p)

    # 3. 品类专属可调规格驱动
    spec_options: dict[str, list[str]] = {}

    def register_spec(spec_key: str, choices: list[str]) -> None:
        merged = list(choices)
        for cur in collect_current_spec(spec_key):
            if cur not in merged:
                merged.insert(0, cur)
        if merged:
            spec_options[spec_key] = merged

    if product_type == "名片":
        corner_choices = ["直角", "圆角"] if "圆角" in (card.get("binding") or {}) else ["直角"]
        register_spec("cardCorners", corner_choices)

    if family == "folded":
        fold_choices = [name for name in ("二折", "三折", "风琴折") if name in (card.get("binding") or {})]
        register_spec("folding", fold_choices)

    if family == "booklet":
        cover_candidates = [
            "200g 铜版纸", "250g 铜版纸", "300g 铜版纸",
            "250g 哑粉纸", "250g 白卡纸", "300g 白卡纸", "350g 白卡纸",
        ]
        cover_list = _dedupe_materials(cover_candidates, card, collect_current_spec("coverPaper"))
        if cover_list:
            spec_options["coverPaper"] = cover_list

    if family == "box":
        structures = list(category.get("supportedStructures") or [])
        register_spec("boxStructure", structures)

    if family == "label":
        register_spec("labelShape", ["方形", "矩形", "圆形"])
        register_spec("adhesive", ["普通胶", "永久胶"])
        register_spec("labelFormat", ["张装"])

    if family == "display":
        register_spec("install", ["无需安装", "自行安装", "墙面张贴", "仅画面"])
        if "boardThickness" in required_drivers and isinstance(category.get("boardThicknesses"), dict):
            register_spec("boardThickness", list(category["boardThicknesses"].keys()))

    if family == "cup" and isinstance(category.get("cupSizes"), dict):
        register_spec("cupVolume", list(category["cupSizes"].keys()))

    if family == "form" and isinstance(category.get("paperParts"), dict):
        register_spec("paperParts", list(category["paperParts"].keys()))

    spec_labels = {k: SPEC_LABELS.get(k, k) for k in spec_options}

    return {
        "materials": materials,
        "materialField": material_field,
        "finishings": finishings,
        "bindings": bindings,
        "printModes": print_modes,
        "printings": printings,
        "specOptions": spec_options,
        "specLabels": spec_labels,
    }


def _snapshot_values(order: dict[str, Any], family: str) -> dict[str, Any]:
    """提取订单用于试算 UI 回显的当前参数快照。"""
    specs = order.get("productSpecs") if isinstance(order.get("productSpecs"), dict) else {}
    selected = order.get("selectedOption") if isinstance(order.get("selectedOption"), dict) else {}
    finishes, _ = _processes(order.get("finishing"))
    material = (
        str(specs.get("labelMaterial") or order.get("paper") or "").strip()
        if family == "label"
        else str(specs.get("displayMaterial") or order.get("paper") or "").strip()
        if family == "display"
        else str(order.get("paper") or "").strip()
    )
    qty_dec = _quantity(order)
    return {
        "quantity": int(qty_dec) if qty_dec is not None else None,
        "unit": _extract_unit(order),
        "material": material,
        "paper": str(order.get("paper") or "").strip(),
        "finishing": finishes,
        "binding": str(order.get("binding") or specs.get("binding") or "").strip(),
        "printMode": str(order.get("printMode") or selected.get("printMode") or "合版").strip(),
        "printing": str(order.get("printing") or "").strip(),
        "pages": str(order.get("pages") or "").strip(),
        "productSpecs": {
            k: str(v).strip()
            for k, v in specs.items()
            if k in ALLOWED_SPEC_OVERRIDE_KEYS and isinstance(v, str) and v.strip()
        },
    }


def _apply_overrides_to_copy(
    sim_order: dict[str, Any],
    applied: dict[str, Any],
    raw_overrides: dict[str, Any] | None,
    family: str,
) -> None:
    """将已清洗的 What-If 覆盖参数应用到订单副本上。"""
    if not applied:
        return

    if "quantity" in applied:
        qty = applied["quantity"]
        unit = _extract_unit(sim_order)
        # 若原订单无单位但覆盖文本显式带了合法单位，沿用该单位
        if not unit and isinstance(raw_overrides, dict) and isinstance(raw_overrides.get("quantity"), str):
            _, explicit_unit = _parse_int_quantity(raw_overrides["quantity"], field_name="quantity")
            unit = explicit_unit
        sim_order["quantityValue"] = qty
        sim_order["quantity"] = f"{qty} {unit}".strip() if unit else str(qty)
        if unit:
            sim_order["quantityUnit"] = unit

    spec_overrides = applied.get("productSpecs") if isinstance(applied.get("productSpecs"), dict) else {}

    if "paper" in applied:
        sim_order["paper"] = applied["paper"]
        if family == "label" and "labelMaterial" not in spec_overrides:
            sim_specs = sim_order.get("productSpecs")
            if not isinstance(sim_specs, dict):
                sim_specs = {}
                sim_order["productSpecs"] = sim_specs
            sim_specs["labelMaterial"] = applied["paper"]
        elif family == "display" and "displayMaterial" not in spec_overrides:
            sim_specs = sim_order.get("productSpecs")
            if not isinstance(sim_specs, dict):
                sim_specs = {}
                sim_order["productSpecs"] = sim_specs
            sim_specs["displayMaterial"] = applied["paper"]

    for scalar_key in ("finishing", "binding", "printMode", "pages", "printing"):
        if scalar_key in applied:
            sim_order[scalar_key] = applied[scalar_key]

    if spec_overrides:
        sim_specs = sim_order.get("productSpecs")
        if not isinstance(sim_specs, dict):
            sim_specs = {}
            sim_order["productSpecs"] = sim_specs
        for spec_key, spec_val in spec_overrides.items():
            sim_specs[spec_key] = spec_val
            if spec_key in ("labelMaterial", "displayMaterial") and "paper" not in applied:
                sim_order["paper"] = spec_val


def _compute_delta(
    baseline: dict[str, Any],
    simulated: dict[str, Any],
    base_qty: Decimal | None,
    sim_qty: Decimal | None,
) -> dict[str, Any]:
    """用 Decimal 精确计算试算结果相对当前基准的总价与单价差额。"""
    base_ready = baseline.get("status") == "ready" and bool(baseline.get("range")) and base_qty is not None
    sim_ready = simulated.get("status") == "ready" and bool(simulated.get("range")) and sim_qty is not None

    unit_baseline: str | None = None
    unit_simulated: str | None = None
    base_mid = base_low = base_high = base_unit = None
    sim_mid = sim_low = sim_high = sim_unit = None

    if base_ready and base_qty is not None:
        base_low = decimal_value(baseline["range"]["low"])
        base_mid = decimal_value(baseline["range"]["mid"])
        base_high = decimal_value(baseline["range"]["high"])
        base_unit = base_mid / base_qty
        unit_baseline = _format_unit_price(base_mid, base_qty)

    if sim_ready and sim_qty is not None:
        sim_low = decimal_value(simulated["range"]["low"])
        sim_mid = decimal_value(simulated["range"]["mid"])
        sim_high = decimal_value(simulated["range"]["high"])
        sim_unit = sim_mid / sim_qty
        unit_simulated = _format_unit_price(sim_mid, sim_qty)

    if not (base_ready and sim_ready and base_mid is not None and sim_mid is not None):
        return {
            "comparable": False,
            "midDiff": None,
            "lowDiff": None,
            "highDiff": None,
            "midPct": None,
            "unitBaseline": unit_baseline,
            "unitSimulated": unit_simulated,
            "unitDiffPct": None,
            "direction": None,
        }

    mid_delta = sim_mid - base_mid
    low_delta = sim_low - base_low
    high_delta = sim_high - base_high
    mid_pct = (
        round(float(mid_delta / base_mid * Decimal(100)), 1)
        if base_mid > 0
        else 0.0
    )
    unit_diff_pct = (
        round(float((sim_unit - base_unit) / base_unit * Decimal(100)), 1)
        if base_unit is not None and base_unit > 0 and sim_unit is not None
        else 0.0
    )
    direction = "up" if mid_delta > 0 else "down" if mid_delta < 0 else "flat"

    return {
        "comparable": True,
        "midDiff": _signed_money(mid_delta),
        "lowDiff": _signed_money(low_delta),
        "highDiff": _signed_money(high_delta),
        "midPct": mid_pct,
        "unitBaseline": unit_baseline,
        "unitSimulated": unit_simulated,
        "unitDiffPct": unit_diff_pct,
        "direction": direction,
    }


def simulate_what_if(
    order: dict[str, Any],
    overrides: dict[str, Any] | None = None,
    *,
    rate_card: dict[str, Any] | None = None,
    quantities: list[int] | tuple[int, ...] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """执行只读工艺/材料/数量 What-If 试算并返回基准对比、阶梯档位与拐点摘要。"""
    if not isinstance(order, dict):
        raise ValueError("order 必须为对象")

    card = deepcopy(rate_card) if rate_card is not None else load_rate_card()
    validate_rate_card(card)

    base_order = deepcopy(order)
    sim_order = deepcopy(order)

    product_type = str(base_order.get("productType") or "")
    category = card["categories"].get(product_type, {})
    family = str(category.get("family") or "")

    baseline = estimate_order(base_order, rate_card=card, as_of=as_of)
    applied = sanitize_overrides(overrides)
    _apply_overrides_to_copy(
        sim_order,
        applied,
        overrides if isinstance(overrides, dict) else None,
        family,
    )
    simulated = estimate_order(sim_order, rate_card=card, as_of=as_of)

    base_qty = _quantity(base_order)
    sim_qty = _quantity(sim_order)
    unit = _extract_unit(sim_order)

    delta = _compute_delta(baseline, simulated, base_qty, sim_qty)
    tier_bundle = simulate_quantity_tiers(
        sim_order,
        rate_card=card,
        quantities=quantities,
        as_of=as_of,
    )
    adjustable = build_adjustable_options(sim_order, card, extra_orders=[base_order])

    return {
        "type": "pricing_simulation",
        "readOnly": True,
        "productType": product_type,
        "family": family,
        "unit": unit,
        "currentQuantity": int(sim_qty) if sim_qty is not None else None,
        "baselineQuantity": int(base_qty) if base_qty is not None else None,
        "baseline": baseline,
        "simulated": simulated,
        "appliedOverrides": applied,
        "hasOverrides": bool(applied),
        "delta": delta,
        "tiers": tier_bundle["tiers"],
        "sweetSpot": tier_bundle["sweetSpot"],
        "adjustable": adjustable,
        "baselineValues": _snapshot_values(base_order, family),
        "simulatedValues": _snapshot_values(sim_order, family),
        "disclaimer": DISCLAIMER,
    }
