"""Small, dependency-free print order agent.

Flow: perceive -> remember -> plan -> call tools -> respond.
The contracts are intentionally compatible with a future LangGraph/FastAPI layer.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from product_knowledge import KNOWLEDGE_MANIFEST, KNOWLEDGE_VERSION, alias_map, find_product, parameter_state, profile_for
from supplier_adapters import get_adapter


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
    return []


def _find_product_mentions(text: str) -> list[tuple[str, int, int]]:
    """Find non-overlapping catalog mentions so multi-product requests are explicit."""
    normalized = unicodedata.normalize("NFKC", text or "")
    candidates = []
    for alias, product in sorted(alias_map().items(), key=lambda item: len(item[0]), reverse=True):
        for match in re.finditer(re.escape(alias), normalized, re.IGNORECASE):
            candidates.append((match.start(), match.end(), product))
    accepted: list[tuple[str, int, int]] = []
    for start, end, product in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(start < other_end and end > other_start for _, other_start, other_end in accepted):
            continue
        accepted.append((product, start, end))
    return sorted(accepted, key=lambda item: item[1])


class Memory:
    """SQLite-backed session memory; survives server restarts."""

    def __init__(self, path: str | Path = "data/agent.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    def load(self, session_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return self.fresh_state()
        state = json.loads(row[0])
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
        return state

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        data = json.dumps(state, ensure_ascii=False)
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (session_id, data))

    @staticmethod
    def fresh_state() -> dict[str, Any]:
        order = deepcopy(ORDER_DEFAULTS)
        normalize_order_dimensions(order)
        normalize_order_items(order)
        return {"order": order, "messages": [], "stage": "collect",
                "selectedOption": None, "orderGenerated": False, "uploadedFile": None,
                "uploadedFiles": [],
                "handoff": None, "confirmation": {"status": "not_ready"},
                "fieldMeta": {}, "conflicts": [], "lastRun": None, "runHistory": [],
                "workflowStage": "collect", "activeItemIndex": None, "itemOptions": {},
                "quoteRequests": [], "activeQuoteRequestId": None}

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db


PLATFORMS = {
    "generic": {"name": "通用印刷平台", "mode": "export", "capabilities": ["标准订单导出"],
                "supplierProfile": {"categories": ["全品类"], "maxSize": "按供应商确认", "papers": ["按供应商确认"], "finishing": ["按供应商确认"], "leadTime": "按供应商确认"}},
    "shengda": {"name": "盛大印刷", "mode": "manual", "capabilities": ["订单草稿", "人工询价"],
                "supplierProfile": {"categories": ["名片", "单页", "折页", "宣传册", "画册", "标签", "包装盒"], "maxSize": "1200×900mm", "papers": ["铜版纸", "哑粉纸", "白卡纸", "牛皮纸"], "finishing": ["覆膜", "哑膜", "亮膜", "烫金", "局部UV"], "leadTime": "1-5 天"}},
    "platform_a": {"name": "平台 A", "mode": "adapter", "capabilities": ["字段映射", "订单草稿"],
                   "supplierProfile": {"categories": ["名片", "单页", "折页", "宣传册", "画册"], "maxSize": "A3+", "papers": ["铜版纸", "哑粉纸"], "finishing": ["覆膜", "哑膜", "亮膜"], "leadTime": "2-6 天"}},
    "platform_b": {"name": "平台 B", "mode": "adapter", "capabilities": ["字段映射", "订单草稿"],
                   "supplierProfile": {"categories": ["名片", "标签", "手提袋", "包装盒"], "maxSize": "900×600mm", "papers": ["白卡纸", "牛皮纸", "不干胶"], "finishing": ["烫金", "击凸", "局部UV"], "leadTime": "3-7 天"}},
    "supplier": {"name": "自定义供应商", "mode": "adapter", "capabilities": ["字段映射"],
                 "supplierProfile": {"categories": ["待补充"], "maxSize": "待补充", "papers": ["待补充"], "finishing": ["待补充"], "leadTime": "待补充"}},
}
SUPPLIER_PROFILE_VERSION = "2026.09.04"

Tool = Callable[..., Any]
TOOLS: dict[str, Tool] = {}
TOOL_META: dict[str, dict[str, Any]] = {}

# A small, provider-neutral contract.  The UI can render these contracts and
# an eventual LangGraph/MCP adapter can pass them to a model without coupling
# the core agent to one SDK.
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "recommend_processes": {
        "input": {"type": "object", "properties": {"order": {"type": "object"}}, "required": ["order"]},
        "output": {"type": "array", "items": {"type": "object"}},
    },
    "explain_print_term": {
        "input": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
        "output": {"type": "object", "properties": {"topic": {"type": "string"}, "answer": {"type": "string"}}},
    },
    "preflight_file": {
        "input": {"type": "object", "properties": {"fileName": {"type": "string"}, "sizeBytes": {"type": "integer"}, "pageCount": {"type": ["integer", "null"]}, "inspection": {"type": ["object", "null"]}, "expectedSize": {"type": ["string", "null"]}, "itemIndex": {"type": ["integer", "null"]}}, "required": ["fileName", "sizeBytes"]},
        "output": {"type": "object", "properties": {"ok": {"type": "boolean"}, "warnings": {"type": "array"}}},
    },
    "validate_order": {
        "input": {"type": "object", "properties": {"order": {"type": "object"}}, "required": ["order"]},
        "output": {"type": "object", "properties": {"ok": {"type": "boolean"}, "missing": {"type": "array"}, "risks": {"type": "array"}}},
    },
    "estimate_price": {
        "input": {"type": "object", "properties": {"order": {"type": "object"}, "itemIndex": {"type": ["integer", "null"]}}, "required": ["order"]},
        "output": {"type": "object", "properties": {"range": {"type": "string"}, "missing": {"type": "array"}}},
    },
    "prepare_handoff": {
        "input": {"type": "object", "properties": {"order": {"type": "object"}, "itemIndex": {"type": ["integer", "null"]}}, "required": ["order"]},
        "output": {"type": "object", "properties": {"text": {"type": "string"}, "supplierReadiness": {"type": "object"}}},
    },
    "match_supplier_capability": {
        "input": {"type": "object", "properties": {"order": {"type": "object"}, "platformId": {"type": "string"}, "itemIndex": {"type": ["integer", "null"]}}, "required": ["order"]},
        "output": {"type": "object", "properties": {"status": {"type": "string"}, "supported": {"type": "array"}, "needsReview": {"type": "array"}, "unsupported": {"type": "array"}}},
    },
    "request_supplier_quote": {
        "input": {"type": "object", "properties": {"order": {"type": "object"}, "platformId": {"type": "string"}, "itemIndex": {"type": ["integer", "null"]}}, "required": ["order"]},
        "output": {"type": "object", "properties": {"status": {"type": "string"}, "requestId": {"type": "string"}, "mappedOrder": {"type": "object"}, "requiresHumanConfirmation": {"type": "boolean"}}},
    },
}


def tool(fn: Tool) -> Tool:
    TOOLS[fn.__name__] = fn
    TOOL_META[fn.__name__] = {"name": fn.__name__, "description": (fn.__doc__ or "").strip()}
    return fn


@tool
def recommend_processes(order: dict[str, Any]) -> list[dict[str, str]]:
    """根据用途、预算、交期和工艺约束生成可比较的候选方案。"""
    if _multi_product_info(order):
        # Keep the low-level tool contract as an array for existing callers;
        # Agent.call_tool adds the structured blocked response and explanation.
        return []
    product = order.get("productType") or "印刷品"
    profile = profile_for(product)
    premium = order.get("budget") == "优先视觉质感" or product in {"包装盒", "邀请函"}
    fast = order.get("budget") == "优先交期" or order.get("deadline") in {"今天", "明天", "后天", "一周内"}
    known_paper = order.get("paper") or ""
    printing = order.get("printing") or "四色印刷"
    format_note = f"{order.get('size')}，{order.get('pages')}" if order.get("size") and order.get("pages") else order.get("size") or "按成品尺寸确认"
    binding = order.get("binding") or profile.get("defaultBinding", "无需装订")
    profile_note = profile.get("recommendation", "先确认用途、尺寸、材料、颜色、数量和交期。")
    specs = order.get("productSpecs") or {}
    material_profiles = {
        "标签": (specs.get("labelMaterial") or "铜版不干胶", ("无特殊表面工艺", "按面材适配上光 / 覆膜", "白墨 / 专色")),
        "手提袋": (specs.get("bagMaterial") or "250g 白卡纸", ("无特殊表面工艺", "覆膜", "烫金 / 专色")),
        "纸杯": (specs.get("cupMaterial") or "食品级淋膜纸", ("无特殊表面工艺", "食品级上光", "专色")),
        "海报": (specs.get("displayMaterial") or "海报纸 / 背胶", ("无特殊表面工艺", "覆膜", "高精度输出")),
        "喷画": (specs.get("displayMaterial") or "户外灯布", ("无特殊表面工艺", "户外防护", "高精度输出")),
        "PVC": ("PVC 板材", ("无特殊表面工艺", "覆面 / 背胶", "高精度输出")),
        "PVC卡": ("PVC 卡基", ("无特殊表面工艺", "覆膜", "专色 / 编码")),
    }
    if product in material_profiles:
        material, finishes = material_profiles[product]
        materials = (material, material, material)
    elif product == "包装盒":
        material = known_paper if known_paper not in {"", "待推荐"} else "350g 白卡纸"
        materials = (material, material, material)
        finishes = ("无特殊表面工艺", "哑膜", "烫金 / 击凸")
    else:
        material = known_paper if known_paper not in {"", "待推荐"} else "157g 哑粉纸"
        materials = (material, "200g 铜版纸", "特种纸" if premium else "高克重纸张")
        finishes = ("无特殊工艺", "哑膜", "烫金 / 击凸")
    return [
        {"id": "economy", "title": "经济方案", "description": f"{materials[0]} + {printing}，{format_note}，{finishes[0]}，{binding}。",
         "cost": "成本较低", "lead": "交期较快" if fast else "交期稳定", "score": "适合控预算",
         "reason": f"{profile_note}适合预算敏感的项目。", "risk": "视觉层次和耐磨性相对基础。",
         "paper": materials[0], "finishing": finishes[0], "binding": binding},
        {"id": "balanced", "title": "平衡方案", "description": f"{materials[1]} + {printing}，{finishes[1]}，{binding}，兼顾效果与生产稳定性。",
         "cost": "成本中等", "lead": "交期稳定", "score": "综合推荐",
         "reason": f"{profile_note}在颜色表现、手感和成本之间取平衡。", "risk": "覆膜或复杂后道会增加少量加工时间和费用。",
         "paper": materials[1], "finishing": finishes[1], "binding": binding},
        {"id": "premium", "title": "质感方案", "description": f"{materials[2]} + {printing}，{finishes[2]}，{binding}，强化品牌表现。",
         "cost": "成本较高", "lead": "需要确认加急" if fast else "交期较长", "score": "视觉优先",
         "reason": f"{profile_note}适合品牌发布和需要触感记忆点的物料。", "risk": "需要专色、打样、结构或后道工艺确认。",
         "paper": materials[2], "finishing": finishes[2], "binding": binding},
    ]


def _number(value: str) -> int | None:
    match = re.search(r"\d[\d,]*(?:\.\d+)?", value or "")
    return int(float(match.group(0).replace(",", ""))) if match else None


@tool
def explain_print_term(question: str) -> dict[str, str]:
    """解释常见印刷术语，帮助非专业用户做选择。"""
    text = question.lower()
    if any(term in text for term in ("哑粉", "铜版", "纸张", "纸怎么选", "纸材")):
        answer = "哑粉纸颜色柔和、反光少，适合阅读型宣传册；铜版纸色彩更鲜亮、表面更光滑，适合图片和营销物料；不确定时可先用平衡方案。"
        topic = "纸张选择"
    elif any(term in text for term in ("出血", "安全边", "裁切")):
        answer = "出血是画面超出成品裁切线的区域，常规建议四边各 3mm；文字和标志要放在安全边距内，避免裁切后贴边。"
        topic = "出血与安全边"
    elif any(term in text for term in ("覆膜", "哑膜", "亮膜")):
        answer = "哑膜触感细腻、反光少，适合高端和阅读场景；亮膜颜色更亮、耐磨性好，适合促销物料。覆膜会增加一点成本和交期。"
        topic = "覆膜选择"
    elif any(term in text for term in ("四色", "专色", "印刷颜色", "黑白")):
        answer = "四色印刷适合大多数彩色文件；专色适合品牌色和高一致性要求；黑白/单色成本较低，适合文字资料。"
        topic = "颜色模式"
    elif any(term in text for term in ("装订", "骑马钉", "胶装")):
        answer = "骑马钉适合页数较少的宣传册，摊平性好；胶装适合页数较多、需要更正式的画册；包装盒通常需要糊盒而不是书刊装订。"
        topic = "装订方式"
    else:
        answer = "我可以解释纸张、出血、颜色、覆膜和装订。你也可以直接告诉我用途、数量、尺寸和交期，我会替你做选择。"
        topic = "印刷基础"
    return {"topic": topic, "answer": answer, "next": "如果愿意，我可以把这个偏好直接写入当前订单。"}


@tool
def preflight_file(file_name: str, size_bytes: int, page_count: int | None = None,
                   encrypted: bool = False, readable: bool = True,
                   inspection: dict[str, Any] | None = None,
                   expected_size: str | None = None) -> dict[str, Any]:
    """检查文件类型、大小和浏览器本地解析出的印前线索。"""
    checks = []
    errors = []
    warnings = []
    suggestions = []
    if not file_name.lower().endswith(".pdf"):
        errors.append("MVP 暂只支持 PDF 文件。")
        checks.append({"label": "文件类型", "status": "error", "detail": "仅支持 PDF"})
    else:
        checks.append({"label": "文件类型", "status": "ok", "detail": "PDF"})
    if size_bytes <= 0:
        errors.append("文件大小无效，请重新选择文件。")
        checks.append({"label": "文件大小", "status": "error", "detail": "大小无效"})
    elif size_bytes > 20 * 1024 * 1024:
        errors.append("文件超过 20 MB，请压缩后再上传。")
        checks.append({"label": "文件大小", "status": "error", "detail": "超过 20 MB"})
    else:
        checks.append({"label": "文件大小", "status": "ok", "detail": f"{size_bytes / 1024 / 1024:.1f} MB"})
    if not readable:
        errors.append("文件内容无法读取，请确认文件未损坏后重新上传。")
        checks.append({"label": "文件内容", "status": "error", "detail": "无法读取"})
    elif encrypted:
        errors.append("PDF 已加密，请先解除密码保护再上传。")
        checks.append({"label": "文件保护", "status": "error", "detail": "已加密"})
    else:
        checks.append({"label": "文件保护", "status": "ok", "detail": "未发现加密标记"})
    if page_count is None:
        warnings.append("无法解析页数，请在下单前人工确认页数。")
        checks.append({"label": "页数", "status": "unknown", "detail": "未解析到"})
    elif page_count <= 0:
        warnings.append("未解析到页数，可能使用了压缩对象流，请人工确认页数。")
        checks.append({"label": "页数", "status": "unknown", "detail": "未解析到"})
    else:
        checks.append({"label": "页数", "status": "ok", "detail": f"{page_count} 页"})
        if page_count >= 49:
            warnings.append("页数较多，请确认装订方式和翻阅强度。")
            suggestions.append("页数较多时优先确认胶装、锁线或特殊装订方案。")
    file_hints = " ".join(part.lower() for part in re.split(r"[\s_-]+", file_name) if part)
    naming_warnings = []
    if "无出血" in file_name or "no-bleed" in file_hints:
        naming_warnings.append("文件名提示可能缺少出血")
    if "rgb" in file_hints:
        naming_warnings.append("文件名提示可能使用 RGB 颜色")
    if "低分辨率" in file_name or "low-res" in file_hints:
        naming_warnings.append("文件名提示可能存在低分辨率图片")
    if naming_warnings:
        warnings.extend(f"{item}，请上传前检查印前设置。" for item in naming_warnings)
        checks.append({"label": "文件命名", "status": "warn", "detail": "；".join(naming_warnings)})
    else:
        checks.append({"label": "文件命名", "status": "ok", "detail": "未发现常见风险标记"})

    # The browser performs a bounded, local PDF metadata scan.  Treat every
    # result as a clue, not a production-grade proof; final preflight stays a
    # human or professional PDF tool responsibility.
    inspection = inspection if isinstance(inspection, dict) else {}
    if inspection:
        if inspection.get("isPdf") is False:
            errors.append("未检测到有效的 PDF 文件头，请重新导出文件。")
            checks.append({"label": "PDF 文件头", "status": "error", "detail": "格式异常"})
        elif inspection.get("isPdf") is True:
            checks.append({"label": "PDF 文件头", "status": "ok", "detail": "格式标记正常"})
        pdf_version = str(inspection.get("pdfVersion") or "").strip()[:12]
        if pdf_version:
            checks.append({"label": "PDF 版本", "status": "info", "detail": f"PDF {pdf_version}"})
        inspected_pages = inspection.get("pageCount")
        if isinstance(inspected_pages, int) and page_count and inspected_pages != page_count:
            warnings.append("浏览器解析页数与提交页数不一致，请人工核对")
            checks.append({"label": "页数一致性", "status": "warn", "detail": f"提交 {page_count} 页 / 解析 {inspected_pages} 页"})
        if inspection.get("hasEof") is False:
            warnings.append("未发现 PDF 结束标记，文件可能未完整导出")
            checks.append({"label": "文件结束标记", "status": "warn", "detail": "未发现 %%EOF"})
        elif inspection.get("hasEof") is True:
            checks.append({"label": "文件结束标记", "status": "ok", "detail": "已发现 %%EOF"})

        boxes = inspection.get("boxes") if isinstance(inspection.get("boxes"), dict) else {}
        media_box = boxes.get("media") if isinstance(boxes.get("media"), list) else None
        trim_box = boxes.get("trim") if isinstance(boxes.get("trim"), list) else None
        bleed_box = boxes.get("bleed") if isinstance(boxes.get("bleed"), list) else None

        def box_detail(box: Any) -> str:
            if not isinstance(box, list) or len(box) != 4:
                return "未解析到"
            try:
                width = abs(float(box[2]) - float(box[0])) * 25.4 / 72
                height = abs(float(box[3]) - float(box[1])) * 25.4 / 72
                return f"约 {width:.1f}×{height:.1f} mm"
            except (TypeError, ValueError):
                return "格式异常"

        if media_box:
            checks.append({"label": "页面 MediaBox", "status": "info", "detail": box_detail(media_box)})
        if trim_box:
            checks.append({"label": "裁切 TrimBox", "status": "ok", "detail": box_detail(trim_box)})
        else:
            warnings.append("未解析到 TrimBox，成品裁切尺寸需要人工确认")
            checks.append({"label": "裁切 TrimBox", "status": "unknown", "detail": "未解析到"})
        if bleed_box:
            checks.append({"label": "出血 BleedBox", "status": "ok", "detail": box_detail(bleed_box)})
        else:
            warnings.append("未解析到 BleedBox，出血需要人工确认")
            suggestions.append("确认成品四边通常各预留约 3mm 出血，并检查文字安全边")
            checks.append({"label": "出血 BleedBox", "status": "unknown", "detail": "未解析到"})

        # Compare the requested flat size with TrimBox (or MediaBox when no
        # TrimBox is available). Three-dimensional package sizes stay unknown.
        expected_sizes = []
        for part in str(expected_size or "").split("/"):
            parsed = _parse_size_mm(part.strip())
            if parsed and len(parsed) == 2:
                expected_sizes.append(parsed)
        observed_box = trim_box or media_box
        if expected_sizes and observed_box and len(observed_box) == 4:
            try:
                observed = (abs(float(observed_box[2]) - float(observed_box[0])) * 25.4 / 72,
                            abs(float(observed_box[3]) - float(observed_box[1])) * 25.4 / 72)
                matches = any(
                    (abs(observed[0] - candidate[0]) <= 1 and abs(observed[1] - candidate[1]) <= 1)
                    or (abs(observed[0] - candidate[1]) <= 1 and abs(observed[1] - candidate[0]) <= 1)
                    for candidate in expected_sizes
                )
                observed_detail = f"约 {observed[0]:.1f}×{observed[1]:.1f} mm"
                if matches:
                    checks.append({"label": "成品尺寸一致性", "status": "ok", "detail": f"文件 {observed_detail}"})
                else:
                    warnings.append(f"文件页面尺寸为{observed_detail}，与订单成品尺寸不一致，请确认是否为展开尺寸")
                    checks.append({"label": "成品尺寸一致性", "status": "warn", "detail": f"文件 {observed_detail} / 订单 {expected_size}"})
            except (TypeError, ValueError):
                checks.append({"label": "成品尺寸一致性", "status": "unknown", "detail": "尺寸格式异常"})
        elif expected_sizes:
            checks.append({"label": "成品尺寸一致性", "status": "unknown", "detail": "缺少可比页面框"})

        color_spaces = inspection.get("colorSpaces") if isinstance(inspection.get("colorSpaces"), list) else []
        color_spaces = [str(item)[:24] for item in color_spaces if item][:8]
        if color_spaces:
            checks.append({"label": "颜色空间线索", "status": "info", "detail": "、".join(color_spaces)})
            if "DeviceRGB" in color_spaces:
                warnings.append("检测到 RGB 颜色空间，印刷前请确认是否需要转换为 CMYK")
            if any(item in color_spaces for item in ("Separation", "DeviceN")):
                warnings.append("检测到专色/多色版线索，请确认专色名称和供应商配置")
        else:
            checks.append({"label": "颜色空间线索", "status": "unknown", "detail": "未解析到"})

        font_state = str(inspection.get("fontEmbedding") or "unknown")
        if font_state == "embedded":
            checks.append({"label": "字体嵌入线索", "status": "ok", "detail": "发现字体文件嵌入标记"})
        elif font_state == "missing":
            warnings.append("发现字体可能未嵌入，印刷前请转曲或嵌入字体")
            checks.append({"label": "字体嵌入线索", "status": "warn", "detail": "可能未嵌入"})
        else:
            checks.append({"label": "字体嵌入线索", "status": "unknown", "detail": "无法从轻量扫描确认"})

        image_count = inspection.get("imageCount")
        if isinstance(image_count, int) and image_count > 0:
            checks.append({"label": "图片对象", "status": "info", "detail": f"发现 {image_count} 个图片对象；分辨率需专业工具确认"})
        else:
            checks.append({"label": "图片对象", "status": "unknown", "detail": "未解析到"})
        if inspection.get("hasTransparency"):
            warnings.append("检测到透明度对象，需确认扁平化和叠印效果")
            checks.append({"label": "透明度线索", "status": "warn", "detail": "发现透明度对象"})
        if inspection.get("hasOverprint"):
            warnings.append("检测到叠印线索，请确认黑版和专色叠印设置")
            checks.append({"label": "叠印线索", "status": "warn", "detail": "发现叠印标记"})
    if errors:
        message = errors[0]
    elif warnings:
        page_summary = f"已解析到 {page_count} 页；" if page_count and page_count > 0 else ""
        message = f"基础检查完成，{page_summary}有 {len(warnings)} 项需要人工确认：" + "；".join(warnings)
    else:
        message = "基础检查通过；出血、颜色和字体仍需正式印前检查。"
    return {"ok": not errors, "message": message, "fileName": file_name, "sizeBytes": size_bytes,
            "pageCount": page_count, "encrypted": encrypted, "checks": checks,
            "warnings": warnings, "suggestions": suggestions,
            "inspectionLevel": "metadata" if inspection else "basic"}


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


@tool
def match_supplier_capability(order: dict[str, Any], platform_id: str | None = None) -> dict[str, Any]:
    """将标准订单与供应商能力档案逐字段匹配，返回支持、待确认和不支持项。"""
    selected_id = platform_id or order.get("platform") or "generic"
    platform = PLATFORMS.get(selected_id, PLATFORMS["generic"])
    profile = platform.get("supplierProfile", {})
    supported: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []

    def review(field: str, message: str) -> None:
        needs_review.append({"field": field, "message": message})

    def support(field: str, value: Any) -> None:
        supported.append({"field": field, "value": value})

    product_type = order.get("productType", "")
    categories = profile.get("categories", [])
    if product_type and (product_type in categories or "全品类" in categories):
        support("品类", product_type)
    elif product_type:
        unsupported.append({"field": "品类", "message": f"供应商档案未明确支持{product_type}"})
    else:
        review("品类", "尚未确定印刷品品类")

    paper = order.get("paper", "")
    papers = profile.get("papers", [])
    if paper:
        if not papers or "按供应商确认" in papers:
            review("纸张/材料", "纸张能力需要供应商人工确认")
        elif any(item.lower() in paper.lower() for item in papers):
            support("纸张/材料", paper)
        else:
            unsupported.append({"field": "纸张/材料", "message": f"{paper}不在常用材料档案中"})

    finishing = order.get("finishing", "")
    finishings = profile.get("finishing", [])
    if finishing:
        if not finishings or "按供应商确认" in finishings:
            review("表面工艺", "表面工艺能力需要供应商人工确认")
        else:
            matched = [item for item in finishings if item.lower() in finishing.lower()]
            if matched:
                support("表面工艺", "、".join(matched))
            else:
                unsupported.append({"field": "表面工艺", "message": f"{finishing}不在常用工艺档案中"})

    size = order.get("size", "")
    max_size = profile.get("maxSize", "")
    if size:
        limit = _parse_max_size(max_size)
        actual = _parse_size_mm(size.split(" / ")[0])
        if max_size in {"", "按供应商确认", "待补充"}:
            review("成品尺寸", f"请由供应商确认{size}的可生产范围")
        elif limit and actual:
            actual_sorted, limit_sorted = sorted(actual), sorted(limit)
            if len(actual_sorted) > len(limit_sorted):
                review("成品尺寸", f"{size}包含结构尺寸，需供应商按刀模和展开尺寸确认")
            elif len(actual_sorted) == len(limit_sorted) and all(a <= b for a, b in zip(actual_sorted, limit_sorted)):
                support("成品尺寸", size)
                review("成品尺寸", f"档案范围已覆盖{size}，仍需确认成品/展开尺寸和出血要求")
            else:
                unsupported.append({"field": "成品尺寸", "message": f"{size}可能超过供应商最大尺寸{max_size}"})
        else:
            review("成品尺寸", f"请确认{size}与供应商最大尺寸{max_size}的关系")

    deadline = order.get("deadline", "")
    if deadline:
        lead_time = profile.get("leadTime", "按供应商确认")
        if lead_time in {"", "按供应商确认", "待补充"}:
            review("交期", f"请由供应商确认{deadline}是否可交付")
        else:
            review("交期", f"静态档案参考交期为{lead_time}，仍需确认{deadline}")

    product_profile = parameter_state(order)
    if product_profile.get("parameters"):
        filled = [item["label"] for item in product_profile["parameters"] if item.get("filled")]
        if filled:
            review("品类参数", f"已填写{('、'.join(filled))}，需映射到供应商字段并人工确认")

    multi_product = _multi_product_info(order)
    if multi_product:
        label = "、".join(multi_product) if len(multi_product) > 1 else "多个订单项"
        items = order.get("items") if isinstance(order.get("items"), list) else []
        split_ready = items and all(isinstance(item, dict) and item.get("selectedOption") for item in items)
        review("多产品订单", f"{label}已拆分，将按产品项分别确认能力" if split_ready
               else f"检测到{label}，需拆分为独立订单项后再询价")

    status = "unsupported" if unsupported else "review" if needs_review else "ready"
    denominator = len(supported) + len(needs_review) + len(unsupported)
    confidence = round(len(supported) / denominator * 100) if denominator else 0
    return {"platformId": selected_id, "platform": platform["name"], "status": status,
            "confidence": confidence, "supported": supported, "needsReview": needs_review,
            "unsupported": unsupported, "profile": profile,
            "knowledgeVersion": KNOWLEDGE_VERSION,
            "supplierProfileVersion": SUPPLIER_PROFILE_VERSION,
            "multiProduct": multi_product,
            "requiresHumanConfirmation": status != "ready" or bool(needs_review)}


@tool
def request_supplier_quote(order: dict[str, Any], platform_id: str | None = None) -> dict[str, Any]:
    """Prepare a supplier quote request without sending anything externally."""
    selected_id = platform_id or order.get("platform") or "generic"
    platform = PLATFORMS.get(selected_id, PLATFORMS["generic"])
    capability = match_supplier_capability(order, selected_id)
    multi_product = capability.get("multiProduct") or _multi_product_info(order)
    if capability.get("unsupported") or multi_product:
        message = ("当前订单包含多个产品项，未生成合并询价请求；请在当前产品项中分别询价。"
                   if multi_product else "当前平台存在明确不支持项，未生成询价请求；请切换平台或先人工确认。")
        return {
            "status": "blocked",
            "platformId": selected_id,
            "platform": platform["name"],
            "capabilityStatus": capability.get("status"),
            "knowledgeVersion": KNOWLEDGE_VERSION,
            "supplierProfileVersion": SUPPLIER_PROFILE_VERSION,
            "unsupported": capability["unsupported"],
            "multiProduct": multi_product,
            "requiresHumanConfirmation": True,
            "message": message,
        }
    adapter = get_adapter(selected_id)
    return {
        **adapter.prepare_quote_request(order, capability),
        "knowledgeVersion": KNOWLEDGE_VERSION,
        "supplierProfileVersion": SUPPLIER_PROFILE_VERSION,
    }


@tool
def prepare_handoff(order: dict[str, Any]) -> dict[str, Any]:
    """按目标平台生成标准化订单交接文本。"""
    platform_id = order.get("platform") or "generic"
    platform = PLATFORMS.get(platform_id, PLATFORMS["generic"])
    adapter = get_adapter(platform_id)
    supplier_profile = platform.get("supplierProfile", {})
    readiness = match_supplier_capability(order)
    multi_product = _multi_product_info(order)
    if multi_product:
        return {
            "status": "blocked",
            "platform": platform,
            "adapter": {"platformId": adapter.platform_id, "mode": adapter.mode},
            "mappedOrder": {},
            "text": "订单包含多个产品项，需分别完成各项确认后再生成整体交接单。",
            "productProfile": parameter_state(order),
            "supplierReadiness": readiness,
            "knowledgeVersion": KNOWLEDGE_VERSION,
            "multiProduct": multi_product,
            "requiresHumanConfirmation": True,
        }
    dimensions = order.get("dimensions") if isinstance(order.get("dimensions"), dict) else {}
    fields = [(key, LABELS[key]) for key in LABELS if key != "platform"
              and not (key == "size" and dimensions.get("packageSize") and not dimensions.get("finishedSize"))]
    lines = [f"目标平台：{platform['name']}"] + [f"{label}：{order[key] or '未填写'}" for key, label in fields]
    dimension_lines = [f"{DIMENSION_LABELS[key]}：{dimensions.get(key)}"
                       for key in DIMENSION_LABELS if dimensions.get(key)]
    if dimension_lines:
        lines.append("尺寸定义：")
        lines.extend(f"- {line}" for line in dimension_lines)
    profile = parameter_state(order)
    if profile["parameters"]:
        lines.append(f"品类分类：{profile['category']}")
        lines.append("品类参数：")
        lines.extend(f"- {item['label']}：{item['value'] or '未填写'}" for item in profile["parameters"])
    text = "\n".join(lines)
    return {"status": "ready", "platform": platform, "adapter": {"platformId": adapter.platform_id, "mode": adapter.mode},
            "mappedOrder": adapter.map_order(order), "text": text, "productProfile": profile,
            "supplierReadiness": readiness,
            "knowledgeVersion": KNOWLEDGE_VERSION,
            "requiresHumanConfirmation": True}


@tool
def estimate_price(order: dict[str, Any]) -> dict[str, Any]:
    """根据订单字段估算价格区间，不替代印刷厂正式报价。"""
    multi_product = _multi_product_info(order)
    if multi_product:
        return {"type": "estimate", "status": "blocked", "range": None, "missing": [],
                "multiProduct": multi_product, "assumptions": "多个产品项不能合并估算；请拆分后分别估算。",
                "knowledgeVersion": KNOWLEDGE_VERSION, "requiresHumanConfirmation": True}
    missing = [LABELS[key] for key in ("productType", "quantity", "size", "printing") if not order.get(key)]
    if missing:
        return {"type": "estimate", "range": None, "missing": missing,
                "assumptions": "至少需要印刷品、数量、尺寸和印刷颜色后才能估算。",
                "knowledgeVersion": KNOWLEDGE_VERSION, "requiresHumanConfirmation": True}
    quantity = _number(order.get("quantity", "")) or 500
    base = 180 if order.get("productType") in {"名片", "折页", "单页"} else 520
    if order.get("productType") in {"包装盒", "手提袋", "纸杯", "标签"}:
        base *= 1.35
    unit = max(0.35, base / max(quantity, 1))
    if order.get("finishing") and order["finishing"] not in {"无特殊工艺", "待推荐"}: unit *= 1.35
    low, high = round(quantity * unit * 0.85), round(quantity * unit * 1.25)
    return {"type": "estimate", "range": f"¥{low} - ¥{high}", "assumptions": "按常规纸张、四色印刷和当前数量估算，未含运输及特殊打样。", "knowledgeVersion": KNOWLEDGE_VERSION, "requiresHumanConfirmation": True}


@tool
def validate_order(order: dict[str, Any]) -> dict[str, Any]:
    """校验订单字段完整性，输出阻塞项、风险和下一步建议。"""
    required_keys = required_order_keys(order)
    missing = [LABELS[key] for key in required_keys if not order.get(key)]
    warnings = []
    suggestions = []
    risks = []
    profile = parameter_state(order)
    product_missing = [item["label"] for item in profile["missing"]]
    multi_product = _multi_product_info(order)
    if multi_product:
        label = "、".join(multi_product) if len(multi_product) > 1 else "多个订单项"
        warnings.append(f"检测到多个印刷品：{label}，当前需要拆分为独立订单项")
        suggestions.append("请分别确认每个产品的数量、尺寸、材料和交期，再分别询价")
    if order.get("productType") in {"宣传册", "画册"} and not order.get("binding"):
        warnings.append("宣传册/画册尚未确认装订方式")
        suggestions.append("页数少于 48 页可优先考虑骑马钉，页数较多再考虑胶装")
    page_count = _number(order.get("pages", ""))
    if (order.get("productType") in {"宣传册", "画册"} and order.get("binding") == "骑马钉"
            and page_count is not None and page_count >= 48):
        message = f"{page_count} 页画册使用骑马钉，装订强度和摊平度可能不足"
        warnings.append(message)
        suggestions.append("页数达到 48 页或更多时，优先确认胶装、锁线胶装或特殊装订")
        risks.append({"level": "warning", "message": message, "suggestion": suggestions[-1]})
    if order.get("finishing") in {"烫金", "烫金 / 击凸"}:
        warnings.append("烫金需要确认文件专色、线条粗细和加急交期")
    if order.get("printing") == "双面四色" and order.get("productType") in {"宣传册", "画册"} and not order.get("pages"):
        suggestions.append("补充页数后才能准确判断装订和纸张克重")
    if order.get("size") and any(re.fullmatch(r"\d+×\d+(?:×\d+)?", part) for part in order["size"].split(" / ")):
        warnings.append("自定义尺寸未注明单位，请确认是 mm 还是 cm")
    if product_missing:
        warnings.append(f"{order.get('productType') or '该品类'}还需确认：{'、'.join(product_missing)}")
        suggestions.append(profile["missing"][0]["question"])
    if order.get("productType") == "包装盒" and order.get("size") and not (order.get("productSpecs") or {}).get("boxSize"):
        suggestions.append("包装盒请补充长×宽×高，并区分内尺寸/外尺寸")
    if order.get("productType") in {"喷画", "海报", "PVC"} and (order.get("productSpecs") or {}).get("install"):
        if "户外" in str((order.get("productSpecs") or {}).get("install")) and order.get("productType") == "海报":
            warnings.append("户外展示请确认介质耐候性与安装安全")
    quantity = _number(order.get("quantity", ""))
    if quantity is not None and quantity <= 0:
        warnings.append("数量必须大于 0")
    readiness = round((len(required_keys) - len(missing)) / len(required_keys) * 100) if required_keys else 100
    item_validations = _validate_order_items(order) if multi_product else []
    item_readiness = round(sum(item["readiness"] for item in item_validations) / len(item_validations)) if item_validations else None
    if item_validations and all(item.get("ok") for item in item_validations) \
            and all(item.get("selectedOption") for item in (order.get("items") or []) if isinstance(item, dict)):
        warnings = [item for item in warnings if "检测到多个印刷品" not in item]
        suggestions = [item for item in suggestions if "分别确认每个产品" not in item]
    return {"ok": not missing and quantity != 0 and not multi_product, "missing": missing,
            "multiProduct": multi_product, "productMissing": product_missing,
            "productProfile": profile, "warnings": warnings, "suggestions": suggestions,
            "risks": risks, "readiness": readiness, "productReadiness": profile["readiness"],
            "itemValidations": item_validations, "itemReadiness": item_readiness,
            "knowledgeVersion": KNOWLEDGE_VERSION}


def _validate_order_items(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate each product item against its own product profile.

    Top-level order fields stay available for backwards compatibility, but a
    multi-product order must use these per-item results before recommendation,
    quote, or handoff actions are enabled.
    """
    items = order.get("items") if isinstance(order.get("items"), list) else []
    shared_fields = ("purpose", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget")
    results: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            continue
        item = deepcopy(raw_item)
        item["items"] = []
        item["productTypes"] = []
        for key in shared_fields:
            if not item.get(key) and order.get(key):
                item[key] = deepcopy(order[key])
        item_result = validate_order(item)
        ready = bool(item_result.get("ok")) and not item_result.get("productMissing")
        results.append({
            "itemId": str(raw_item.get("itemId") or f"item-{index + 1}"),
            "index": index,
            "productType": raw_item.get("productType") or "",
            "ok": ready,
            "status": "ready" if ready else "needs_input",
            "missing": item_result.get("missing", []),
            "productMissing": item_result.get("productMissing", []),
            "warnings": item_result.get("warnings", []),
            "risks": item_result.get("risks", []),
            "parameters": [{"key": parameter.get("key"), "label": parameter.get("label"),
                            "value": parameter.get("value", ""), "filled": parameter.get("filled", False),
                            "required": parameter.get("required", False)}
                           for parameter in item_result.get("productProfile", {}).get("parameters", [])
                           if parameter.get("key")],
            "readiness": round((item_result.get("readiness", 0) + item_result.get("productReadiness", 0)) / 2),
            "productReadiness": item_result.get("productReadiness", 0),
        })
    return results


class Agent:
    def __init__(self, memory: Memory, session_id: str | None = None, planner: Any = None) -> None:
        self.memory, self.id, self.planner = memory, session_id or uuid.uuid4().hex[:12], planner
        self.state = memory.load(self.id)
        self.trace: list[str] = []
        self.run_id = ""
        self.run_operation = ""
        self.run_events: list[dict[str, Any]] = []

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _begin_run(self, operation: str) -> None:
        """Start a bounded, inspectable run while keeping the old trace API."""
        self.run_id = uuid.uuid4().hex[:16]
        self.run_operation = operation
        self.run_events = []
        self._event("run", "started", f"开始{operation}")

    def _invalidate_delivery_state(self) -> None:
        """Invalidate generated handoff data after an order-affecting change."""
        self._mark_quote_requests_stale("订单字段、文件或目标平台发生变化")
        self.state["orderGenerated"] = False
        self.state["handoff"] = None
        self.state["confirmation"] = {"status": "not_ready"}

    def _mark_quote_requests_stale(self, reason: str) -> None:
        """Mark pending quote requests stale so changed orders cannot be reused."""
        requests = self.state.get("quoteRequests")
        if not isinstance(requests, list):
            self.state["quoteRequests"] = []
            return
        now = self._timestamp()
        for request in requests:
            if not isinstance(request, dict) or request.get("status") not in QUOTE_ACTIVE_STATUSES:
                continue
            request["status"] = "stale"
            request["updatedAt"] = now
            request["staleAt"] = now
            request["staleReason"] = reason
        active_id = self.state.get("activeQuoteRequestId")
        if active_id and not any(request.get("requestId") == active_id and request.get("status") in QUOTE_ACTIVE_STATUSES
                                 for request in requests if isinstance(request, dict)):
            self.state["activeQuoteRequestId"] = None

    def _quote_request(self, request_id: str | None = None) -> dict[str, Any] | None:
        requests = self.state.get("quoteRequests")
        if not isinstance(requests, list):
            return None
        target = request_id or self.state.get("activeQuoteRequestId")
        if target:
            return next((item for item in requests if isinstance(item, dict) and item.get("requestId") == target), None)
        return next((item for item in reversed(requests) if isinstance(item, dict)), None)

    def _persist_quote_request(self, result: dict[str, Any], order: dict[str, Any],
                               platform_id: str, item_index: int | None = None) -> dict[str, Any]:
        """Persist a quote preparation result and reuse active identical requests."""
        if result.get("status") != "awaiting_human_confirmation":
            return result
        item_id = None
        if item_index is not None:
            item_id = str(order.get("itemId") or f"item-{item_index + 1}")
        key = quote_idempotency_key(order, platform_id, item_id)
        requests = self.state.setdefault("quoteRequests", [])
        if not isinstance(requests, list):
            requests = []
            self.state["quoteRequests"] = requests
        existing = next((item for item in reversed(requests)
                         if isinstance(item, dict) and item.get("idempotencyKey") == key
                         and item.get("status") in QUOTE_ACTIVE_STATUSES), None)
        if existing:
            reused = deepcopy(existing)
            reused["idempotent"] = True
            reused["message"] = "相同订单已经存在待确认询价请求，已复用原请求，不会重复提交。"
            return reused
        now = self._timestamp()
        request = deepcopy(result)
        request.update({
            "requestId": f"quote-{uuid.uuid4().hex[:16]}",
            "idempotencyKey": key,
            "status": "awaiting_human_confirmation",
            "platformId": platform_id,
            "itemId": item_id,
            "itemIndex": item_index,
            "orderFingerprint": key.removeprefix("quote:"),
            "createdAt": now,
            "updatedAt": now,
            "idempotent": False,
        })
        requests.append(request)
        self.state["quoteRequests"] = requests[-MAX_QUOTE_REQUESTS:]
        self.state["activeQuoteRequestId"] = request["requestId"]
        return deepcopy(request)

    def _bind_uploaded_file(self, file_name: str, item_index: int | None = None) -> None:
        """Bind a checked file to one product item, or to the single-order draft."""
        items = self.state["order"].get("items")
        if isinstance(items, list) and len(items) > 1:
            if item_index is None or not (0 <= item_index < len(items)):
                return
            item = items[item_index]
            item["uploadedFile"] = file_name
            item_id = item.get("itemId") or f"item-{item_index + 1}"
            files = [entry for entry in (self.state.get("uploadedFiles") or [])
                     if isinstance(entry, dict) and entry.get("itemId") != item_id]
            files.append({"itemId": item_id, "itemIndex": item_index, "fileName": file_name})
            self.state["uploadedFiles"] = files
            # Keep the legacy field useful for the currently focused item.
            self.state["uploadedFile"] = file_name
            return
        self.state["uploadedFile"] = file_name
        self.state["uploadedFiles"] = [{"itemId": None, "itemIndex": None, "fileName": file_name}]

    def _event(self, step: str, status: str = "ok", detail: str = "", **extra: Any) -> None:
        if not self.run_id:
            return
        event = {"step": step, "status": status, "detail": detail, "at": self._timestamp()}
        event.update({key: value for key, value in extra.items() if value is not None})
        self.run_events.append(event)
        if len(self.run_events) > MAX_RUN_EVENTS:
            self.run_events = self.run_events[-MAX_RUN_EVENTS:]

    def _finish_run(self, status: str = "completed") -> dict[str, Any] | None:
        if not self.run_id:
            return self.state.get("lastRun")
        self._event("run", status, "运行完成" if status == "completed" else "运行结束")
        record = {"runId": self.run_id, "operation": self.run_operation, "status": status,
                  "startedAt": self.run_events[0]["at"] if self.run_events else self._timestamp(),
                  "finishedAt": self._timestamp(), "events": deepcopy(self.run_events)}
        history = list(self.state.get("runHistory") or [])
        history.append(record)
        self.state["runHistory"] = history[-MAX_RUN_HISTORY:]
        self.state["lastRun"] = record
        self.run_id = ""
        self.run_operation = ""
        self.run_events = []
        return record

    def _item_index(self, value: Any = None) -> int | None:
        """Resolve an explicit or active product-item index safely."""
        items = self.state["order"].get("items")
        candidate = self.state.get("activeItemIndex") if value is None else value
        try:
            candidate = int(candidate)
        except (TypeError, ValueError):
            return None
        return candidate if isinstance(items, list) and len(items) > 1 and 0 <= candidate < len(items) else None

    def _item_order(self, index: int) -> dict[str, Any] | None:
        """Build a standalone order view for one item and inherit only shared fields."""
        items = self.state["order"].get("items")
        if not isinstance(items, list) or not (0 <= index < len(items)) or not isinstance(items[index], dict):
            return None
        item = deepcopy(items[index])
        item["items"] = []
        item["productTypes"] = []
        item["platform"] = self.state["order"].get("platform") or "generic"
        for key in ("purpose", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget"):
            if not item.get(key) and self.state["order"].get(key):
                item[key] = deepcopy(self.state["order"][key])
        top_dimensions = self.state["order"].get("dimensions") if isinstance(self.state["order"].get("dimensions"), dict) else {}
        item_dimensions = item.get("dimensions") if isinstance(item.get("dimensions"), dict) else {}
        if "/" not in str(self.state["order"].get("size") or ""):
            item["dimensions"] = {
                key: item_dimensions.get(key) or top_dimensions.get(key) or ""
                for key in DIMENSION_DEFAULTS
            }
        return item

    def _item_validation(self, index: int) -> dict[str, Any] | None:
        item_order = self._item_order(index)
        if item_order is None:
            return None
        result = validate_order(item_order)
        return {
            "itemId": item_order.get("itemId") or f"item-{index + 1}", "index": index,
            "productType": item_order.get("productType") or "", "ok": bool(result.get("ok")) and not result.get("productMissing"),
            "missing": result.get("missing", []), "productMissing": result.get("productMissing", []),
            "warnings": result.get("warnings", []), "risks": result.get("risks", []),
            "parameters": [{"key": parameter.get("key"), "label": parameter.get("label"),
                            "value": parameter.get("value", ""), "filled": parameter.get("filled", False),
                            "required": parameter.get("required", False)}
                           for parameter in result.get("productProfile", {}).get("parameters", [])
                           if parameter.get("key")],
            "readiness": round((result.get("readiness", 0) + result.get("productReadiness", 0)) / 2),
            "productReadiness": result.get("productReadiness", 0),
        }

    def _recommend_item(self, index: int) -> list[dict[str, str]]:
        item_order = self._item_order(index)
        if item_order is None:
            return []
        validation = validate_order(item_order)
        if not validation.get("ok") or validation.get("productMissing"):
            return []
        options = self._call("recommend_processes", item_order)
        item_id = item_order.get("itemId") or f"item-{index + 1}"
        self.state.setdefault("itemOptions", {})[item_id] = deepcopy(options)
        self.state["workflowStage"] = "recommend"
        return options

    def _prepare_quote_request(self, item_index: int | None = None,
                               platform_id: str | None = None) -> dict[str, Any]:
        """Prepare and persist one quote request without contacting a supplier."""
        selected_id = str(platform_id or self.state["order"].get("platform") or "generic")
        resolved_index = self._item_index(item_index)
        item_order = self._item_order(resolved_index) if resolved_index is not None else None
        quote_order = item_order or self.state["order"]
        validation = validate_order(quote_order)
        missing = list(validation.get("missing") or []) + list(validation.get("productMissing") or [])
        selected = bool(item_order.get("selectedOption")) if item_order is not None else bool(self.state.get("selectedOption"))
        if missing or not selected:
            if missing:
                self.state["workflowStage"] = "collect" if validation.get("missing") else "clarify"
                message = f"当前信息还不完整，暂不能询价。请先补充：{'、'.join(missing)}。"
            else:
                self.state["workflowStage"] = "recommend"
                message = "请先选择工艺方案，再生成询价请求。"
            return {
                "status": "blocked", "reason": "order_not_ready", "missing": missing,
                "requiresHumanConfirmation": True, "message": message,
            }
        result = self._call("request_supplier_quote", quote_order, selected_id)
        if resolved_index is not None and item_order is not None:
            result = {**result, "itemId": item_order.get("itemId"), "itemIndex": resolved_index}
        if result.get("status") == "blocked":
            self.state["workflowStage"] = "clarify"
            return result
        result = self._persist_quote_request(result, quote_order, selected_id, resolved_index)
        self.state["workflowStage"] = "quote"
        return result

    def _base_workflow_stage(self, validation: dict[str, Any] | None = None) -> str:
        validation = validation or validate_order(self.state["order"])
        if validation.get("multiProduct"):
            active_index = self._item_index()
            active_validation = next((item for item in validation.get("itemValidations", [])
                                      if item.get("index") == active_index), None)
            items = self.state["order"].get("items") or []
            item_validations = validation.get("itemValidations") or []
            if item_validations and all(item.get("ok") for item in item_validations) \
                    and all(item.get("selectedOption") for item in items if isinstance(item, dict)):
                return "confirm"
            active_item = items[active_index] if active_validation and isinstance(active_index, int) else None
            if active_validation and active_validation.get("ok") and active_item and not active_item.get("selectedOption"):
                return "recommend"
            return "clarify"
        if validation.get("missing"):
            return "collect"
        if validation.get("productMissing"):
            return "clarify"
        if not self.state.get("selectedOption"):
            return "recommend"
        return "confirm"

    def _workflow_stage(self, validation: dict[str, Any] | None = None) -> str:
        stored = self.state.get("workflowStage")
        if stored in {"preflight", "quote", "export"}:
            return stored
        return self._base_workflow_stage(validation)

    def _set_field_meta(self, key: str, value: Any, source: str, confidence: float) -> None:
        if value in (None, ""):
            self.state.setdefault("fieldMeta", {}).pop(key, None)
            return
        self.state.setdefault("fieldMeta", {})[key] = {
            "value": deepcopy(value), "source": source,
            "sourceLabel": FIELD_SOURCE_LABELS.get(source, source),
            "confidence": round(max(0.0, min(1.0, float(confidence))), 2),
            "runId": self.run_id or None, "updatedAt": self._timestamp(),
        }

    def _low_confidence_fields(self) -> list[str]:
        return [key for key, meta in (self.state.get("fieldMeta") or {}).items()
                if meta.get("value") not in (None, "", {}) and float(meta.get("confidence", 1)) < 0.75]

    def _record_conflict(self, key: str, previous: Any, current: Any, source: str) -> None:
        if previous in (None, "", {}) or current in (None, "", {}) or previous == current:
            return
        conflicts = list(self.state.get("conflicts") or [])
        conflicts.append({"field": key, "label": LABELS.get(key, key), "previous": deepcopy(previous),
                          "current": deepcopy(current), "source": source,
                          "sourceLabel": FIELD_SOURCE_LABELS.get(source, source),
                          "resolved": True, "runId": self.run_id or None, "at": self._timestamp()})
        self.state["conflicts"] = conflicts[-20:]

    @staticmethod
    def _equivalent_value(left: Any, right: Any) -> bool:
        """Ignore harmless spacing/full-width differences from model output."""
        if left in (None, "") or right in (None, ""):
            return left == right
        normalize = lambda value: re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).lower()
        return normalize(left) == normalize(right)

    def chat(self, text: str, patch: dict[str, str] | None = None, item_index: int | None = None) -> dict[str, Any]:
        self._begin_run("chat")
        self.trace = ["感知需求"]
        self._event("perceive", "ok", "已完成规则感知")
        perceived = self._perceive(text)
        focus_match = re.search(r"第\s*(\d+)\s*项", text or "")
        requested_index = item_index
        if requested_index is None and focus_match and re.search(r"处理|查看|编辑|切换|更新", text or ""):
            try:
                requested_index = int(focus_match.group(1)) - 1
            except ValueError:
                requested_index = -1
        if requested_index is not None:
            items = self.state["order"].get("items")
            if not isinstance(items, list) or not (0 <= requested_index < len(items)):
                requested_index = None
            else:
                self.state["activeItemIndex"] = requested_index
                # A focus-only command should not trigger a second perception
                # pass or overwrite the top-level compatibility fields.
                if not perceived and not patch:
                    self._remember("user", text)
                    product = items[requested_index].get("productType") if isinstance(items[requested_index], dict) else ""
                    message = f"已切换到第 {requested_index + 1} 项{f'：{product}' if product else ''}。请继续补充或确认这一项的参数。"
                    self._remember("assistant", message)
                    self._save()
                    return self._result([message])
        if patch is not None and not isinstance(patch, dict):
            patch = {}
        items = self.state["order"].get("items")
        active_item = self.state.get("activeItemIndex")
        target_item = (isinstance(items, list) and isinstance(active_item, int)
                       and 0 <= active_item < len(items) and len(items) > 1)
        if target_item:
            changed_item = self._update_item(active_item, perceived, source="rule", confidence=0.84)
            if patch:
                changed_item |= self._update_item(active_item, patch, source="user", confidence=1.0)
            changed_fields = [f"items.{items[active_item].get('itemId', f'item-{active_item + 1}')}.{key}" for key in changed_item]
        else:
            changed = self._update_order(perceived, source="rule", confidence=0.84)
            if patch:
                changed |= self._update_order(patch, source="user", confidence=1.0)
            changed_fields = list(changed)
        self.state["workflowStage"] = self._base_workflow_stage()
        self._event("memory", "ok", "已更新订单记忆", changedFields=changed_fields)
        self._remember("user", text)

        multi_products = _multi_product_info(self.state["order"])
        if multi_products:
            self.state["stage"] = "collect"
            self.state["workflowStage"] = "clarify"
            validation = self._call("validate_order", self.state["order"])
            item_validations = validation.get("itemValidations") or []
            current_item = next((item for item in item_validations if item.get("index") == active_item), None)
            item_options: list[dict[str, str]] = []
            if target_item and current_item:
                self._event("clarify", "ok" if current_item.get("ok") else "blocked", "已更新独立产品项",
                            itemId=current_item.get("itemId"), readiness=current_item.get("readiness"))
                missing = list(current_item.get("missing") or []) + list(current_item.get("productMissing") or [])
                item_name = f"（{current_item.get('productType')}）" if current_item.get("productType") else ""
                if missing:
                    message = (f"已更新第 {active_item + 1} 项{item_name}，"
                               f"当前信息度 {current_item.get('readiness', 0)}%。还需确认：{'、'.join(missing)}。")
                else:
                    item_options = self._recommend_item(active_item)
                    message = (f"已更新第 {active_item + 1} 项{item_name}，当前信息度 100%。"
                               f"我为这一项生成了 {len(item_options)} 个工艺方案，请选择后再继续询价。")
            else:
                self._event("clarify", "blocked", "检测到多个产品，需要拆分订单项", products=multi_products)
                message = (f"我识别到多个印刷品：{'、'.join(multi_products)}。"
                            "它们的尺寸、材料和后道不同，当前不能合并成一张订单或直接报价。"
                            "请先分别确认每个产品的数量、尺寸、材料和交期，我会按独立订单项继续。")
            self._remember("assistant", message)
            self._save()
            return self._result([message], options=item_options, tool_result=item_options or validation)

        # An optional LLM planner improves language understanding while field
        # patches and tool names remain constrained by this Agent.
        if self.planner and getattr(self.planner, "enabled", False):
            self.trace.append(f"调用模型：{getattr(self.planner, 'model', '已配置模型')}")
            self._event("plan", "started", "请求模型规划", model=getattr(self.planner, "model", "已配置模型"))
            plan = self._ask_planner(text)
            last_tool_name = ""
            last_tool_response: dict[str, Any] | None = None
            final_reply = ""
            for tool_round in range(MAX_PLANNER_TOOL_ROUNDS):
                if not isinstance(plan, dict):
                    break
                self._apply_plan(plan)
                planned_tool = plan.get("tool")
                if not isinstance(planned_tool, dict) or planned_tool.get("name") not in TOOLS:
                    if plan.get("reply"):
                        final_reply = str(plan["reply"])
                    break
                tool_name = str(planned_tool["name"])
                if tool_name == last_tool_name:
                    self.trace.append(f"阻止重复工具：{tool_name}")
                    break
                last_tool_name = tool_name
                last_tool_response = self.call_tool(
                    tool_name, planned_tool.get("arguments", {}), preserve_trace=True, remember=False,
                )
                if tool_round + 1 >= MAX_PLANNER_TOOL_ROUNDS:
                    break
                self.trace.append(f"调用模型总结工具结果：{getattr(self.planner, 'model', '已配置模型')}")
                plan = self._ask_planner(text, {"name": tool_name, "result": last_tool_response.get("toolResult")})
            if last_tool_response is not None:
                result = last_tool_response.get("toolResult")
                message = final_reply.strip() or self._planner_tool_fallback(last_tool_name, result)
                self._remember("assistant", message)
                self._save()
                return self._result(
                    [message], options=last_tool_response.get("options", []),
                    handoff=last_tool_response.get("handoff"), tool_result=result,
                )
            if final_reply:
                self.state["stage"] = "collect" if self._missing_fields() else "recommend"
                self._remember("assistant", final_reply)
                self._save()
                return self._result([final_reply])
            if getattr(self.planner, "last_error", ""):
                self.trace.append(f"模型回退：{self.planner.last_error}")
                self._event("plan", "fallback", self.planner.last_error)

        if self._is_explanation_request(text):
            result = self._call("explain_print_term", text)
            message = f"{result['topic']}：{result['answer']}\n\n{result['next']}"
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)

        # Explicit intents let users invoke a tool before the order is complete.
        if any(term in text for term in ("多少钱", "价格", "报价", "预算估算")):
            active_index = self._item_index()
            item_order = self._item_order(active_index) if active_index is not None else None
            result = self._call("estimate_price", item_order or self.state["order"])
            if item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": active_index}
            if result.get("status") == "blocked":
                message = result["assumptions"]
            else:
                message = (f"按当前已填写信息，费用只能做区间估算：{result['range']}。\n{result['assumptions']}"
                           if result["range"] else f"我调用了费用估算工具，但信息还不足。\n还需要：{'、'.join(result['missing'])}。")
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if any(term in text for term in ("询价", "问价", "查报价", "查价格")):
            active_index = self._item_index()
            result = self._prepare_quote_request(active_index, self.state["order"].get("platform"))
            message = result.get("message", "已准备询价请求，正式发送前需要人工确认。")
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if any(term in text for term in ("检查订单", "校验订单", "还缺什么", "检查一下")):
            result = self._call("validate_order", self.state["order"])
            message = self._validation_message(result)
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if any(term in text for term in ("生成订单", "生成草稿", "下单")):
            self._save()
            return self.generate()

        missing = self._missing_fields()
        if missing:
            self.state["stage"] = "collect"
            validation = self._call("validate_order", self.state["order"])
            message = f"{self._summary()}\n\n还需要确认：{self._question(missing[0])}"
            quick = self._quick_replies(missing[0], self.state["order"].get("productType"))
            options: list[dict[str, str]] = []
            tool_result: Any = validation
        else:
            self.state["stage"] = "recommend"
            options = self._call("recommend_processes", self.state["order"])
            profile = parameter_state(self.state["order"])
            quick = self._product_quick_replies(profile["missing"][0]["key"]) if profile["missing"] else []
            product_note = (f"基础订单信息已经齐了。为了让{profile.get('category', '该品类')}对接更准确，建议补充：{profile['missing'][0]['question']}"
                            if profile["missing"] else "信息已经齐了。")
            message = f"{self._summary()}\n\n{product_note}\n我调用工艺推荐工具生成了 3 个可执行方案，请选择一个。"
            tool_result = options
        self._remember("assistant", message)
        self._save()
        return self._result([message], quick, options, tool_result=tool_result)

    def call_tool(self, name: str, payload: dict[str, Any] | None = None, preserve_trace: bool = False,
                  remember: bool = True) -> dict[str, Any]:
        """Public tool gateway used by a UI, MCP bridge, or an LLM planner."""
        if not preserve_trace:
            self._begin_run(f"tool:{name}")
            self.trace = []
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            self.trace.append(f"工具参数无效：{name}")
            self._event("tool", "rejected", f"工具 {name} 参数不是 JSON 对象", tool=name)
            return self._tool_reply("工具参数需要使用 JSON 对象，未执行。", remember=remember)
        if name == "recommend_processes":
            multi_product = _multi_product_info(self.state["order"])
            if multi_product:
                item_index = self._item_index(payload.get("itemIndex"))
                item_order = self._item_order(item_index) if item_index is not None else None
                item_validation = self._item_validation(item_index) if item_index is not None else None
                if item_order is None or not item_validation or not item_validation.get("ok"):
                    label = "、".join(multi_product) if len(multi_product) > 1 else "多个订单项"
                    self.state["stage"] = "collect"
                    self.state["workflowStage"] = "clarify"
                    return self._tool_reply(
                        f"当前订单包含{label}，请先选择一个产品项并补齐该项字段，不能生成合并工艺方案。",
                        tool_result={"status": "blocked", "reason": "multi_product", "multiProduct": multi_product,
                                     "itemValidation": item_validation},
                        remember=remember,
                    )
                result = self._recommend_item(item_index)
                self.state["stage"] = "recommend"
                return self._tool_reply(
                    f"已为第 {item_index + 1} 项生成工艺方案。", options=result,
                    tool_result={"status": "ready", "itemId": item_order.get("itemId"),
                                 "itemIndex": item_index, "options": result}, remember=remember)
            result = self._call(name, self.state["order"])
            self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
            return self._tool_reply("已调用工艺推荐工具。", options=result, tool_result=result, remember=remember)
        if name == "validate_order":
            result = self._call(name, self.state["order"])
            return self._tool_reply(self._validation_message(result), tool_result=result, remember=remember)
        if name == "explain_print_term":
            result = self._call(name, str(payload.get("question", "印刷工艺怎么选？")))
            message = f"{result['topic']}：{result['answer']}\n\n{result['next']}"
            return self._tool_reply(message, tool_result=result, remember=remember)
        if name == "estimate_price":
            item_index = self._item_index(payload.get("itemIndex"))
            item_order = self._item_order(item_index) if item_index is not None else None
            result = self._call(name, item_order or self.state["order"])
            if item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            self.state["workflowStage"] = "clarify" if result.get("status") == "blocked" else "quote"
            message = (result.get("assumptions") if result.get("status") == "blocked"
                       else f"已调用费用估算工具：{result['range']}。" if result["range"]
                       else f"费用估算工具需要更多信息：{'、'.join(result['missing'])}。")
            return self._tool_reply(message, tool_result=result, remember=remember)
        if name == "prepare_handoff":
            item_index = self._item_index(payload.get("itemIndex"))
            item_order = self._item_order(item_index) if item_index is not None else None
            result = self._call(name, item_order or self.state["order"])
            if item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            message = (result.get("text") if result.get("status") == "blocked"
                       else "已调用订单交接工具，生成平台适配文本。")
            return self._tool_reply(message, tool_result=result, handoff=result, remember=remember)
        if name == "match_supplier_capability":
            platform_id = payload.get("platformId") or self.state["order"].get("platform")
            item_index = self._item_index(payload.get("itemIndex"))
            item_order = self._item_order(item_index) if item_index is not None else None
            result = self._call(name, item_order or self.state["order"], str(platform_id) if platform_id else None)
            if item_order is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            return self._tool_reply("已完成供应商能力匹配，请查看支持项与待确认项。", tool_result=result, remember=remember)
        if name == "request_supplier_quote":
            platform_id = payload.get("platformId") or self.state["order"].get("platform")
            item_index = self._item_index(payload.get("itemIndex"))
            result = self._prepare_quote_request(item_index, str(platform_id) if platform_id else None)
            message = result.get("message", "已准备询价请求，正式发送前需要人工确认。")
            return self._tool_reply(message, tool_result=result, remember=remember)
        if name == "preflight_file":
            try:
                size_bytes = int(payload.get("sizeBytes", 0))
            except (TypeError, ValueError):
                size_bytes = 0
            page_count = payload.get("pageCount")
            try:
                page_count = int(page_count) if page_count is not None else None
            except (TypeError, ValueError):
                page_count = None
            has_multi = isinstance(self.state["order"].get("items"), list) and len(self.state["order"]["items"]) > 1
            item_index = self._item_index(payload.get("itemIndex"))
            if has_multi and item_index is None:
                return self._tool_reply("当前订单包含多个产品项，请先选择具体产品项再做文件预检。",
                                        tool_result={"status": "blocked", "reason": "item_required"}, remember=remember)
            item_order = self._item_order(item_index) if item_index is not None else self.state["order"]
            result = self._call(name, str(payload.get("fileName", "")), size_bytes, page_count,
                                payload.get("encrypted") is True, payload.get("readable") is not False,
                                payload.get("inspection"), payload.get("expectedSize") or item_order.get("size"))
            self.state["workflowStage"] = "preflight"
            if result.get("ok"):
                self._bind_uploaded_file(str(payload.get("fileName", "")), item_index)
                self._invalidate_delivery_state()
            if item_index is not None:
                result = {**result, "itemId": item_order.get("itemId"), "itemIndex": item_index}
            return self._tool_reply(result["message"], tool_result=result, remember=remember)
        self._event("tool", "rejected", f"工具 {name} 不在白名单中", tool=name)
        return self._tool_reply(f"工具 {name} 不在白名单中，未执行。", remember=remember)

    def choose(self, option_id: str, item_index: int | None = None) -> dict[str, Any]:
        self._begin_run("choose")
        self.trace = []
        validation = self._call("validate_order", self.state["order"])
        if validation.get("multiProduct"):
            index = self._item_index(item_index)
            item_order = self._item_order(index) if index is not None else None
            item_validation = self._item_validation(index) if index is not None else None
            if item_order is None or not item_validation or not item_validation.get("ok"):
                return self._result(["还不能选择方案。请先选择一个产品项，并补齐该项的基础字段和品类参数。"], tool_result=item_validation or validation)
            item_id = item_order.get("itemId") or f"item-{index + 1}"
            options = deepcopy(self.state.get("itemOptions", {}).get(item_id, []))
            if not options:
                options = self._recommend_item(index)
            option = next((item for item in options if item["id"] == option_id), None)
            if not option:
                return self._result(["没有找到这个产品项的方案。"], [], options)
            updates = {"finishing": option["finishing"], "binding": option.get("binding", item_order.get("binding", ""))}
            if item_order.get("productType") not in MATERIAL_SPEC_PRODUCTS:
                updates["paper"] = option["paper"]
            self._update_item(index, updates, source="recommendation", confidence=0.9)
            self.state["order"]["items"][index]["selectedOption"] = option_id
            all_selected = all(item.get("selectedOption") for item in self.state["order"].get("items", []) if isinstance(item, dict))
            self.state["workflowStage"] = "confirm" if all_selected else "clarify"
            message = f"已为第 {index + 1} 项选择{option['title']}，该项参数已更新。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], [], options, tool_result={"status": "selected", "itemId": item_id,
                                                                       "itemIndex": index, "option": option})
        if not validation["ok"]:
            return self._result([f"还不能选择方案。{self._validation_message(validation)}"], tool_result=validation)
        options = self._call("recommend_processes", self.state["order"])
        option = next((item for item in options if item["id"] == option_id), None)
        if not option:
            return self._result(["没有找到这个方案。"], [], options)
        updates = {"finishing": option["finishing"], "binding": option.get("binding", self.state["order"]["binding"])}
        if self.state["order"].get("productType") not in MATERIAL_SPEC_PRODUCTS:
            updates["paper"] = option["paper"]
        self._update_order(updates, source="recommendation", confidence=0.9)
        self.state["selectedOption"] = option_id
        self.state["workflowStage"] = "confirm"
        message = f"已选择{option['title']}，订单参数已更新。"
        self._remember("assistant", message)
        self._save()
        return self._result([message], [], options)

    def generate(self) -> dict[str, Any]:
        self._begin_run("generate")
        self.trace = []
        if self.state["orderGenerated"]:
            message = ("订单交接单已经确认，可继续导出或交给受控平台适配器。"
                       if (self.state.get("confirmation") or {}).get("status") == "confirmed"
                       else "订单草稿已经生成，正式提交前请确认文件、价格和交期。")
            return self._result([message], handoff=self.state.get("handoff"))
        validation = self._call("validate_order", self.state["order"])
        if validation.get("multiProduct"):
            item_validations = validation.get("itemValidations") or []
            pending = [item for item in item_validations if not item.get("ok")]
            if pending:
                details = []
                for item in pending:
                    missing = list(item.get("missing") or []) + list(item.get("productMissing") or [])
                    item_name = f"（{item.get('productType')}）" if item.get("productType") else ""
                    details.append(f"第 {item.get('index', 0) + 1} 项{item_name}：{'、'.join(missing) or '请检查风险'}")
                message = "订单还不能生成。请先逐项补齐：" + "；".join(details) + "。"
                self._remember("assistant", message)
                self._save()
                return self._result([message], tool_result=validation)
            uncertain = [field for field in self._low_confidence_fields() if field.startswith("items.")]
            if uncertain:
                self._event("approval", "blocked", "产品项存在低置信度字段", fields=uncertain)
                labels = [LABELS.get(field.rsplit(".", 1)[-1], field) for field in uncertain]
                message = f"订单还不能生成。请先确认产品项字段：{'、'.join(labels)}。"
                self._remember("assistant", message)
                self._save()
                return self._result([message], tool_result={"ok": False, "uncertain": uncertain})
            unselected = [item for item in self.state["order"].get("items", [])
                          if isinstance(item, dict) and not item.get("selectedOption")]
            if unselected:
                active_index = self._item_index()
                options: list[dict[str, str]] = []
                if active_index is not None:
                    current = next((item for item in item_validations if item.get("index") == active_index), None)
                    if current and current.get("ok"):
                        options = self._recommend_item(active_index)
                message = "请为每个产品项分别选择工艺方案后，再生成整体交接单。"
                self._remember("assistant", message)
                self._save()
                return self._result([message], options=options, tool_result={"status": "needs_selection",
                                                                                "items": unselected})
            capabilities = []
            handoffs = []
            unsupported: list[str] = []
            for index, item in enumerate(self.state["order"].get("items", [])):
                item_order = self._item_order(index)
                if item_order is None:
                    continue
                capability = self._call("match_supplier_capability", item_order)
                capabilities.append({"itemId": item_order.get("itemId"), "itemIndex": index, **capability})
                unsupported.extend(f"第 {index + 1} 项：{entry.get('field', '能力')}" for entry in capability.get("unsupported", []))
                if not capability.get("unsupported"):
                    handoffs.append(self._call("prepare_handoff", item_order))
            if unsupported:
                message = f"订单还不能生成。目标平台存在不支持项：{'、'.join(unsupported)}。请切换平台或先向供应商确认。"
                self._event("capability", "blocked", "产品项供应商能力不匹配", fields=unsupported)
                self._remember("assistant", message)
                self._save()
                return self._result([message], tool_result={"status": "blocked", "unsupported": unsupported,
                                                             "items": capabilities})
            sections = []
            for index, handoff in enumerate(handoffs):
                product = self.state["order"]["items"][index].get("productType") or f"产品项 {index + 1}"
                sections.append(f"【第 {index + 1} 项：{product}】\n{handoff.get('text', '')}")
                self.state["order"]["items"][index]["orderGenerated"] = True
            aggregate = {"status": "ready", "items": handoffs, "supplierReadiness": capabilities,
                         "text": "\n\n".join(sections), "requiresHumanConfirmation": True}
            self.state.update({"stage": "confirm", "workflowStage": "confirm", "orderGenerated": True,
                               "handoff": deepcopy(aggregate), "confirmation": {"status": "pending"}})
            message = "所有产品项已完成并生成整体交接单。正式提交前仍需要人工确认价格、文件和交期。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], [], [], handoff=aggregate, tool_result=aggregate)
        if not validation["ok"]:
            message = f"订单还不能生成。{self._validation_message(validation)}"
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=validation)
        if validation.get("productMissing"):
            missing = "、".join(validation["productMissing"])
            message = f"订单还不能生成。{self.state['order']['productType']}还缺少品类参数：{missing}。先补充后再生成交接单。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=validation)
        uncertain = self._low_confidence_fields()
        if uncertain:
            labels = [LABELS.get(key, key) for key in uncertain]
            message = f"订单还不能生成。请先确认低置信度字段：{'、'.join(labels)}。确认后再生成订单草稿。"
            self._event("approval", "blocked", "存在低置信度字段", fields=uncertain)
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result={"ok": False, "uncertain": uncertain})
        capability = match_supplier_capability(self.state["order"])
        if capability.get("unsupported"):
            fields = [item["field"] for item in capability["unsupported"]]
            message = f"订单还不能生成。目标平台暂不支持或未登记：{'、'.join(fields)}。请切换平台或先向供应商确认。"
            self._event("capability", "blocked", "供应商能力不匹配", fields=fields)
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=capability)
        if not self.state["selectedOption"]:
            options = self._call("recommend_processes", self.state["order"])
            message = "请先选择工艺方案，再生成订单草稿。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], [], options, tool_result=options)
        handoff = self._call("prepare_handoff", self.state["order"])
        self.state.update({"stage": "confirm", "workflowStage": "confirm", "orderGenerated": True,
                           "handoff": deepcopy(handoff), "confirmation": {"status": "pending"}})
        message = "订单草稿已生成。正式提交前仍需要人工确认价格、文件和交期。"
        self._remember("assistant", message)
        self._save()
        return self._result([message], [], [], handoff)

    def confirm(self, note: str = "") -> dict[str, Any]:
        """Persist an explicit human approval without submitting externally."""
        self._begin_run("confirm")
        self.trace = []
        if not self.state.get("orderGenerated") or not self.state.get("handoff"):
            message = "当前没有可确认的订单交接单，请先完成订单并生成草稿。"
            self._event("approval", "blocked", "没有可确认的交接单")
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result={"status": "blocked", "reason": "handoff_not_ready"})
        confirmation = self.state.get("confirmation") or {}
        if confirmation.get("status") == "confirmed":
            return self._result(["该订单交接单已经确认，无需重复确认。"], handoff=self.state.get("handoff"),
                                tool_result={"status": "confirmed", **confirmation})
        confirmed_at = self._timestamp()
        self.state["confirmation"] = {"status": "confirmed", "confirmedAt": confirmed_at,
                                       "note": str(note or "").strip()[:240]}
        self.state["workflowStage"] = "export"
        self.state["stage"] = "confirm"
        self._event("approval", "ok", "人工确认已记录", confirmedAt=confirmed_at)
        quote_request = self._quote_request()
        if quote_request and quote_request.get("status") == "awaiting_human_confirmation":
            quote_request["status"] = "confirmed"
            quote_request["updatedAt"] = confirmed_at
            quote_request["confirmedAt"] = confirmed_at
            quote_request["confirmationNote"] = str(note or "").strip()[:240]
        self._remember("assistant", "已记录人工确认。当前不会自动向供应商提交，下一步可导出交接包或由受控适配器继续处理。")
        self._save()
        return self._result(["已记录人工确认。当前不会自动向供应商提交，下一步可导出交接包或由受控适配器继续处理。"],
                            handoff=self.state.get("handoff"),
                            tool_result={"status": "confirmed", **self.state["confirmation"]})

    def quote_status(self, request_id: str | None = None) -> dict[str, Any]:
        """Return a persisted quote request without performing any external call."""
        self._begin_run("quote_status")
        self.trace = []
        request = self._quote_request(str(request_id).strip() if request_id else None)
        if request is None:
            message = "当前没有找到询价请求。"
            result = {"status": "not_found", "requestId": request_id}
        else:
            status_labels = {
                "awaiting_human_confirmation": "待人工确认",
                "confirmed": "已确认，等待受控适配器提交",
                "cancelled": "已取消", "stale": "已失效",
                "submitted": "已提交", "failed": "提交失败",
            }
            message = f"询价请求 {request['requestId']} 当前状态：{status_labels.get(request.get('status'), request.get('status', '未知'))}。"
            result = deepcopy(request)
        self._remember("assistant", message)
        self._save()
        return self._result([message], tool_result=result)

    def cancel_quote(self, request_id: str | None = None, reason: str = "用户取消询价") -> dict[str, Any]:
        """Cancel a pending quote request locally; never contact a supplier."""
        self._begin_run("quote_cancel")
        self.trace = []
        request = self._quote_request(str(request_id).strip() if request_id else None)
        if request is None:
            message = "当前没有可取消的询价请求。"
            result = {"status": "not_found", "requestId": request_id}
        elif request.get("status") == "cancelled":
            message = "该询价请求已经取消，无需重复操作。"
            result = deepcopy(request)
            result["idempotent"] = True
        elif request.get("status") not in QUOTE_ACTIVE_STATUSES:
            message = f"该询价请求当前为“{request.get('status', '未知')}”，不能取消。"
            result = {"status": "not_cancellable", "request": deepcopy(request)}
        else:
            now = self._timestamp()
            request["status"] = "cancelled"
            request["updatedAt"] = now
            request["cancelledAt"] = now
            request["cancelReason"] = str(reason or "用户取消询价").strip()[:240]
            if self.state.get("activeQuoteRequestId") == request.get("requestId"):
                self.state["activeQuoteRequestId"] = None
            self.state["workflowStage"] = self._base_workflow_stage()
            message = f"询价请求 {request['requestId']} 已取消，不会向供应商发送。"
            result = deepcopy(request)
        self._remember("assistant", message)
        self._save()
        return self._result([message], tool_result=result)

    def upload(self, file_name: str, size_bytes: int, page_count: int | None = None,
               encrypted: bool = False, readable: bool = True,
               inspection: dict[str, Any] | None = None,
               expected_size: str | None = None,
               item_index: int | None = None) -> dict[str, Any]:
        self._begin_run("preflight")
        self.trace = []
        items = self.state["order"].get("items")
        if isinstance(items, list) and len(items) > 1:
            item_index = self._item_index(item_index)
            if item_index is None:
                message = "当前订单包含多个产品项，请先处理具体产品项，再上传对应 PDF。"
                self._event("preflight", "blocked", "多产品上传缺少产品项")
                self._remember("assistant", message)
                self._save()
                return self._result([message], tool_result={"status": "blocked", "reason": "item_required"})
            item_order = self._item_order(item_index) or self.state["order"]
        else:
            item_order = self.state["order"]
        check = self._call("preflight_file", file_name, size_bytes, page_count, encrypted, readable, inspection,
                           expected_size or item_order.get("size"))
        self.state["workflowStage"] = "preflight"
        if check["ok"]:
            self._bind_uploaded_file(file_name, item_index)
            self._invalidate_delivery_state()
            self._save()
        if item_index is not None:
            check = {**check, "itemId": item_order.get("itemId"), "itemIndex": item_index}
        return self._result([check["message"]], tool_result=check)

    def set_platform(self, platform_id: str) -> dict[str, Any]:
        self._begin_run("set_platform")
        platform_id = platform_id if platform_id in PLATFORMS else "generic"
        self._update_order({"platform": platform_id}, source="user", confidence=1.0)
        self.state["workflowStage"] = self._base_workflow_stage()
        self._save()
        return self._result([f"目标平台已切换为{PLATFORMS[platform_id]['name']}。订单核心字段保持不变。"])

    def snapshot(self) -> dict[str, Any]:
        self._begin_run("snapshot")
        options: list[dict[str, str]] = []
        active_index = self._item_index()
        if active_index is not None:
            item = self._item_order(active_index) or {}
            options = deepcopy(self.state.get("itemOptions", {}).get(item.get("itemId"), []))
        elif self.state["stage"] != "collect":
            options = self._call("recommend_processes", self.state["order"])
        self.trace = ["恢复会话记忆"] if self.state["messages"] else []
        return self._result([], [], options)

    def _call(self, name: str, *args: Any) -> Any:
        self.trace.append(f"调用工具：{name}")
        started = time.monotonic()
        self._event("tool", "started", f"开始调用 {name}", tool=name)
        try:
            result = TOOLS[name](*args)
        except Exception as error:
            self._event("tool", "failed", "工具执行失败", tool=name,
                        error=error.__class__.__name__, durationMs=round((time.monotonic() - started) * 1000))
            raise
        self._event("tool", "ok", "工具执行完成", tool=name,
                    durationMs=round((time.monotonic() - started) * 1000))
        return result

    def _result(self, messages: list[str], quick: list[dict[str, Any]] | None = None,
                options: list[dict[str, str]] | None = None, handoff: dict[str, Any] | None = None,
                tool_result: Any = None) -> dict[str, Any]:
        validation = validate_order(self.state["order"])
        run = self._finish_run()
        # Some callers save before building the response; saving here as well
        # ensures the completed run and field provenance survive a restart.
        self._save()
        workflow_stage = self._workflow_stage(validation)
        supplier_capability = match_supplier_capability(self.state["order"])
        active_index = self._item_index()
        active_item = self.state["order"].get("items", [])[active_index] if isinstance(active_index, int) else None
        handoff_value = handoff if handoff is not None else self.state.get("handoff")
        quote_request = self._quote_request()
        return {"sessionId": self.id, "messages": messages, "quickReplies": quick or [], "options": options or [],
                "order": deepcopy(self.state["order"]), "stage": self.state["stage"],
                "selectedOption": self.state["selectedOption"], "orderGenerated": self.state["orderGenerated"],
                "uploadedFile": self.state["uploadedFile"], "uploadedFiles": deepcopy(self.state.get("uploadedFiles") or []), "toolTrace": self.trace,
                "availableTools": self.available_tools(), "toolResult": tool_result, "handoff": deepcopy(handoff_value),
                "confirmation": deepcopy(self.state.get("confirmation") or {"status": "not_ready"}),
                "quoteRequest": deepcopy(quote_request),
                "quoteRequests": deepcopy(self.state.get("quoteRequests") or []),
                "activeQuoteRequestId": self.state.get("activeQuoteRequestId"),
                "history": deepcopy(self.state["messages"]), "validation": validation,
                "readiness": validation["readiness"],
                "missingFields": validation["missing"],
                "llm": self.planner.public_config() if self.planner and hasattr(self.planner, "public_config") else None,
                "productProfile": parameter_state(self.state["order"]), "nextAction": self._next_action(validation),
                "workflowStage": workflow_stage, "workflowLabel": WORKFLOW_LABELS[workflow_stage],
                "runId": run["runId"] if run else None, "runTrace": deepcopy(run["events"] if run else []),
                "lastRun": deepcopy(run),
                "fieldMeta": deepcopy(self.state.get("fieldMeta") or {}),
                "conflicts": deepcopy(self.state.get("conflicts") or []),
                "activeItemIndex": self.state.get("activeItemIndex"),
                "activeItemSelectedOption": active_item.get("selectedOption") if isinstance(active_item, dict) else None,
                "decision": self._decision_summary(validation, supplier_capability),
                "supplierCapability": supplier_capability,
                "knowledge": deepcopy(KNOWLEDGE_MANIFEST)}

    def _decision_summary(self, validation: dict[str, Any], supplier_capability: dict[str, Any] | None = None) -> dict[str, Any]:
        """Expose the next decision without leaking provider internals."""
        stage = self._workflow_stage(validation)
        if stage == "collect":
            reason = f"基础信息仍缺少：{'、'.join(validation.get('missing', []))}"
        elif stage == "clarify":
            item_validations = validation.get("itemValidations") or []
            pending = [item for item in item_validations if not item.get("ok")]
            if pending:
                names = "、".join(str(item.get("productType") or f"第 {item.get('index', 0) + 1} 项") for item in pending)
                reason = f"需要逐项补齐：{names}"
            else:
                items = self.state["order"].get("items") or []
                unselected = [(index, item.get("productType") or f"第 {index + 1} 项")
                              for index, item in enumerate(items)
                              if isinstance(item, dict) and not item.get("selectedOption")]
                reason = (f"还需为第 {unselected[0][0] + 1} 项（{unselected[0][1]}）选择工艺方案"
                          if unselected else f"需要补齐{self.state['order'].get('productType') or '当前品类'}专属参数")
        elif stage == "recommend":
            active_index = self._item_index()
            if validation.get("multiProduct") and active_index is not None:
                reason = f"第 {active_index + 1} 项基础信息可用，等待比较并选择工艺方案"
            else:
                reason = "基础信息可用，等待比较并选择工艺方案"
        elif stage == "preflight":
            reason = "正在检查文件基础信息，正式印前仍需人工签核"
        elif stage == "quote":
            reason = "已准备费用估算，正式报价仍以供应商回复为准"
        elif stage == "export":
            reason = "订单信息已整理，等待导出或人工交接"
        else:
            reason = "已选择方案，生成前需要人工确认文件、价格和交期"
        capability = supplier_capability or match_supplier_capability(self.state["order"])
        if capability.get("unsupported"):
            reason += "；目标平台存在不支持项"
        elif capability.get("needsReview"):
            reason += "；供应商能力仍需询价确认"
        confirmed = (self.state.get("confirmation") or {}).get("status") == "confirmed"
        return {"stage": stage, "label": WORKFLOW_LABELS[stage], "reason": reason,
                "humanConfirmationRequired": not confirmed and (stage == "confirm" or bool(validation.get("warnings"))
                or capability.get("status") != "ready")}

    def _tool_reply(self, message: str, remember: bool = True, **kwargs: Any) -> dict[str, Any]:
        if remember:
            self._remember("assistant", message)
        self._save()
        return self._result([message], **kwargs)

    @staticmethod
    def available_tools() -> list[dict[str, Any]]:
        return [{**deepcopy(meta), **deepcopy(TOOL_SCHEMAS.get(name, {}))}
                for name, meta in TOOL_META.items()]

    def _summary(self) -> str:
        order = self.state["order"]
        parts = [f"{LABELS[key]}：{order[key]}" for key in ("productType", "quantity", "size", "pages", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget") if order.get(key)]
        profile = parameter_state(order)
        spec_parts = [f"{item['label']}：{item['value']}" for item in profile["parameters"] if item.get("value")]
        if spec_parts:
            parts.append("关键参数：" + "、".join(spec_parts[:3]))
        return "我理解的是：" + "；".join(parts) + "。" if parts else "我还没有识别到有效订单信息。"

    def _missing_fields(self) -> list[str]:
        return [key for key in required_order_keys(self.state["order"]) if not self.state["order"].get(key)]

    def _next_action(self, validation: dict[str, Any]) -> str:
        if self.state.get("orderGenerated"):
            if (self.state.get("confirmation") or {}).get("status") == "confirmed":
                return "下一步：导出交接包或由受控平台适配器继续处理"
            return "下一步：人工确认文件、价格和交期"
        if validation.get("multiProduct"):
            pending = [item for item in (validation.get("itemValidations") or []) if not item.get("ok")]
            if pending:
                item = pending[0]
                missing = list(item.get("missing") or []) + list(item.get("productMissing") or [])
                return f"下一步：处理第 {item.get('index', 0) + 1} 项并补充{missing[0] if missing else '产品参数'}"
            active_index = self._item_index()
            items = self.state["order"].get("items") or []
            active_item = items[active_index] if isinstance(active_index, int) and active_index < len(items) else None
            if active_item and not active_item.get("selectedOption"):
                return f"下一步：比较并选择第 {active_index + 1} 项的工艺方案"
            unselected = [index for index, item in enumerate(items)
                          if isinstance(item, dict) and not item.get("selectedOption")]
            if unselected:
                return f"下一步：处理第 {unselected[0] + 1} 项并选择工艺方案"
            if items and all(item.get("selectedOption") for item in items if isinstance(item, dict)):
                return "下一步：生成多产品交接单并人工确认"
            return "下一步：逐项确认工艺方案后再询价"
        if validation["missing"]:
            return f"下一步：补充{validation['missing'][0]}"
        if validation.get("productMissing"):
            return f"下一步：确认{validation['productMissing'][0]}（{self.state['order'].get('productType') or '当前品类'}专属参数）"
        if not self.state["selectedOption"]:
            return "下一步：比较并选择工艺方案"
        if not self.state["orderGenerated"]:
            return "下一步：确认方案后生成订单草稿"
        return "下一步：人工确认文件、价格和交期"

    def _update_item(self, index: int, changes: dict[str, Any], source: str = "rule",
                     confidence: float = 0.84) -> set[str]:
        """Apply a constrained patch to one product item in a multi-product order."""
        items = self.state["order"].get("items")
        if not isinstance(items, list) or not (0 <= index < len(items)) or not isinstance(changes, dict):
            return set()
        previous = deepcopy(items[index])
        item = deepcopy(previous)
        product = str(changes.get("productType") or item.get("productType") or "")
        valid: dict[str, Any] = {}
        quantity_keys = {"quantity", "quantityValue", "quantityUnit"}
        if any(key in changes for key in quantity_keys):
            raw_quantity = changes.get("quantity")
            if raw_quantity in (None, ""):
                raw_quantity = changes.get("quantityValue", item.get("quantity"))
            parsed_quantity = parse_quantity(raw_quantity, product, str(changes.get("quantityUnit") or ""))
            if parsed_quantity:
                display, numeric, unit = parsed_quantity
                valid.update({"quantity": display, "quantityValue": numeric, "quantityUnit": unit})
        for key, value in changes.items():
            if key not in ITEM_DEFAULTS or key in quantity_keys or key in {"itemId", "selectedOption", "orderGenerated"}:
                continue
            if value is None:
                continue
            if key == "dimensions" and isinstance(value, dict):
                current_dimensions = dict(item.get("dimensions") or {})
                next_dimensions = dict(DIMENSION_DEFAULTS)
                next_dimensions.update({name: str(current_dimensions.get(name) or "").strip()
                                        for name in DIMENSION_DEFAULTS})
                for name, dimension_value in value.items():
                    if name not in DIMENSION_DEFAULTS:
                        continue
                    next_dimensions[name] = str(dimension_value).strip() if dimension_value is not None else ""
                if next_dimensions != current_dimensions:
                    valid[key] = next_dimensions
            elif key == "productSpecs" and isinstance(value, dict):
                current_specs = dict(item.get("productSpecs") or {})
                next_specs = dict(current_specs)
                dimension_patch: dict[str, Any] = {}
                for name, spec_value in value.items():
                    name = str(name).strip()
                    if not name:
                        continue
                    normalized = str(spec_value).strip() if spec_value is not None else ""
                    if name in DIMENSION_DEFAULTS:
                        dimension_patch[name] = normalized
                        continue
                    if normalized:
                        next_specs[name] = normalized
                    else:
                        next_specs.pop(name, None)
                if next_specs != current_specs:
                    valid[key] = next_specs
                if dimension_patch:
                    valid["dimensions"] = merge_dimension_patch(item.get("dimensions"), dimension_patch)
            elif str(value).strip():
                normalized = str(value).strip()
                if not self._equivalent_value(item.get(key), normalized):
                    valid[key] = normalized
        changed = {key for key, value in valid.items() if item.get(key) != value}
        item.update(valid)
        previous_dimensions = previous.get("dimensions") or {}
        normalize_order_dimensions(item)
        if previous_dimensions != item.get("dimensions"):
            changed.add("dimensions")
        if "productType" in changed and previous.get("productType") and previous.get("productType") != item.get("productType"):
            item["productSpecs"] = {}
            item["dimensions"] = deepcopy(DIMENSION_DEFAULTS)
            changed.add("productSpecs")
            changed.add("dimensions")
        if changed & RECOMMENDATION_FIELDS:
            item["selectedOption"] = None
            item["orderGenerated"] = False
            self.state.setdefault("itemOptions", {}).pop(item.get("itemId") or f"item-{index + 1}", None)
            self._invalidate_delivery_state()
            self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        items[index] = item
        normalize_order_items(self.state["order"])
        item_id = item.get("itemId") or f"item-{index + 1}"
        for key in changed:
            old_value, new_value = previous.get(key), item.get(key)
            field = f"items.{item_id}.{key}"
            if old_value and new_value and old_value != new_value:
                self._record_conflict(field, old_value, new_value, source)
            if key == "productSpecs":
                before = previous.get("productSpecs") or {}
                after = item.get("productSpecs") or {}
                for name in set(before) | set(after):
                    spec_field = f"items.{item_id}.productSpecs.{name}"
                    self._set_field_meta(spec_field, after.get(name), source, confidence)
            elif key == "dimensions":
                before = previous.get("dimensions") or {}
                after = item.get("dimensions") or {}
                for name in DIMENSION_DEFAULTS:
                    spec_field = f"items.{item_id}.dimensions.{name}"
                    old_value, new_value = before.get(name, ""), after.get(name, "")
                    if old_value and new_value and old_value != new_value:
                        self._record_conflict(spec_field, old_value, new_value, source)
                    self._set_field_meta(spec_field, new_value, source, confidence)
            else:
                self._set_field_meta(field, new_value, source, confidence)
        return changed

    def _update_order(self, changes: dict[str, Any], source: str = "rule", confidence: float = 0.84) -> set[str]:
        previous_product = self.state["order"].get("productType")
        previous_order = deepcopy(self.state["order"])
        valid: dict[str, Any] = {}
        # Quantity is kept backwards-compatible as a display string, while the
        # numeric value and unit are stored separately for pricing and adapters.
        quantity_keys = {"quantity", "quantityValue", "quantityUnit"}
        if any(key in changes for key in quantity_keys):
            product_for_quantity = str(changes.get("productType") or previous_product or "")
            raw_quantity = changes.get("quantity")
            if raw_quantity in (None, ""):
                raw_quantity = changes.get("quantityValue", self.state["order"].get("quantity"))
            parsed_quantity = parse_quantity(
                raw_quantity,
                product_for_quantity,
                str(changes.get("quantityUnit") or ""),
            )
            if parsed_quantity:
                display, numeric, unit = parsed_quantity
                valid.update({"quantity": display, "quantityValue": numeric, "quantityUnit": unit})
        for key, value in changes.items():
            if key not in ORDER_DEFAULTS or value is None or key in quantity_keys:
                continue
            if key in {"productTypes", "items"}:
                if isinstance(value, list):
                    candidate = deepcopy(self.state["order"])
                    candidate[key] = deepcopy(value)
                    if key == "items":
                        normalize_order_items(candidate)
                        normalized_items = candidate["items"]
                    else:
                        normalized_items = deepcopy(value)
                    if normalized_items != self.state["order"].get(key):
                        valid[key] = normalized_items
                    if key == "items":
                        next_types = candidate.get("productTypes", [])
                        if next_types != self.state["order"].get("productTypes", []):
                            valid["productTypes"] = next_types
                continue
            if key == "dimensions" and isinstance(value, dict):
                current_dimensions = dict(self.state["order"].get("dimensions") or {})
                next_dimensions = dict(DIMENSION_DEFAULTS)
                next_dimensions.update({name: str(current_dimensions.get(name) or "").strip()
                                        for name in DIMENSION_DEFAULTS})
                for name, dimension_value in value.items():
                    if name not in DIMENSION_DEFAULTS:
                        continue
                    next_dimensions[name] = str(dimension_value).strip() if dimension_value is not None else ""
                if next_dimensions != current_dimensions:
                    valid[key] = next_dimensions
            elif key == "productSpecs" and isinstance(value, dict):
                current_specs = dict(self.state["order"].get("productSpecs") or {})
                next_specs = dict(current_specs)
                dimension_patch: dict[str, Any] = {}
                for name, item in value.items():
                    name = str(name).strip()
                    if not name:
                        continue
                    normalized = str(item).strip() if item is not None else ""
                    if name in DIMENSION_DEFAULTS:
                        dimension_patch[name] = normalized
                        continue
                    if normalized:
                        if self._equivalent_value(next_specs.get(name), normalized):
                            if source == "user":
                                self._set_field_meta(f"productSpecs.{name}", next_specs.get(name), source, confidence)
                            continue
                        next_specs[name] = normalized
                    else:
                        next_specs.pop(name, None)
                if next_specs != current_specs:
                    valid[key] = next_specs
                if dimension_patch:
                    valid["dimensions"] = merge_dimension_patch(self.state["order"].get("dimensions"), dimension_patch)
            elif str(value).strip():
                normalized = str(value).strip()
                if self._equivalent_value(self.state["order"].get(key), normalized):
                    if source == "user":
                        self._set_field_meta(key, self.state["order"].get(key), source, confidence)
                    continue
                valid[key] = normalized
        changed = {key for key, value in valid.items() if self.state["order"].get(key) != value}
        previous_dimensions = previous_order.get("dimensions") or {}
        self.state["order"].update(valid)
        normalize_order_dimensions(self.state["order"])
        normalize_order_items(self.state["order"])
        if previous_dimensions != self.state["order"].get("dimensions"):
            changed.add("dimensions")
        if "productType" in changed and previous_product and previous_product != self.state["order"].get("productType"):
            # Product-specific fields belong to the old item and must not leak into a new draft.
            self.state["order"]["productSpecs"] = {}
            self.state["order"]["dimensions"] = deepcopy(DIMENSION_DEFAULTS)
            changed.add("productSpecs")
            changed.add("dimensions")
            for field in list(self.state.get("fieldMeta") or {}):
                if field.startswith("productSpecs."):
                    self.state["fieldMeta"].pop(field, None)
        for key in changed:
            if key == "productSpecs":
                before = previous_order.get("productSpecs") or {}
                after = self.state["order"].get("productSpecs") or {}
                for name in set(before) | set(after):
                    old_value, new_value = before.get(name, ""), after.get(name, "")
                    field = f"productSpecs.{name}"
                    if old_value and new_value and old_value != new_value:
                        self._record_conflict(field, old_value, new_value, source)
                    self._set_field_meta(field, new_value, source, confidence)
            elif key == "dimensions":
                before = previous_order.get("dimensions") or {}
                after = self.state["order"].get("dimensions") or {}
                for name in DIMENSION_DEFAULTS:
                    field = f"dimensions.{name}"
                    old_value, new_value = before.get(name, ""), after.get(name, "")
                    if old_value and new_value and old_value != new_value:
                        self._record_conflict(field, old_value, new_value, source)
                    self._set_field_meta(field, new_value, source, confidence)
            else:
                old_value, new_value = previous_order.get(key), self.state["order"].get(key)
                if old_value and new_value and old_value != new_value:
                    self._record_conflict(key, old_value, new_value, source)
                self._set_field_meta(key, new_value, source, confidence)
        if changed & RECOMMENDATION_FIELDS:
            self.state["selectedOption"] = None
            self._invalidate_delivery_state()
            if self.state["stage"] == "confirm": self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        elif "platform" in changed:
            # A platform switch can invalidate a previously mapped handoff even
            # when the production recommendation itself remains unchanged.
            self._invalidate_delivery_state()
            if self.state["stage"] == "confirm": self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        return changed

    @staticmethod
    def _is_explanation_request(text: str) -> bool:
        return bool(re.search(r"怎么选|如何选|什么区别|有什么区别|是什么|解释|为什么", text)) and bool(
            re.search(r"纸|出血|安全边|裁切|覆膜|哑膜|亮膜|专色|四色|黑白|装订|骑马钉|胶装|工艺", text, re.I)
        )

    @staticmethod
    def _validation_message(result: dict[str, Any]) -> str:
        if result["ok"] and not result["warnings"]:
            suffix = "；".join(result.get("suggestions", []))
            return f"订单字段目前完整（信息度 {result.get('readiness', 100)}%），暂未发现规则警告。" + (f"\n建议：{suffix}" if suffix else " 可以调用工艺推荐工具。")
        parts = []
        if result["missing"]: parts.append("还缺少：" + "、".join(result["missing"]))
        if result["warnings"]: parts.append("需要确认：" + "；".join(result["warnings"]))
        if result.get("suggestions"): parts.append("建议：" + "；".join(result["suggestions"]))
        return "。".join(parts) + "。"

    def _apply_plan(self, plan: dict[str, Any]) -> None:
        patch = plan.get("patch") if isinstance(plan, dict) else None
        if not isinstance(patch, dict):
            return
        changed = self._update_order(patch, source="model", confidence=0.68)
        if changed:
            self._event("plan", "ok", "模型提出了受限字段更新", changedFields=sorted(changed))

    def _ask_planner(self, text: str, tool_result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Call a provider with bounded context; provider failures stay inside the Agent."""
        history = self.state["messages"][:-1]
        try:
            if tool_result is None:
                return self.planner.plan(text, self.state["order"], self.available_tools(), history)
            try:
                return self.planner.plan(text, self.state["order"], self.available_tools(), history, tool_result=tool_result)
            except TypeError:
                # Keep compatibility with an older custom planner implementation.
                return self.planner.plan(text, self.state["order"], self.available_tools(), history)
        except Exception:
            if hasattr(self.planner, "last_error"):
                self.planner.last_error = "模型调用异常"
            return None

    def _planner_tool_fallback(self, name: str, result: Any) -> str:
        """Give the user a useful answer if the second synthesis call fails."""
        if name == "recommend_processes":
            return "已生成工艺方案，请在右侧比较效果、成本、交期和注意事项。"
        if name == "validate_order" and isinstance(result, dict):
            return self._validation_message(result)
        if name == "explain_print_term" and isinstance(result, dict):
            return f"{result.get('topic', '印刷基础')}：{result.get('answer', '')}\n\n{result.get('next', '')}".strip()
        if name == "estimate_price" and isinstance(result, dict):
            if result.get("status") == "blocked":
                return str(result.get("assumptions") or "当前订单暂不能合并估算。")
            return (f"按当前信息，费用区间为：{result['range']}。\n{result['assumptions']}"
                    if result.get("range") else f"费用估算还需要：{'、'.join(result.get('missing', []))}。")
        if name == "prepare_handoff":
            return "订单交接信息已准备好，正式提交前请人工确认价格、文件和交期。"
        if name == "request_supplier_quote":
            return "已准备询价请求，正式发送前请人工确认平台、订单字段和交期。"
        if isinstance(result, dict) and result.get("message"):
            return str(result["message"])
        return "工具已完成处理，请查看订单面板中的结果。"

    def _remember(self, role: str, text: str) -> None:
        self.state["messages"] = (self.state["messages"] + [{"role": role, "text": text}])[-HISTORY_LIMIT:]

    def _save(self) -> None:
        self.memory.save(self.id, self.state)

    @staticmethod
    def _perceive(text: str, allow_multi: bool = True) -> dict[str, Any]:
        # Normalize full-width input copied from design tools or IMEs first.
        text = unicodedata.normalize("NFKC", text or "")
        data: dict[str, Any] = {}
        product = find_product(text)
        # Keep invitation cards from the original MVP as a lightweight family.
        if not product and "邀请函" in text:
            product = "邀请函"
        if product:
            data["productType"] = product
        size_matches = Agent._extract_sizes(text)
        size_spans = [(match.start(), match.end()) for match, _ in size_matches]
        quantity_candidates: list[tuple[int, int, tuple[str, int | float, str]]] = []
        explicit_quantity = re.search(
            rf"(?:数量|印刷量|印多少|做多少)\s*(?:改成|改为|调整为|为|是)?\s*{QUANTITY_CAPTURE}",
            text,
        )
        if explicit_quantity:
            raw = "".join(item or "" for item in explicit_quantity.groups())
            parsed = parse_quantity(raw, product, explicit_quantity.group(3) or "")
            if parsed:
                data["quantity"], data["quantityValue"], data["quantityUnit"] = parsed
        for match in re.finditer(rf"(?:约|大约|需要|印刷|做)?\s*{QUANTITY_CAPTURE}", text):
            # Dimension numbers (including B4's 4) are never quantities.
            number_start, number_end = match.span(1)
            if any(start <= number_start and number_end <= end for start, end in size_spans):
                continue
            unit = match.group(3) or match.group(2) or ""
            next_chars = text[match.end():].lstrip()
            prefix_text = text[match.start():number_start]
            has_quantity_context = bool(re.search(r"(?:约|大约|需要|印刷|做|数量)\s*$", prefix_text))
            if (unit or has_quantity_context) and not next_chars.startswith(("x", "X", "×", "*", "\\")):
                parsed = parse_quantity(match.group(0), product, match.group(3) or "")
                if parsed:
                    data["quantity"], data["quantityValue"], data["quantityUnit"] = parsed
                    quantity_candidates.append((number_start, number_end, parsed))
        if size_matches:
            normalized_sizes = []
            for _, size in size_matches:
                if size not in normalized_sizes:
                    normalized_sizes.append(size)
            labeled_dimensions = {
                key: Agent._extract_labeled_size(text, marker)
                for key, marker in (
                    ("finishedSize", r"(?:成品|裁切)(?:尺寸|大小)?"),
                    ("expandedSize", r"(?:展开|摊开)(?:尺寸|大小)?"),
                    ("dieCutSize", r"(?:刀模|刀线)(?:尺寸|大小)?"),
                    ("packageSize", r"(?:包装|盒体|袋体)(?:尺寸|大小)?"),
                )
            }
            labeled_dimensions = {key: value for key, value in labeled_dimensions.items() if value}
            if labeled_dimensions:
                data["dimensions"] = labeled_dimensions
                # ``size`` remains the backwards-compatible finished-size
                # field. Never let an expanded or die-cut size replace it.
                if labeled_dimensions.get("finishedSize"):
                    data["size"] = labeled_dimensions["finishedSize"]
                elif labeled_dimensions.get("packageSize") and _is_three_dimensional_size(labeled_dimensions["packageSize"]):
                    # Keep the legacy display field for structural products;
                    # the nested dimension remains the authoritative meaning.
                    data["size"] = labeled_dimensions["packageSize"]
                elif not data.get("size"):
                    data["size"] = ""
            else:
                data["size"] = " / ".join(normalized_sizes)
        if match := re.search(r"(?:共|约|大约)?\s*(\d{1,3})\s*(?:页|P(?![A-Za-z]))", text, re.I):
            data["pages"] = f"{match.group(1)} 页"
        if re.search(r"横版|横向|横式", text): data["orientation"] = "横版"
        elif re.search(r"竖版|竖向|纵向|直式", text): data["orientation"] = "竖版"
        paper_match = re.search(r"(\d{2,3})\s*(?:g|克)\s*(哑粉纸|铜版纸|白卡纸|牛皮纸|胶版纸)", text, re.I)
        if paper_match:
            data["paper"] = f"{paper_match.group(1)}g {paper_match.group(2)}"
        else:
            for item in ["特种纸", "牛皮纸", "白卡纸", "铜版纸", "哑粉纸", "胶版纸"]:
                if item in text: data["paper"] = item; break
        if re.search(r"双面.*?(?:四色|彩印)?|两面", text): data["printing"] = "双面四色"
        elif re.search(r"单面.*?(?:四色|彩印)?", text): data["printing"] = "单面四色"
        elif "四色" in text or "彩色" in text or "彩印" in text: data["printing"] = "四色印刷"
        elif "黑白" in text or "单色" in text: data["printing"] = "单色印刷"
        finish_names = ["哑膜", "亮膜", "覆膜", "烫金", "烫银", "局部UV", "局部 UV", "击凸", "压凹", "上光"]
        removed_finishes = [item for item in finish_names if re.search(rf"(?:不要|不需要|无需|不用|去掉|取消).{{0,5}}{re.escape(item)}", text, re.I)]
        finishes = [item for item in finish_names if item.lower() in text.lower() and item not in removed_finishes]
        if removed_finishes and not finishes: data["finishing"] = "无特殊工艺"
        elif finishes: data["finishing"] = "、".join(dict.fromkeys(finishes))
        if "骑马钉" in text: data["binding"] = "骑马钉"
        elif "胶装" in text: data["binding"] = "胶装"
        elif "锁线" in text: data["binding"] = "锁线胶装"
        elif re.search(r"(?:不要|不需要|无需|不用|去掉|取消).{0,5}装订", text): data["binding"] = "无需装订"
        if budget_match := re.search(r"预算\s*(?:控制在|不超过|约|为)?\s*([\d,]+)\s*[元块]", text):
            data["budget"] = f"预算 ¥{budget_match.group(1).replace(',', '')}"
        elif re.search(r"低预算|便宜|控制成本|经济|不超过\s*\d+\s*[元块]", text): data["budget"] = "优先控制成本"
        elif re.search(r"高级|质感|精致|有档次", text): data["budget"] = "优先视觉质感"
        elif re.search(r"赶|尽快|明天|后天|下周|三天|两天", text): data["budget"] = "优先交期"
        if match := re.search(r"(今天|明天|后天|(?:三|两|一|四|五|六|七)天(?:后|内)?|\d+\s*天(?:后|内)?|本周[一二三四五六日天]?|下周[一二三四五六日天]?|月底|\d{1,2}月\d{1,2}日)", text):
            data["deadline"] = re.sub(r"\s+", "", match.group(1))
        if "宣传" in text or "推广" in text or "活动" in text: data["purpose"] = "品牌宣传"
        platform_aliases = {"盛大": "shengda", "平台A": "platform_a", "平台 B": "platform_b", "平台B": "platform_b"}
        for phrase, platform in platform_aliases.items():
            if phrase in text: data["platform"] = platform; break
        if not product and re.search(r"天地盖|抽屉盒|折叠盒|飞机盒|开窗盒", text):
            product = data["productType"] = "包装盒"
        specs = Agent._extract_product_specs(text, product, data.get("size", ""))
        if specs:
            data["productSpecs"] = specs
        normalize_order_dimensions(data)
        if allow_multi:
            mentions = _find_product_mentions(text)
            distinct_products = list(dict.fromkeys(item[0] for item in mentions))
            if len(mentions) > 1 and len(distinct_products) > 1:
                items = []
                used_quantities: set[int] = set()
                for index, (item_product, item_start, _) in enumerate(mentions):
                    segment_start = item_start
                    segment_end = mentions[index + 1][1] if index + 1 < len(mentions) else len(text)
                    item = Agent._perceive(text[segment_start:segment_end], allow_multi=False)
                    item["productType"] = item_product
                    if quantity_candidates:
                        mention_mid = (item_start + mentions[index][2]) / 2
                        available = [
                            (candidate_index, candidate)
                            for candidate_index, candidate in enumerate(quantity_candidates)
                            if candidate_index not in used_quantities
                        ]
                        if available:
                            candidate_index, (_, _, quantity) = min(
                                available,
                                key=lambda entry: abs(((entry[1][0] + entry[1][1]) / 2) - mention_mid),
                            )
                            item["quantity"], item["quantityValue"], item["quantityUnit"] = quantity
                            used_quantities.add(candidate_index)
                    # Values written after the product mentions are commonly
                    # shared by every item. Copy only unambiguous shared fields;
                    # product-specific dimensions and specs stay item-local.
                    for shared_key in ("purpose", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget"):
                        if not item.get(shared_key) and data.get(shared_key):
                            item[shared_key] = deepcopy(data[shared_key])
                    if len(size_matches) == 1 and not item.get("size") and data.get("size"):
                        item["size"] = data["size"]
                    if len(size_matches) == 1 and data.get("dimensions"):
                        item["dimensions"] = deepcopy(data["dimensions"])
                    items.append(item)
                data["productType"] = mentions[0][0]
                data["productTypes"] = distinct_products
                data["items"] = items
        return data

    @staticmethod
    def _extract_product_specs(text: str, product: str, size: str) -> dict[str, str]:
        """Extract only the product-specific details understood by the MVP catalog."""
        specs: dict[str, str] = {}

        def set_if(pattern: str, key: str, value: str | None = None, flags: int = re.I) -> None:
            match = re.search(pattern, text, flags)
            if match:
                specs[key] = value or match.group(1)

        fold = re.search(r"(二折|三折|四折|对折|风琴折|荷包折|卷折)", text)
        if fold:
            specs["folding"] = fold.group(1)
        parts = re.search(r"([二三四五六])\s*联", text)
        if parts:
            specs["paperParts"] = f"{parts.group(1)}联"
        if re.search(r"流水号|连续编号|打号码|编号", text):
            specs["numbering"] = "需要连续编号"
        if re.search(r"(?:不要|不需要|无需|不用).{0,4}(?:编号|流水号)", text):
            specs["numbering"] = "不需要编号"

        structure = re.search(r"(天地盖|抽屉盒|折叠盒|飞机盒|书型盒|开窗盒|异型盒)", text)
        if structure:
            specs["boxStructure"] = structure.group(1)
        dimensions = [value for _, value in Agent._extract_sizes(text)]
        three_dimensions = next((value for value in dimensions if value.count("×") >= 2), "")
        if product == "包装盒" or structure:
            if three_dimensions:
                specs["boxSize"] = three_dimensions
            if "刀模" in text or "刀线" in text:
                specs["dieCut"] = "需确认刀模文件" if re.search(r"没有|无|未有|需要制作", text) else "已有/提供刀模文件"
        if product == "手提袋" and three_dimensions:
            specs["bagSize"] = three_dimensions
        if product == "信封封套" and size:
            specs["envelopeSize"] = size
        if product in {"海报", "喷画", "PVC"} and size:
            specs["displaySize" if product != "PVC" else "boardSize"] = size

        material_terms = ["铜版不干胶", "透明不干胶", "牛皮纸不干胶", "PET", "PVC", "热敏纸", "不干胶"]
        material = next((term for term in material_terms if term.lower() in text.lower()), "")
        if material and product == "标签":
            specs["labelMaterial"] = material
        shape = re.search(r"(方形|圆形|椭圆形|异形|圆角)", text)
        if shape and product == "标签":
            specs["labelShape"] = shape.group(1)
        adhesive = re.search(r"(可移胶|强粘胶|普通胶|冷冻胶|可移除胶)", text)
        if adhesive and product == "标签":
            specs["adhesive"] = adhesive.group(1)

        bag_material = next((term for term in ["无纺布", "帆布", "牛皮纸", "白卡纸"] if term in text), "")
        if bag_material and product == "手提袋":
            specs["bagMaterial"] = bag_material
        handle = re.search(r"(棉绳|扁绳|丝带|尼龙绳|手挽绳|穿绳)", text)
        if handle and product == "手提袋":
            specs["handle"] = handle.group(1)
        if product == "手提袋" and "承重" in text:
            load = re.search(r"承重\s*(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)?", text, re.I)
            specs["loadBearing"] = f"{load.group(1)}kg" if load else "需确认承重"

        volume = re.search(r"(\d+(?:\.\d+)?)\s*(?:ml|毫升)", text, re.I)
        if volume and product == "纸杯":
            specs["cupVolume"] = f"{volume.group(1)}ml"
        cup_material = re.search(r"(单\s*PE|双\s*PE|食品级纸杯纸)", text, re.I)
        if cup_material and product == "纸杯":
            specs["cupMaterial"] = re.sub(r"\s+", " ", cup_material.group(1)).upper() if "PE" in cup_material.group(1).upper() else cup_material.group(1)
        if product == "纸杯" and re.search(r"不需要?淋膜|无淋膜", text):
            specs["innerCoating"] = "不需要内淋膜"
        elif product == "纸杯" and "淋膜" in text:
            specs["innerCoating"] = "需要内淋膜"

        display_material = next((term for term in ["背胶", "灯片", "车贴", "灯布", "相纸", "写真布", "KT板", "刀刮布", "PVC"] if term.lower() in text.lower()), "")
        if display_material and product in {"海报", "喷画"}:
            specs["displayMaterial"] = display_material
        if product == "PVC":
            thickness = re.search(r"(?:板材厚度|厚度|PVC)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米)", text, re.I)
            if thickness:
                specs["boardThickness"] = f"{thickness.group(1)}mm"
        install = next((term for term in ["墙面张贴", "墙面", "裱板", "展架", "易拉宝", "打孔", "包边", "挂装", "支架", "户外", "室内"] if term in text), "")
        if install and product in {"海报", "喷画", "PVC"}:
            specs["install"] = install
        distance = re.search(r"(?:观看距离|距离)\s*(\d+(?:\.\d+)?)\s*(米|m)", text, re.I)
        if distance and product == "喷画":
            specs["viewingDistance"] = f"{distance.group(1)}米"

        if product == "名片":
            if "圆角" in text: specs["cardCorners"] = "圆角"
            elif "直角" in text: specs["cardCorners"] = "直角"
            if "专色" in text: specs["cardColor"] = "专色"
        if product == "PVC卡":
            card_type = re.search(r"(智能卡|人像证卡|滴胶卡|冲切卡|异形卡)", text)
            if card_type: specs["cardType"] = card_type.group(1)
            thickness = re.search(r"(?:卡片厚度|卡厚|厚度)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米)", text, re.I)
            if not thickness:
                thickness = re.search(r"(?<![\d×x*])((?:0?\.38|0?\.5|0?\.76|0?\.8|0?\.84))\s*(?:mm|毫米)", text, re.I)
            if thickness: specs["cardThickness"] = f"{thickness.group(1)}mm"
            if re.search(r"芯片|磁条|IC卡|ID卡", text, re.I): specs["chip"] = "需要芯片/磁条"
        if product == "吊牌":
            hole = re.search(r"(圆孔|蝴蝶孔|挂孔|打孔)", text)
            if hole: specs["hangHole"] = hole.group(1)
            string = re.search(r"(棉绳|扁绳|丝带|尼龙绳|别针|配绳)", text)
            if string: specs["string"] = string.group(1)
        if "出血" in text and product in {"单页", "折页", "名片", "宣传册", "画册"}:
            bleed = re.search(r"出血\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米)?", text, re.I)
            specs["bleed"] = f"{bleed.group(1)}mm" if bleed else "需确认出血"
        opening = re.search(r"(上开口|侧开口|自封|胶条封口|不干胶封口)", text)
        if opening and product == "信封封套":
            specs["opening"] = opening.group(1)
        if product == "信封封套" and "开口" in text and "opening" not in specs:
            specs["opening"] = "需确认开口方向"
        if product == "数码印刷":
            if re.search(r"可变数据|每份不同|个性化|流水号", text): specs["variableData"] = "需要可变数据"
            if "打样" in text: specs["proofing"] = "需要打样"
        return specs

    @staticmethod
    def _extract_sizes(text: str) -> list[tuple[re.Match[str], str]]:
        """Find standard or custom finished sizes and return normalized labels."""
        separator = r"(?:[x×✕✖]|(?:\\)?\*)"
        dimension = (
            rf"\d{{2,4}}\s*(?:mm|毫米|cm|厘米)?\s*{separator}\s*"
            rf"\d{{2,4}}\s*(?:mm|毫米|cm|厘米)?"
            rf"(?:\s*{separator}\s*\d{{2,4}}\s*(?:mm|毫米|cm|厘米)?)?"
        )
        pattern = re.compile(
            rf"(?<![A-Za-z0-9])(?:[AB]\s*-?\s*[3-6](?!\d)|{dimension})"
            rf"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        found: list[tuple[re.Match[str], str]] = []
        for match in pattern.finditer(text):
            compact = re.sub(r"\s+", "", match.group(0)).upper()
            if re.fullmatch(r"[AB]-?[3-6]", compact):
                size = compact.replace("-", "")
            else:
                size = compact.replace("毫米", "MM").replace("厘米", "CM")
                size = size.replace("\\*", "×").replace("*", "×")
                size = size.replace("X", "×").replace("✕", "×").replace("✖", "×")
                size = re.sub(r"(?:MM|CM)(?=×)", "", size)
            found.append((match, size))
        return found

    @staticmethod
    def _extract_labeled_size(text: str, marker_pattern: str) -> str:
        """Read the first size immediately following a dimension label."""
        marker = re.search(rf"{marker_pattern}\s*[:：]?", text, re.IGNORECASE)
        if not marker:
            return ""
        tail = text[marker.end(): marker.end() + 96]
        matches = Agent._extract_sizes(tail)
        return matches[0][1] if matches else ""

    @staticmethod
    def _question(key: str) -> str:
        return {"productType": "你想做哪一种印刷品？", "quantity": "大约需要多少份？", "size": "成品尺寸是多少？",
                "paper": "对纸张有偏好吗？不确定可以选按效果推荐。", "printing": "需要单面、双面还是黑白印刷？",
                "deadline": "什么时候需要拿到成品？"}[key]

    @staticmethod
    def _quick_replies(key: str, product: str | None = None) -> list[dict[str, Any]]:
        choices = {
            "productType": [("宣传册", "宣传册"), ("折页", "折页"), ("名片", "名片"), ("包装盒", "包装盒")],
            "quantity": [(f"100 {default_quantity_unit(product)}", f"100 {default_quantity_unit(product)}"),
                         (f"500 {default_quantity_unit(product)}", f"500 {default_quantity_unit(product)}"),
                         (f"1,000 {default_quantity_unit(product)}", f"1000 {default_quantity_unit(product)}")],
            "size": [("A4", "A4"), ("A5", "A5"), ("210 × 285 mm", "210×285MM")],
            "paper": [("按效果推荐", "待推荐"), ("157g 哑粉纸", "157g 哑粉纸"), ("250g 铜版纸", "250g 铜版纸")],
            "printing": [("双面四色", "双面四色"), ("单面四色", "单面四色"), ("黑白/单色", "单色印刷")],
            "deadline": [("一周内", "一周内"), ("两周内", "两周内"), ("时间不紧", "时间不紧")],
        }
        return [{"label": label, "data": {key: value}} for label, value in choices.get(key, [])]

    @staticmethod
    def _product_quick_replies(key: str) -> list[dict[str, Any]]:
        choices = {
            "folding": [("二折", "二折"), ("三折", "三折"), ("风琴折", "风琴折")],
            "paperParts": [("二联", "二联"), ("三联", "三联"), ("四联", "四联")],
            "boxStructure": [("天地盖", "天地盖"), ("抽屉盒", "抽屉盒"), ("折叠盒", "折叠盒")],
            "labelMaterial": [("铜版不干胶", "铜版不干胶"), ("透明不干胶", "透明不干胶"), ("PET", "PET")],
            "labelShape": [("方形", "方形"), ("圆形", "圆形"), ("异形", "异形")],
            "cardType": [("智能卡", "智能卡"), ("人像证卡", "人像证卡"), ("滴胶卡", "滴胶卡")],
            "cardThickness": [("0.38mm", "0.38mm"), ("0.76mm", "0.76mm"), ("其他厚度", "需确认卡片厚度")],
            "hangHole": [("圆孔", "圆孔"), ("蝴蝶孔", "蝴蝶孔"), ("打孔", "打孔")],
            "string": [("棉绳", "棉绳"), ("扁绳", "扁绳"), ("不需要配绳", "无需配绳")],
            "bagMaterial": [("白卡纸", "白卡纸"), ("牛皮纸", "牛皮纸"), ("无纺布", "无纺布")],
            "handle": [("棉绳", "棉绳"), ("扁绳", "扁绳"), ("丝带", "丝带")],
            "cupMaterial": [("单 PE", "单 PE"), ("双 PE", "双 PE")],
            "innerCoating": [("需要内淋膜", "需要内淋膜"), ("不需要内淋膜", "不需要内淋膜")],
            "displayMaterial": [("背胶", "背胶"), ("灯片", "灯片"), ("车贴", "车贴")],
            "install": [("墙面张贴", "墙面张贴"), ("裱板", "裱板"), ("打孔包边", "打孔包边")],
            "boardThickness": [("3mm", "3mm"), ("5mm", "5mm"), ("10mm", "10mm")],
        }
        return [{"label": label, "data": {"productSpecs": {key: value}}} for label, value in choices.get(key, [])]
