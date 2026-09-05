"""Order data contracts: fields, quantity/dimension normalization, migrations.

Split from agent.py so the order model can evolve (schemaVersion, new
dimension semantics) without touching perception, tools, or the workflow.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from typing import Any


DIMENSION_DEFAULTS = {
    "finishedSize": "", "expandedSize": "", "dieCutSize": "", "packageSize": "",
}
PACKAGE_DIMENSION_PRODUCTS = {"包装盒", "手提袋"}
ORDER_DEFAULTS = {
    "productType": "", "productTypes": [], "items": [], "purpose": "", "quantity": "", "quantityValue": None, "quantityUnit": "", "size": "",
    "dimensions": deepcopy(DIMENSION_DEFAULTS),
    "pages": "", "orientation": "", "paper": "", "printing": "", "finishing": "", "binding": "",
    "deadline": "", "budget": "", "platform": "generic", "productSpecs": {},
}
ITEM_DEFAULTS = {
    "itemId": "", "productType": "", "purpose": "", "quantity": "", "quantityValue": None,
    "quantityUnit": "", "size": "", "dimensions": deepcopy(DIMENSION_DEFAULTS), "pages": "", "orientation": "", "paper": "",
    "printing": "", "finishing": "", "binding": "", "deadline": "", "budget": "",
    "productSpecs": {}, "selectedOption": None, "orderGenerated": False, "uploadedFile": None,
}
REQUIRED = ["productType", "quantity", "size", "paper", "printing", "deadline"]
HISTORY_LIMIT = 80
MAX_PLANNER_TOOL_ROUNDS = 2
MAX_RUN_EVENTS = 64
MAX_RUN_HISTORY = 20
MAX_QUOTE_REQUESTS = 40
QUOTE_ACTIVE_STATUSES = {"awaiting_human_confirmation", "confirmed"}
QUOTE_TERMINAL_STATUSES = {"cancelled", "stale", "submitted", "failed"}
RECOMMENDATION_FIELDS = {"productType", "productTypes", "items", "purpose", "quantity", "quantityValue", "quantityUnit", "size", "dimensions", "pages", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget", "productSpecs"}
WORKFLOW_LABELS = {
    "collect": "需求收集", "clarify": "品类澄清", "recommend": "方案选择",
    "preflight": "文件预检", "quote": "报价准备", "confirm": "订单确认",
    "export": "导出交接",
}
FIELD_SOURCE_LABELS = {
    "user": "用户输入", "rule": "规则识别", "model": "模型推断",
    "recommendation": "方案带入", "system": "系统默认",
}
LABELS = {
    "productType": "印刷品", "purpose": "使用场景", "quantity": "数量",
    "size": "成品尺寸", "pages": "页数", "orientation": "版式方向", "paper": "纸张/材料", "printing": "印刷颜色",
    "finishing": "表面工艺", "binding": "装订/后道", "deadline": "交期",
    "budget": "预算偏好", "platform": "目标平台",
}
DIMENSION_LABELS = {
    "finishedSize": "成品尺寸", "expandedSize": "展开尺寸",
    "dieCutSize": "刀模尺寸", "packageSize": "包装三维尺寸",
}
MATERIAL_SPEC_PRODUCTS = {"标签", "手提袋", "纸杯", "海报", "喷画", "PVC", "PVC卡"}


def quote_idempotency_key(order: dict[str, Any], platform_id: str, item_id: str | None = None) -> str:
    """Build a stable key from the order data that affects a supplier quote."""
    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): normalize(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip())
        return value

    payload = {
        "platformId": str(platform_id or "generic"),
        "itemId": str(item_id) if item_id else None,
        "order": {key: normalize(order.get(key)) for key in sorted(RECOMMENDATION_FIELDS) if key != "items"},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"quote:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


QUANTITY_MULTIPLIERS = {"万": 10000, "千": 1000, "百": 100}
QUANTITY_UNIT_ALIASES = {
    "份": "份", "本": "本", "册": "册", "张": "张", "个": "个", "件": "件", "盒": "盒",
    "套": "套", "包": "包", "箱": "箱", "杯": "杯", "块": "块", "枚": "枚",
    "平方米": "平方米", "平米": "平方米", "㎡": "平方米",
}
QUANTITY_CAPTURE = (
    r"(\d[\d,]*(?:\.\d+)?)\s*(?:(万|千|百)\s*)?"
    r"(平方米|平米|㎡|份|本|册|张|个|件|盒|套|包|箱|杯|块|枚)?"
)
DEFAULT_QUANTITY_UNITS = {
    "名片": "张", "单页": "张", "折页": "张", "标签": "张", "吊牌": "张", "海报": "张",
    "喷画": "张", "PVC": "块", "PVC卡": "张", "包装盒": "个", "手提袋": "个", "纸杯": "个",
    "信封封套": "个", "宣传册": "本", "画册": "本", "联单": "本", "数码印刷": "份",
}


def default_quantity_unit(product: str | None) -> str:
    """Choose a display unit only when the user did not provide one."""
    return DEFAULT_QUANTITY_UNITS.get(product or "", "份")


def parse_quantity(value: Any, product: str | None = None, unit_hint: str = "") -> tuple[str, int | float, str] | None:
    """Return a stable display value, numeric value and unit for an order quantity."""
    if value in (None, ""):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    match = re.search(
        r"(?<![A-Za-z0-9])([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:(万|千|百)\s*)?"
        r"(平方米|平米|㎡|份|本|册|张|个|件|盒|套|包|箱|杯|块|枚)?",
        text,
    )
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    count = number * QUANTITY_MULTIPLIERS.get(match.group(2) or "", 1)
    # An explicit unit in the display text is authoritative; the hint is for
    # numeric-only patches and legacy records that lack a unit.
    raw_unit = match.group(3) or unit_hint or ""
    unit = QUANTITY_UNIT_ALIASES.get(raw_unit, raw_unit) or default_quantity_unit(product)
    numeric: int | float = int(count) if count.is_integer() else round(count, 3)
    count_text = str(numeric)
    return f"{count_text} {unit}", numeric, unit


def normalize_order_quantity(order: dict[str, Any]) -> None:
    """Migrate old sessions and keep quantity display/number/unit in sync."""
    parsed = parse_quantity(order.get("quantity"), order.get("productType"), str(order.get("quantityUnit") or ""))
    if not parsed:
        return
    display, numeric, unit = parsed
    order["quantity"] = display
    order["quantityValue"] = numeric
    order["quantityUnit"] = unit


def _is_three_dimensional_size(value: Any) -> bool:
    """Identify a structural L×W×H value without guessing its unit."""
    return len(re.findall(r"×", unicodedata.normalize("NFKC", str(value or "")))) >= 2


def normalize_order_dimensions(order: dict[str, Any]) -> None:
    """Keep explicit dimension meanings while migrating legacy ``size`` data."""
    raw = order.get("dimensions") if isinstance(order.get("dimensions"), dict) else {}
    dimensions = deepcopy(DIMENSION_DEFAULTS)
    for key in DIMENSION_DEFAULTS:
        value = raw.get(key)
        if value not in (None, ""):
            dimensions[key] = str(value).strip()

    specs = order.get("productSpecs") if isinstance(order.get("productSpecs"), dict) else {}
    # Dimension meanings have a single canonical home. Remove aliases that
    # may have been written by an older client or an unconstrained model.
    if isinstance(order.get("productSpecs"), dict):
        order["productSpecs"] = {key: value for key, value in specs.items() if key not in DIMENSION_DEFAULTS}
        specs = order["productSpecs"]
    if not dimensions["packageSize"]:
        dimensions["packageSize"] = str(specs.get("boxSize") or specs.get("bagSize") or "").strip()
    if not dimensions["expandedSize"]:
        dimensions["expandedSize"] = str(specs.get("expandedSize") or "").strip()
    if not dimensions["dieCutSize"]:
        dimensions["dieCutSize"] = str(specs.get("dieCutSize") or "").strip()

    legacy_size = str(order.get("size") or "").strip()
    if legacy_size:
        if _is_three_dimensional_size(legacy_size) or order.get("productType") in PACKAGE_DIMENSION_PRODUCTS:
            if not dimensions["packageSize"]:
                dimensions["packageSize"] = legacy_size
        elif not dimensions["finishedSize"]:
            dimensions["finishedSize"] = legacy_size
    order["dimensions"] = dimensions


def merge_dimension_patch(current: dict[str, Any] | None, patch: dict[str, Any]) -> dict[str, str]:
    """Merge only the four dimension meanings accepted by the order model."""
    dimensions = deepcopy(DIMENSION_DEFAULTS)
    if isinstance(current, dict):
        dimensions.update({key: str(current.get(key) or "").strip() for key in DIMENSION_DEFAULTS})
    for key, value in patch.items():
        if key in DIMENSION_DEFAULTS:
            dimensions[key] = str(value).strip() if value is not None else ""
    return dimensions


def migrate_dimension_field_meta(state: dict[str, Any]) -> None:
    """Move legacy product-spec provenance keys to the canonical dimensions path."""
    metadata = state.get("fieldMeta") if isinstance(state.get("fieldMeta"), dict) else {}
    for field in list(metadata):
        parts = str(field).split(".")
        target = ""
        if len(parts) == 2 and parts[0] == "productSpecs" and parts[1] in DIMENSION_DEFAULTS:
            target = f"dimensions.{parts[1]}"
        elif len(parts) == 4 and parts[0] == "items" and parts[2] == "productSpecs" and parts[3] in DIMENSION_DEFAULTS:
            target = f"items.{parts[1]}.dimensions.{parts[3]}"
        if not target:
            continue
        if target not in metadata:
            metadata[target] = metadata[field]
        metadata.pop(field, None)
    state["fieldMeta"] = metadata


def normalize_order_items(order: dict[str, Any]) -> None:
    """Normalize multi-product items while preserving stable IDs and legacy fields."""
    raw_items = order.get("items")
    if not isinstance(raw_items, list):
        order["items"] = []
        return
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        item = deepcopy(ITEM_DEFAULTS)
        item.update({key: deepcopy(value) for key, value in raw.items() if key in ITEM_DEFAULTS or key == "itemId"})
        item["itemId"] = str(raw.get("itemId") or f"item-{index + 1}").strip() or f"item-{index + 1}"
        item["productSpecs"] = dict(raw.get("productSpecs") or {}) if isinstance(raw.get("productSpecs"), dict) else {}
        normalize_order_quantity(item)
        normalize_order_dimensions(item)
        normalized.append(item)
    order["items"] = normalized
    product_types = list(dict.fromkeys(str(item["productType"]) for item in normalized if item.get("productType")))
    if len(product_types) > 1:
        order["productTypes"] = product_types
        if not order.get("productType"):
            order["productType"] = product_types[0]


def required_order_keys(order: dict[str, Any]) -> list[str]:
    """Return base fields that make sense for the selected product family."""
    product = order.get("productType")
    return [key for key in REQUIRED if not (key == "paper" and product in MATERIAL_SPEC_PRODUCTS)]


def _multi_product_info(order: dict[str, Any]) -> list[str]:
    """Return labels when an order contains multiple independent order items."""
    product_types = order.get("productTypes") if isinstance(order.get("productTypes"), list) else []
    items = order.get("items") if isinstance(order.get("items"), list) else []
    labels: list[str] = []
    for value in product_types:
        if value and str(value) not in labels:
            labels.append(str(value))
    for item in items:
        if isinstance(item, dict) and item.get("productType"):
            value = str(item["productType"])
            if value not in labels:
                labels.append(value)
    if len(product_types) > 1 or len(items) > 1 or len(labels) > 1:
        return labels or ["多个订单项"]


def _number(value: str) -> int | None:
    match = re.search(r"\d[\d,]*(?:\.\d+)?", value or "")
    return int(float(match.group(0).replace(",", ""))) if match else None


def _parse_size_mm(value: str) -> tuple[float, ...] | None:
    """Parse common named/custom sizes for capability checks, without guessing units."""
    text = unicodedata.normalize("NFKC", str(value or "")).upper().replace("＊", "×").replace("*", "×")
    named = {"A3": (297.0, 420.0), "A4": (210.0, 297.0), "A5": (148.0, 210.0),
             "B4": (257.0, 364.0), "B5": (182.0, 257.0)}
    compact = re.sub(r"\s+", "", text)
    if compact in named:
        return named[compact]
    match = re.fullmatch(r"(\d+(?:\.\d+)?)×(\d+(?:\.\d+)?)(?:×(\d+(?:\.\d+)?))?(MM|CM)?", compact)
    if not match:
        return None
    values = tuple(float(item) for item in match.groups()[:3] if item is not None)
    unit = match.group(4)
    if unit == "CM":
        return tuple(item * 10 for item in values)
    if unit == "MM":
        return values
    return None


def _parse_max_size(value: str) -> tuple[float, ...] | None:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().replace("＊", "×").replace("*", "×")
    if text == "A3+":
        return (330.0, 480.0)
    return _parse_size_mm(text)


STATE_SCHEMA_VERSION = 2


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Apply every schema migration so old sessions load exactly like fresh ones.

    New session keys belong here (one setdefault each) together with a bump of
    ``STATE_SCHEMA_VERSION``; ad-hoc migrations stay in this single function.
    """
    order = deepcopy(ORDER_DEFAULTS)
    order.update(state.get("order") or {})
    normalize_order_quantity(order)
    normalize_order_dimensions(order)
    normalize_order_items(order)
    state["order"] = order
    state.setdefault("messages", [])
    state.setdefault("stage", "collect")
    state.setdefault("selectedOption", None)
    state.setdefault("orderGenerated", False)
    state.setdefault("uploadedFile", None)
    state.setdefault("uploadedFiles", [])
    state.setdefault("handoff", None)
    state.setdefault("confirmation", {"status": "not_ready"})
    state.setdefault("fieldMeta", {})
    migrate_dimension_field_meta(state)
    state.setdefault("conflicts", [])
    state.setdefault("lastRun", None)
    state.setdefault("runHistory", [])
    state.setdefault("workflowStage", state.get("stage", "collect"))
    state.setdefault("activeItemIndex", None)
    state.setdefault("itemOptions", {})
    state.setdefault("quoteRequests", [])
    state.setdefault("activeQuoteRequestId", None)
    state.setdefault("schemaVersion", STATE_SCHEMA_VERSION)
    return state


# Full-sheet sizes (mm) for imposition hints. 印张光边按四边各 3mm 预留。
SHEET_SIZES_MM = {"大度": (889.0, 1194.0), "正度": (787.0, 1092.0)}
TRIM_MARGIN_MM = 3.0


def imposition_hint(size: str | None) -> str | None:
    """Estimate how many finished flat pieces fit on a full print sheet.

    Pure geometry for reference only: whole-piece counts on 大度/正度 sheets
    after a 3mm trim margin, trying both orientations. Real imposition also
    depends on grain direction, bleed and the supplier's layout.
    """
    parsed = _parse_size_mm(size or "")
    if not parsed or len(parsed) != 2:
        return None
    short, long = sorted(parsed)
    best: tuple[int, str, float] | None = None
    for name, (sheet_w, sheet_h) in SHEET_SIZES_MM.items():
        usable_w, usable_h = sheet_w - 2 * TRIM_MARGIN_MM, sheet_h - 2 * TRIM_MARGIN_MM
        for piece_w, piece_h in ((short, long), (long, short)):
            count = int(usable_w // piece_w) * int(usable_h // piece_h)
            if count <= 0:
                continue
            utilization = count * short * long / (sheet_w * sheet_h) * 100
            if best is None or count > best[0]:
                best = (count, name, utilization)
    if best is None:
        return None
    count, name, utilization = best
    return f"{name}全张约可出 {count} 裁，纸张利用率约 {utilization:.0f}%（参考，以供应商拼版为准）"
